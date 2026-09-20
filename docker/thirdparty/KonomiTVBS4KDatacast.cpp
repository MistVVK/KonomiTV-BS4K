#include "KonomiTVBS4KDatacast.hpp"

#include <algorithm>
#include <array>
#include <cstdint>
#include <limits>
#include <sstream>
#include <string>
#include <string_view>

namespace {

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
                output << "\\u00" << hexadecimal[byte >> 4U]
                       << hexadecimal[byte & 0x0fU];
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

std::string Base64Encode(const std::uint8_t* data, const std::size_t size) {
    static constexpr std::string_view alphabet =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string encoded;
    encoded.reserve(((size + 2) / 3) * 4);
    for (std::size_t offset = 0; offset < size; offset += 3) {
        const auto remaining = size - offset;
        const auto first = static_cast<std::uint32_t>(data[offset]);
        const auto second = remaining > 1 ? static_cast<std::uint32_t>(data[offset + 1]) : 0U;
        const auto third = remaining > 2 ? static_cast<std::uint32_t>(data[offset + 2]) : 0U;
        const auto value = (first << 16U) | (second << 8U) | third;
        encoded.push_back(alphabet[(value >> 18U) & 0x3fU]);
        encoded.push_back(alphabet[(value >> 12U) & 0x3fU]);
        encoded.push_back(remaining > 1 ? alphabet[(value >> 6U) & 0x3fU] : '=');
        encoded.push_back(remaining > 2 ? alphabet[value & 0x3fU] : '=');
    }
    return encoded;
}

std::string BuildResourceChunkLine(const aribtlv::ApplicationResource& resource,
                                   const std::uint64_t resource_id,
                                   const std::size_t sequence,
                                   const std::size_t total,
                                   const std::size_t offset,
                                   const std::size_t size) {
    std::ostringstream output;
    output << "{\"type\":\"resource_chunk\",\"contextId\":" << resource.context_id
           << ",\"path\":";
    WriteJSONString(output, resource.path);
    output << ",\"contentType\":";
    WriteJSONString(output, resource.content_type);
    output << ",\"resourceId\":" << resource_id
           << ",\"seq\":" << sequence
           << ",\"total\":" << total
           << ",\"data\":";
    const auto* chunk_data = size == 0 ? nullptr : resource.data.data() + offset;
    WriteJSONString(output, Base64Encode(chunk_data, size));
    output.put('}');
    return output.str();
}

}  // namespace

KonomiTVBS4KDatacast::KonomiTVBS4KDatacast(
    const bool enabled, std::ostream& output, std::ostream& error)
    : enabled_(enabled), output_(output), error_(error) {}

void KonomiTVBS4KDatacast::onApplicationState(const aribtlv::ApplicationState& state) {
    if (!enabled_ || suspended_until_source_reset_) return;

    const auto& application = state.application;
    applications_[ApplicationKey{
        application.context_id,
        application.application_type,
        application.organization_id,
        application.application_id,
    }] = state;

    // entry resource が揃うまでは差分を外へ出さず、最初の出力を必ず完全 snapshot にする。
    if (!snapshot_emitted_) {
        if (hasReadyApplication()) emitSnapshot();
        return;
    }
    emitApplicationState(state);
}

void KonomiTVBS4KDatacast::onApplicationResource(aribtlv::ApplicationResource&& resource) {
    if (!enabled_ || suspended_until_source_reset_) return;

    // 受信済み全resourceを保持するため、追加前にcontext上限と全体上限の両方を検査する。
    if (!canStoreResource(resource)) {
        suspended_until_source_reset_ = true;
        resetState("resource_limit_exceeded", true);
        return;
    }
    if (!canFrameResource(resource, next_resource_id_)) {
        suspended_until_source_reset_ = true;
        resetState("resource_framing_limit_exceeded", true);
        return;
    }

    const ResourceKey key{resource.context_id, resource.path};
    const auto existing = resources_.find(key);
    if (existing != resources_.end()) {
        total_resource_bytes_ -= existing->second.resource.data.size();
    }
    total_resource_bytes_ += resource.data.size();
    auto [stored, inserted] = resources_.insert_or_assign(
        key,
        StoredResource{std::move(resource), next_resource_id_++}
    );
    static_cast<void>(inserted);

    // 初回 snapshot より前のresourceはmapへ蓄積し、entry ready時にまとめて出力する。
    if (snapshot_emitted_) emitResource(stored->second);
}

