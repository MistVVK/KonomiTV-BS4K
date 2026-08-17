#include <aribtlv/demuxer.hpp>

#include <algorithm>
#include <array>
#include <charconv>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <limits>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <tuple>
#include <utility>

namespace {

constexpr std::uint64_t DEFAULT_PROBE_SIZE = 256ULL * 1024ULL * 1024ULL;
// stdin ストリーミングでは SDT をできるだけ早く出力できるよう小さめの読み取り単位にする。
// 1 MiB まで読もうとすると SDT 到着後も読取り完了まで出力が遅れるため、64 KiB に抑える。
constexpr std::size_t STDIN_READ_BUFFER_SIZE = 64 * 1024;
// 録画ファイル解析は従来の 1 MiB 単位を維持し、大容量ファイルの読み取り syscall を増やさない。
constexpr std::size_t FILE_READ_BUFFER_SIZE = 1024 * 1024;

void WriteJSONString(std::ostream& output, const std::string_view value) {
    static constexpr char hexadecimal[] = "0123456789abcdef";

    output.put('"');
    for (const auto character : value) {
        const auto byte = static_cast<unsigned char>(character);
        switch (byte) {
        case '"': output << "\\\""; break;
        case '\\': output << "\\\\"; break;
        case '\b': output << "\\b"; break;
        case '\f': output << "\\f"; break;
        case '\n': output << "\\n"; break;
        case '\r': output << "\\r"; break;
        case '\t': output << "\\t"; break;
        default:
            if (byte < 0x20) {
                output << "\\u00" << hexadecimal[byte >> 4U] << hexadecimal[byte & 0x0fU];
            } else {
                output.put(static_cast<char>(byte));
            }
            break;
        }
    }
    output.put('"');
}

template<typename Value>
void WriteOptionalInteger(std::ostream& output, const std::optional<Value>& value) {
    if (value.has_value()) {
        output << *value;
    } else {
        output << "null";
    }
}

const char* TrackKindName(const aribtlv::TrackKind kind) {
    switch (kind) {
    case aribtlv::TrackKind::Video: return "Video";
    case aribtlv::TrackKind::Audio: return "Audio";
    case aribtlv::TrackKind::Subtitle: return "Subtitle";
    }
    return "Unknown";
}

const char* CodecName(const aribtlv::Codec codec) {
    switch (codec) {
    case aribtlv::Codec::Hevc: return "HEVC";
    case aribtlv::Codec::AacLatm: return "AAC-LATM";
    case aribtlv::Codec::Ttml: return "TTML";
    }
    return "Unknown";
}

class MetadataSink final : public aribtlv::Sink {
public:
    // streaming 時はサービス・トラックの現在スナップショットを stdout へ出力し、
    // 呼び出し側 (ライブ視聴のプローブ) が目的の service_id と映像を得たら早期終了できるようにする。
    explicit MetadataSink(const bool streaming = false) : streaming_(streaming) {}

    void onService(const aribtlv::ServiceInfo&) override {}

    void onTrack(const aribtlv::TrackInfo& track) override {
        // streaming では完全な MPT snapshot を正本にする。個別 callback は MPT commit 後に順次呼ばれるため、
        // ここで出力すると Video の置換途中を一時的な不在として通知してしまう。
        if (streaming_) return;
        // 同じ安定 track ID の更新通知は、最新の MPT 情報で置き換える。
        tracks_[track.track_id] = track;
    }

    void onTrackRemoved(const aribtlv::TrackInfo& track) override {
        if (streaming_) return;
        tracks_.erase(track.track_id);
    }

    void onAccessUnit(aribtlv::AccessUnit&&) override {}

    void onError(const aribtlv::Error& error) override {
        // 復旧不能な入力エラーだけを終了理由として保持し、JSON stdout は汚さない。
        if (!error.recoverable) {
            fatal_error_ = error.message;
        }
    }