void KonomiTVBS4KDatacast::onApplicationResourceRemoved(
    const aribtlv::ApplicationResourceRemoval& removal) {
    if (!enabled_ || suspended_until_source_reset_) return;

    const auto found = resources_.find(ResourceKey{removal.context_id, removal.path});
    if (found != resources_.end()) {
        total_resource_bytes_ -= found->second.resource.data.size();
        resources_.erase(found);
    }
    if (!snapshot_emitted_) return;

    std::ostringstream output;
    output << "{\"type\":\"application_resource_removed\",\"contextId\":"
           << removal.context_id << ",\"path\":";
    WriteJSONString(output, removal.path);
    output.put('}');
    writeLine(output.str());
}

void KonomiTVBS4KDatacast::onApplicationResourcesReset() {
    if (!enabled_) return;
    suspended_until_source_reset_ = false;
    resetState("source_reset", false);
}

void KonomiTVBS4KDatacast::onBroadcastClock(const aribtlv::BroadcastClock& clock) {
    if (!enabled_ || suspended_until_source_reset_) return;
    broadcast_clock_ = clock;
    if (snapshot_emitted_) emitBroadcastClock(clock);
}

void KonomiTVBS4KDatacast::onLayoutConfiguration(
    const aribtlv::LayoutConfiguration& layout) {
    if (!enabled_ || suspended_until_source_reset_) return;
    layout_configuration_ = layout;
    if (snapshot_emitted_) emitLayoutConfiguration(layout);
}

void KonomiTVBS4KDatacast::onEventInfo(const aribtlv::EventInfo& event) {
    if (!enabled_ || suspended_until_source_reset_) return;
    events_[EventKey{event.context_id, event.service_id, event.event_id, event.table_id}] = event;
    if (snapshot_emitted_) emitEventInfo(event);
}

void KonomiTVBS4KDatacast::onStreamEvent(const aribtlv::StreamEvent& event) {
    if (!enabled_ || suspended_until_source_reset_ || !snapshot_emitted_) return;
    emitStreamEvent(event);
}

void KonomiTVBS4KDatacast::onViewerParticipationNotification(
    const aribtlv::ViewerParticipationNotification& notification) {
    if (!enabled_ || suspended_until_source_reset_ || !snapshot_emitted_) return;
    emitViewerParticipationNotification(notification);
}

void KonomiTVBS4KDatacast::onError(const aribtlv::Error& error) {
    if (!enabled_ || suspended_until_source_reset_ ||
        error.code != aribtlv::ErrorCode::ResourceLimit ||
        error.message.find("application resource") == std::string::npos) {
        return;
    }
    error_ << "KonomiTV-BS4K datacast parser resource limit: " << error.message << '\n';
    suspended_until_source_reset_ = true;
    resetState("parser_resource_limit_exceeded", false);
}

void KonomiTVBS4KDatacast::emitCurrentSnapshot() {
    if (!enabled_ || suspended_until_source_reset_ || !hasReadyApplication()) return;
    emitSnapshot();
}

bool KonomiTVBS4KDatacast::canStoreResource(
    const aribtlv::ApplicationResource& resource) const {
    const ResourceKey key{resource.context_id, resource.path};
    const auto existing = resources_.find(key);
    const auto replaced_bytes = existing == resources_.end()
        ? 0
        : existing->second.resource.data.size();
    const auto context_bytes = contextResourceBytes(resource.context_id) - replaced_bytes;
    const auto total_bytes = total_resource_bytes_ - replaced_bytes;
    if (context_bytes > MAX_CONTEXT_RESOURCE_BYTES || total_bytes > MAX_TOTAL_RESOURCE_BYTES) {
        return false;
    }
    return resource.data.size() <= MAX_CONTEXT_RESOURCE_BYTES - context_bytes &&
        resource.data.size() <= MAX_TOTAL_RESOURCE_BYTES - total_bytes;
}

bool KonomiTVBS4KDatacast::canFrameResource(
    const aribtlv::ApplicationResource& resource, const std::uint64_t resource_id) const {
    const auto total = std::max<std::size_t>(
        1,
        (resource.data.size() + RESOURCE_CHUNK_BYTES - 1) / RESOURCE_CHUNK_BYTES
    );
    const auto sample_size = std::min(resource.data.size(), RESOURCE_CHUNK_BYTES);
    const auto line = BuildResourceChunkLine(
        resource,
        resource_id,
        total - 1,
        total,
        0,
        sample_size
    );
    return line.size() + 1 <= MAX_JSON_LINE_BYTES;
}

std::size_t KonomiTVBS4KDatacast::contextResourceBytes(
    const std::uint32_t context_id) const {
    std::size_t bytes = 0;
    for (const auto& [key, stored] : resources_) {
        if (key.first == context_id) bytes += stored.resource.data.size();
    }
    return bytes;
}

bool KonomiTVBS4KDatacast::hasReadyApplication() const {
    return std::any_of(applications_.begin(), applications_.end(), [](const auto& item) {
        return item.second.entry_ready;
    });
}

void KonomiTVBS4KDatacast::emitSnapshot() {
    writeLine("{\"type\":\"snapshot_begin\"}");
    for (const auto& [key, state] : applications_) {
        static_cast<void>(key);
        emitApplicationState(state);
    }
    for (const auto& [key, resource] : resources_) {
        static_cast<void>(key);
        emitResource(resource);
    }
    if (broadcast_clock_.has_value()) emitBroadcastClock(*broadcast_clock_);
    if (layout_configuration_.has_value()) emitLayoutConfiguration(*layout_configuration_);
    for (const auto& [key, event] : events_) {
        static_cast<void>(key);
        emitEventInfo(event);
    }
    writeLine("{\"type\":\"snapshot_end\"}");
    snapshot_emitted_ = true;
}

void KonomiTVBS4KDatacast::emitApplicationState(
    const aribtlv::ApplicationState& state) {
    const auto& application = state.application;
    std::ostringstream output;
    output << "{\"type\":\"application_state\",\"contextId\":"
           << application.context_id
           << ",\"applicationType\":" << application.application_type
           << ",\"organizationId\":" << application.organization_id
           << ",\"applicationId\":" << application.application_id
           << ",\"controlCode\":" << static_cast<unsigned int>(application.control_code)
           << ",\"applicationDescriptorPresent\":"
           << (application.application_descriptor_present ? "true" : "false")
           << ",\"serviceBound\":" << (application.service_bound ? "true" : "false")
           << ",\"visibility\":" << static_cast<unsigned int>(application.visibility)
           << ",\"presentApplicationPriority\":"
           << (application.present_application_priority ? "true" : "false")
           << ",\"applicationPriority\":"
           << static_cast<unsigned int>(application.application_priority)
           << ",\"entryPath\":";
    WriteJSONString(output, application.entry_path);
    output << ",\"transportUrls\":[";
    for (std::size_t index = 0; index < application.transport_urls.size(); ++index) {
        if (index > 0) output.put(',');
        WriteJSONString(output, application.transport_urls[index]);
    }
    output << "],\"entryReady\":" << (state.entry_ready ? "true" : "false") << '}';
    writeLine(output.str());
}