    void onMhSdtSnapshot(const aribtlv::MhSdtSnapshot& snapshot) override {
        // SDT はスナップショットなので、同一 context の古いサービスを一度除去する。
        for (auto iterator = services_.begin(); iterator != services_.end();) {
            if (std::get<0>(iterator->first) == snapshot.context_id) {
                iterator = services_.erase(iterator);
            } else {
                ++iterator;
            }
        }
        for (const auto& service : snapshot.services) {
            services_[{snapshot.context_id, service.service_id}] = ServiceRecord{
                snapshot.context_id,
                snapshot.tlv_stream_id,
                snapshot.original_network_id,
                service,
            };
        }
        // streaming では SDT 更新のたびに、その時点の services / tracks 全件を 1 行で出力する。
        // 呼び出し側は最新の完了行だけでサービスと映像トラックの対応を検証できる。
        streaming_snapshot_metadata_ = StreamingSnapshotMetadata{
            "MHSDT",
            snapshot.context_id,
            snapshot.version,
            snapshot.input_offset,
        };
        emitStreamingSnapshot();
    }

    void onMptSnapshot(const aribtlv::MptSnapshot& snapshot) override {
        if (!streaming_) return;

        // MptSnapshot は context 単位の完全で検証済みのトラック一覧なので、個別 onTrack callback を
        // 合成せず、対象 context の状態を一括置換して原子的な Video 有無を通知する。
        for (auto iterator = tracks_.begin(); iterator != tracks_.end();) {
            if (iterator->second.context_id == snapshot.context_id) {
                iterator = tracks_.erase(iterator);
            } else {
                ++iterator;
            }
        }
        for (const auto& track : snapshot.tracks) {
            tracks_[track.track_id] = track;
        }
        streaming_snapshot_metadata_ = StreamingSnapshotMetadata{
            "MPT",
            snapshot.context_id,
            snapshot.version,
            snapshot.input_offset,
        };
        emitStreamingSnapshot();
    }

    void onEventInfo(const aribtlv::EventInfo& event) override {
        // EIT の再送では input offset が後の情報を優先し、同じイベントを重複出力しない。
        const auto key = std::tuple{event.context_id, event.service_id, event.event_id};
        const auto found = events_.find(key);
        if (found == events_.end() || found->second.input_offset <= event.input_offset) {
            events_[key] = event;
        }
    }

    void onMhTot(const aribtlv::MhTotInfo& tot) override {
        // 先頭・末尾 window で観測した context ごとの最初と最後の時刻を保持する。
        const auto found = tots_.find(tot.context_id);
        if (found == tots_.end()) {
            tots_[tot.context_id] = {tot, tot};
            return;
        }
        if (tot.input_offset < found->second.first.input_offset) found->second.first = tot;
        if (found->second.last.input_offset < tot.input_offset) found->second.last = tot;
    }

    [[nodiscard]] bool hasFatalError() const noexcept {
        return fatal_error_.has_value();
    }

    [[nodiscard]] const std::string& fatalError() const {
        return *fatal_error_;
    }

    [[nodiscard]] bool hasEmittedStreamingSnapshot() const noexcept {
        return has_emitted_streaming_snapshot_;
    }

    void writeStreamingJSONLine(std::ostream& output) const {
        output << '{';
        output << "\"snapshot_type\":";
        if (streaming_snapshot_metadata_.has_value()) {
            WriteJSONString(output, streaming_snapshot_metadata_->type);
            output << ",\"snapshot_context_id\":" << streaming_snapshot_metadata_->context_id
                   << ",\"snapshot_version\":" << static_cast<unsigned int>(streaming_snapshot_metadata_->version)
                   << ",\"input_offset\":" << streaming_snapshot_metadata_->input_offset;
        } else {
            output << "null,\"snapshot_context_id\":null,\"snapshot_version\":null,\"input_offset\":null";
        }
        output.put(',');
        writeServices(output);
        output.put(',');
        writeTracks(output);
        output << "}\n";
        output.flush();
    }