void KonomiTVBS4KDatacast::emitResource(const StoredResource& stored) {
    const auto& resource = stored.resource;
    const auto total = std::max<std::size_t>(
        1,
        (resource.data.size() + RESOURCE_CHUNK_BYTES - 1) / RESOURCE_CHUNK_BYTES
    );
    for (std::size_t sequence = 0; sequence < total; ++sequence) {
        const auto offset = sequence * RESOURCE_CHUNK_BYTES;
        const auto size = std::min(resource.data.size() - std::min(offset, resource.data.size()),
                                   RESOURCE_CHUNK_BYTES);
        writeLine(BuildResourceChunkLine(
            resource,
            stored.resource_id,
            sequence,
            total,
            offset,
            size
        ));
    }
}

void KonomiTVBS4KDatacast::emitBroadcastClock(const aribtlv::BroadcastClock& clock) {
    std::ostringstream output;
    output << "{\"type\":\"broadcast_clock\",\"mediaTimeValue\":\""
           << clock.media_time.value
           << "\",\"mediaTimeTimescale\":" << clock.media_time.timescale
           << ",\"broadcastTimeValue\":\"" << clock.broadcast_time.value
           << "\",\"broadcastTimeTimescale\":" << clock.broadcast_time.timescale << '}';
    writeLine(output.str());
}

void KonomiTVBS4KDatacast::emitLayoutConfiguration(
    const aribtlv::LayoutConfiguration& layout) {
    std::ostringstream output;
    output << "{\"type\":\"layout_configuration\",\"contextId\":" << layout.context_id
           << ",\"sourcePacketId\":" << layout.source_packet_id
           << ",\"version\":" << static_cast<unsigned int>(layout.version)
           << ",\"backgroundColorRgb\":";
    WriteOptionalInteger(output, layout.background_color_rgb);
    output << ",\"devices\":[";
    for (std::size_t device_index = 0; device_index < layout.devices.size(); ++device_index) {
        if (device_index > 0) output.put(',');
        const auto& device = layout.devices[device_index];
        output << "{\"layoutNumber\":" << static_cast<unsigned int>(device.layout_number)
               << ",\"deviceId\":" << static_cast<unsigned int>(device.device_id)
               << ",\"regions\":[";
        for (std::size_t region_index = 0; region_index < device.regions.size(); ++region_index) {
            if (region_index > 0) output.put(',');
            const auto& region = device.regions[region_index];
            output << "{\"regionNumber\":" << static_cast<unsigned int>(region.region_number)
                   << ",\"leftTopPosX\":" << static_cast<unsigned int>(region.left_top_pos_x)
                   << ",\"leftTopPosY\":" << static_cast<unsigned int>(region.left_top_pos_y)
                   << ",\"rightDownPosX\":" << static_cast<unsigned int>(region.right_down_pos_x)
                   << ",\"rightDownPosY\":" << static_cast<unsigned int>(region.right_down_pos_y)
                   << ",\"layerOrder\":" << static_cast<unsigned int>(region.layer_order)
                   << '}';
        }
        output << "]}";
    }
    output << "]}";
    writeLine(output.str());
}