    void writeJSON(std::ostream& output) const {
        output << '{';
        writeServices(output);
        output << ",\"events\":[";
        bool first = true;
        for (const auto& [key, event] : events_) {
            static_cast<void>(key);
            if (!first) output.put(',');
            first = false;
            output << "{\"context_id\":" << event.context_id
                   << ",\"service_id\":" << event.service_id
                   << ",\"event_id\":" << event.event_id
                   << ",\"table_id\":" << static_cast<unsigned int>(event.table_id)
                   << ",\"section_number\":" << static_cast<unsigned int>(event.section_number)
                   << ",\"input_offset\":" << event.input_offset
                   << ",\"start_time_unix_milliseconds\":";
            WriteOptionalInteger(output, event.start_time_unix_milliseconds);
            output << ",\"duration_seconds\":";
            WriteOptionalInteger(output, event.duration_seconds);
            output << ",\"free_ca_mode\":" << (event.free_ca_mode ? "true" : "false")
                   << ",\"title\":";
            WriteJSONString(output, event.title);
            output << ",\"description\":";
            WriteJSONString(output, event.description);
            output << ",\"extended_description\":";
            WriteJSONString(output, event.extended_description);
            output << ",\"genres\":[";
            bool first_genre = true;
            for (const auto& genre : event.genres) {
                if (!first_genre) output.put(',');
                first_genre = false;
                output << "{\"level1\":" << static_cast<unsigned int>(genre.level1)
                       << ",\"level2\":" << static_cast<unsigned int>(genre.level2)
                       << ",\"user1\":" << static_cast<unsigned int>(genre.user1)
                       << ",\"user2\":" << static_cast<unsigned int>(genre.user2) << '}';
            }
            output << "],\"audio_components\":[";
            bool first_audio = true;
            for (const auto& audio : event.audio_components) {
                if (!first_audio) output.put(',');
                first_audio = false;
                output << "{\"component_tag\":" << audio.audio.component_tag
                       << ",\"component_type\":" << static_cast<unsigned int>(audio.audio.component_type)
                       << ",\"main_component\":" << (audio.audio.main_component ? "true" : "false")
                       << ",\"language\":";
                WriteJSONString(output, audio.language);
                output << ",\"secondary_language\":";
                WriteJSONString(output, audio.audio.secondary_language);
                output << ",\"text\":";
                WriteJSONString(output, audio.text);
                output.put('}');
            }
            output << "]}";
        }

        output << "],\"tots\":[";
        first = true;
        for (const auto& [context_id, range] : tots_) {
            const std::array<const aribtlv::MhTotInfo*, 2> values{&range.first, &range.last};
            for (std::size_t index = 0; index < values.size(); ++index) {
                const auto* tot = values[index];
                if (index == 1 && range.first.input_offset == range.last.input_offset) break;
                if (!first) output.put(',');
                first = false;
                output << "{\"context_id\":" << context_id
                       << ",\"time_unix_milliseconds\":" << tot->time_unix_milliseconds
                       << ",\"input_offset\":" << tot->input_offset << '}';
            }
        }

        output << "],";
        writeTracks(output);
        output << "}\n";
    }

private:
    // tracks_ を「"tracks":[...]」の形で出力する。録画解析と stdin streaming の両方から使う。
    void writeTracks(std::ostream& output) const {
        output << "\"tracks\":[";
        bool first = true;
        for (const auto& [track_id, track] : tracks_) {
            if (!first) output.put(',');
            first = false;
            output << "{\"track_id\":" << track_id
                   << ",\"context_id\":" << track.context_id
                   << ",\"packet_id\":" << track.packet_id
                   << ",\"component_tag\":" << track.component_tag
                   << ",\"kind\":";
            WriteJSONString(output, TrackKindName(track.kind));
            output << ",\"codec\":";
            WriteJSONString(output, CodecName(track.codec));
            output << ",\"language\":";
            WriteJSONString(output, track.language);
            output << ",\"timescale\":" << track.timescale;
            if (track.audio.has_value()) {
                output << ",\"audio_sample_rate\":" << track.audio->sample_rate
                       << ",\"audio_channels\":" << aribtlv::audio_channel_count(track.audio->channel_layout)
                       << ",\"audio_main_component\":" << (track.audio->main_component ? "true" : "false");
            } else {
                output << ",\"audio_sample_rate\":null,\"audio_channels\":null,\"audio_main_component\":false";
            }
            output.put('}');
        }
        output.put(']');
    }