void KonomiTVBS4KDatacast::emitEventInfo(const aribtlv::EventInfo& event) {
    std::ostringstream output;
    output << "{\"type\":\"event_info\",\"contextId\":" << event.context_id
           << ",\"sourcePacketId\":" << event.source_packet_id
           << ",\"tableId\":" << static_cast<unsigned int>(event.table_id)
           << ",\"version\":" << static_cast<unsigned int>(event.version)
           << ",\"currentNext\":" << (event.current_next ? "true" : "false")
           << ",\"sectionNumber\":" << static_cast<unsigned int>(event.section_number)
           << ",\"lastSectionNumber\":" << static_cast<unsigned int>(event.last_section_number)
           << ",\"serviceId\":" << event.service_id
           << ",\"tlvStreamId\":" << event.tlv_stream_id
           << ",\"originalNetworkId\":" << event.original_network_id
           << ",\"eventId\":" << event.event_id
           << ",\"startTimeUnixMilliseconds\":";
    WriteOptionalInteger(output, event.start_time_unix_milliseconds);
    output << ",\"durationSeconds\":";
    WriteOptionalInteger(output, event.duration_seconds);
    output << ",\"runningStatus\":" << static_cast<unsigned int>(event.running_status)
           << ",\"freeCaMode\":" << (event.free_ca_mode ? "true" : "false")
           << ",\"language\":";
    WriteJSONString(output, event.language);
    output << ",\"title\":";
    WriteJSONString(output, event.title);
    output << ",\"description\":";
    WriteJSONString(output, event.description);
    output << ",\"extendedDescription\":";
    WriteJSONString(output, event.extended_description);
    output.put('}');
    writeLine(output.str());
}

void KonomiTVBS4KDatacast::emitStreamEvent(const aribtlv::StreamEvent& event) {
    std::ostringstream output;
    output << "{\"type\":\"stream_event\",\"contextId\":" << event.context_id
           << ",\"eventMessageTag\":" << static_cast<unsigned int>(event.event_message_tag)
           << ",\"messageGroupId\":" << event.message_group_id
           << ",\"messageVersion\":" << static_cast<unsigned int>(event.message_version)
           << ",\"currentNext\":" << (event.current_next ? "true" : "false")
           << ",\"timeMode\":" << static_cast<unsigned int>(event.time_mode)
           << ",\"messageId\":" << static_cast<unsigned int>(event.message_id)
           << ",\"privateData\":";
    WriteJSONString(output, Base64Encode(event.private_data.data(), event.private_data.size()));
    output.put('}');
    writeLine(output.str());
}

void KonomiTVBS4KDatacast::emitViewerParticipationNotification(
    const aribtlv::ViewerParticipationNotification& notification) {
    std::ostringstream output;
    output << "{\"type\":\"viewer_participation\",\"contextId\":"
           << notification.context_id
           << ",\"sourcePacketId\":" << notification.source_packet_id
           << ",\"eventMessageTag\":"
           << static_cast<unsigned int>(notification.event_message_tag)
           << ",\"dataEventId\":" << static_cast<unsigned int>(notification.data_event_id)
           << ",\"messageGroupId\":" << notification.message_group_id
           << ",\"version\":" << static_cast<unsigned int>(notification.version)
           << ",\"currentNext\":" << (notification.current_next ? "true" : "false")
           << ",\"sectionNumber\":" << static_cast<unsigned int>(notification.section_number)
           << ",\"lastSectionNumber\":"
           << static_cast<unsigned int>(notification.last_section_number)
           << ",\"inputOffset\":\"" << notification.input_offset << "\"}";
    writeLine(output.str());
}

void KonomiTVBS4KDatacast::emitReset(const std::string_view reason) {
    std::ostringstream output;
    output << "{\"type\":\"application_resources_reset\",\"reason\":";
    WriteJSONString(output, reason);
    output.put('}');
    writeLine(output.str());
}

void KonomiTVBS4KDatacast::resetState(
    const std::string_view reason, const bool report_error) {
    if (report_error) {
        error_ << "KonomiTV-BS4K datacast state reset: " << reason << '\n';
    }
    applications_.clear();
    resources_.clear();
    events_.clear();
    broadcast_clock_.reset();
    layout_configuration_.reset();
    total_resource_bytes_ = 0;
    snapshot_emitted_ = false;
    emitReset(reason);
}

void KonomiTVBS4KDatacast::writeLine(std::string line) {
    if (line.size() + 1 > MAX_JSON_LINE_BYTES) {
        error_ << "KonomiTV-BS4K datacast JSON line exceeded 32 KiB and was dropped.\n";
        return;
    }
    output_ << line << '\n';
    output_.flush();
}