    void emitStreamingSnapshot() {
        if (!streaming_) return;
        writeStreamingJSONLine(std::cout);
        has_emitted_streaming_snapshot_ = true;
    }

    // services_ を「"services":[...]」の形で出力する。録画解析と stdin streaming の両方から使う。
    void writeServices(std::ostream& output) const {
        output << "\"services\":[";
        bool first = true;
        for (const auto& [key, record] : services_) {
            static_cast<void>(key);
            if (!first) output.put(',');
            first = false;
            output << "{\"context_id\":" << record.context_id
                   << ",\"service_id\":" << record.service.service_id
                   << ",\"tlv_stream_id\":" << record.tlv_stream_id
                   << ",\"original_network_id\":" << record.original_network_id
                   << ",\"service_type\":" << static_cast<unsigned int>(record.service.service_type)
                   << ",\"provider_name\":";
            WriteJSONString(output, record.service.provider_name);
            output << ",\"service_name\":";
            WriteJSONString(output, record.service.service_name);
            output.put('}');
        }
        output.put(']');
    }

    struct ServiceRecord {
        std::uint32_t context_id;
        std::uint16_t tlv_stream_id;
        std::uint16_t original_network_id;
        aribtlv::ServiceDescriptionInfo service;
    };

    struct StreamingSnapshotMetadata {
        std::string type;
        std::uint32_t context_id;
        std::uint8_t version;
        std::uint64_t input_offset;
    };

    std::map<std::tuple<std::uint32_t, std::uint16_t>, ServiceRecord> services_;
    std::map<std::tuple<std::uint32_t, std::uint16_t, std::uint16_t>, aribtlv::EventInfo> events_;
    struct TotRange {
        aribtlv::MhTotInfo first;
        aribtlv::MhTotInfo last;
    };

    std::map<std::uint32_t, TotRange> tots_;
    std::map<std::uint64_t, aribtlv::TrackInfo> tracks_;
    std::optional<std::string> fatal_error_;
    // 直近に出力した完全 snapshot の識別情報。入力未確定時の空行では null のままにする。
    std::optional<StreamingSnapshotMetadata> streaming_snapshot_metadata_;
    // サービス・トラック更新ごとに snapshot を逐次出力するか (stdin プローブ専用)
    bool streaming_ = false;
    // streaming snapshot を 1 回以上出力済みか (stdin で情報未取得のまま終了した場合の判定用)
    bool has_emitted_streaming_snapshot_ = false;
};

std::uint64_t ParseProbeSize(const char* text) {
    std::uint64_t value = 0;
    const auto length = std::char_traits<char>::length(text);
    const auto result = std::from_chars(text, text + length, value);
    if (result.ec != std::errc{} || result.ptr != text + length || value == 0) {
        throw std::invalid_argument("probe size must be a positive integer");
    }
    return value;
}

}  // namespace

int main(const int argc, char* argv[]) {
    try {
        if (argc < 2 || argc > 3) {
            std::cerr << "Usage: KonomiTVBS4KTLVMetadata.elf INPUT|- [MAX_BYTES|--follow]\n";
            return 2;
        }
        const bool use_stdin = (std::string(argv[1]) == "-");
        const bool follow_stdin = argc == 3 && std::string(argv[2]) == "--follow";
        if (follow_stdin && !use_stdin) {
            std::cerr << "--follow is only available for stdin input.\n";
            return 2;
        }
        const auto probe_size = argc == 3 && !follow_stdin ? ParseProbeSize(argv[2]) : DEFAULT_PROBE_SIZE;

        MetadataSink sink(use_stdin);
        aribtlv::Limits limits;
        limits.collect_application_resources = false;
        aribtlv::Demuxer demuxer(sink, limits);
        std::array<std::uint8_t, FILE_READ_BUFFER_SIZE> buffer{};

        // stdin は非シークのため専用経路を使い、ifstream のサイズ検出・seek は行わない。
        const auto read_window = [&](
            std::istream& source,
            const std::uint64_t maximum_bytes,
            const std::size_t read_buffer_size = FILE_READ_BUFFER_SIZE
        ) {
            std::uint64_t total_read = 0;
            while (source && total_read < maximum_bytes && !sink.hasFatalError()) {
                const auto remaining = maximum_bytes - total_read;
                const auto request_size = static_cast<std::streamsize>(
                    std::min<std::uint64_t>(remaining, read_buffer_size));
                source.read(reinterpret_cast<char*>(buffer.data()), request_size);
                const auto bytes_read = source.gcount();
                if (bytes_read <= 0) break;
                demuxer.push(buffer.data(), static_cast<std::size_t>(bytes_read));
                total_read += static_cast<std::uint64_t>(bytes_read);
            }
        };

        if (use_stdin) {
            // 継続監視では EOF まで読み、通常プローブでは従来どおり指定上限で打ち切る。
            if (follow_stdin) {
                while (std::cin && !sink.hasFatalError()) {
                    std::cin.read(reinterpret_cast<char*>(buffer.data()), STDIN_READ_BUFFER_SIZE);
                    const auto bytes_read = std::cin.gcount();
                    if (bytes_read <= 0) break;
                    demuxer.push(buffer.data(), static_cast<std::size_t>(bytes_read));
                }
            } else {
                read_window(std::cin, probe_size, STDIN_READ_BUFFER_SIZE);
            }
            demuxer.flush();
            if (sink.hasFatalError()) {
                std::cerr << "MMT/TLV metadata demuxing failed: " << sink.fatalError() << '\n';
                return 1;
            }
            // 情報を一度も取得できなかった場合だけ、空の snapshot を返して呼び出し側へ通知する。
            if (!sink.hasEmittedStreamingSnapshot()) {
                sink.writeStreamingJSONLine(std::cout);
            }
            return 0;
        }

        std::ifstream input(argv[1], std::ios::binary | std::ios::ate);
        if (!input) {
            std::cerr << "Failed to open the input file.\n";
            return 1;
        }
        const auto end_position = input.tellg();
        if (end_position == std::streampos(-1)) {
            std::cerr << "Failed to determine the input file size.\n";
            return 1;
        }
        const auto file_size_offset = static_cast<std::streamoff>(end_position);
        if (file_size_offset < 0) {
            std::cerr << "Invalid input file size.\n";
            return 1;
        }
        const auto file_size = static_cast<std::uint64_t>(file_size_offset);
        input.seekg(0, std::ios::beg);

        // 小さい録画は全体を、大きい録画は上限を二分して先頭と末尾を読む。
        // MH-TOT / MH-EIT の両端を得つつ、巨大録画でも解析 IO を一定に抑える。
        if (file_size <= probe_size) {
            read_window(input, file_size);
        } else {
            const auto front_size = (probe_size / 2) + (probe_size % 2);
            const auto back_size = probe_size - front_size;
            read_window(input, front_size);
            if (back_size > 0 && !sink.hasFatalError()) {
                const auto back_offset = file_size - back_size;
                input.clear();
                input.seekg(static_cast<std::streamoff>(back_offset), std::ios::beg);
                if (!input) {
                    std::cerr << "Failed to seek to the final metadata window.\n";
                    return 1;
                }
                demuxer.reposition(aribtlv::RepositionOptions{back_offset, false});
                read_window(input, back_size);
            }
        }
        demuxer.flush();
        if (sink.hasFatalError()) {
            std::cerr << "MMT/TLV metadata demuxing failed: " << sink.fatalError() << '\n';
            return 1;
        }
        sink.writeJSON(std::cout);
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "MMT/TLV metadata extraction failed: " << error.what() << '\n';
        return 1;
    }
}
