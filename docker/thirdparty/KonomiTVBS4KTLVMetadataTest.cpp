#include <iostream>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

#define main KonomiTVBS4KTLVMetadataProductionMain
#include "KonomiTVBS4KTLVMetadata.cpp"
#undef main

namespace {

bool ExpectEqual(const std::string& actual, const std::string& expected, const char* test_name) {
    if (actual == expected) return true;
    std::cerr << test_name << " failed.\nExpected:\n" << expected << "Actual:\n" << actual;
    return false;
}

bool ExpectContains(const std::string& actual, const std::string& expected, const char* test_name) {
    if (actual.find(expected) != std::string::npos) return true;
    std::cerr << test_name << " failed.\nMissing:\n" << expected << "\nActual:\n" << actual;
    return false;
}

bool ExpectNotContains(const std::string& actual, const std::string& unexpected,
                       const char* test_name) {
    if (actual.find(unexpected) == std::string::npos) return true;
    std::cerr << test_name << " failed.\nUnexpected:\n" << unexpected << "\nActual:\n" << actual;
    return false;
}

bool TestServiceStateResetEmitsEmptyMptWithNullContext() {
    MetadataSink sink(true);

    std::ostringstream output;
    auto* original_output_buffer = std::cout.rdbuf(output.rdbuf());
    sink.onServiceStateReset(aribtlv::ServiceStateReset{
        std::nullopt,
        aribtlv::ServiceStateResetReason::FullReset,
    });
    std::cout.rdbuf(original_output_buffer);

    const std::string expected =
        "{\"snapshot_type\":\"MPT\",\"snapshot_context_id\":null,\"snapshot_version\":null,"
        "\"input_offset\":0,\"services\":[],\"tracks\":[],\"program_hints\":[]}\n";
    return ExpectEqual(output.str(), expected, "service state reset snapshot");
}

aribtlv::MhSdtSnapshot BuildServiceSnapshot(const std::uint16_t service_id,
                                            const std::string& service_name,
                                            const std::uint64_t input_offset) {
    aribtlv::ServiceDescriptionInfo service;
    service.service_id = service_id;
    service.service_type = 0x01;
    service.service_name = service_name;

    aribtlv::MhSdtSnapshot snapshot;
    snapshot.context_id = 1;
    snapshot.tlv_stream_id = 4;
    snapshot.original_network_id = 0x000b;
    snapshot.current_next = true;
    snapshot.input_offset = input_offset;
    snapshot.services.push_back(std::move(service));
    return snapshot;
}

bool TestRecordingSinkRetainsServicesAcrossProbeWindows() {
    MetadataSink sink;
    sink.onMhSdtSnapshot(BuildServiceSnapshot(161, "BS-TBS 4K", 100));
    sink.onMhSdtSnapshot(BuildServiceSnapshot(221, "QVC", 200));

    std::ostringstream output;
    sink.writeJSON(output);
    return ExpectContains(output.str(), "\"service_id\":161", "recording first service") &&
        ExpectContains(output.str(), "\"service_id\":221", "recording second service");
}

bool TestStreamingSinkKeepsOnlyCurrentServiceSnapshot() {
    MetadataSink sink(true);

    std::ostringstream output;
    auto* original_output_buffer = std::cout.rdbuf(output.rdbuf());
    sink.onMhSdtSnapshot(BuildServiceSnapshot(161, "BS-TBS 4K", 100));
    sink.onMhSdtSnapshot(BuildServiceSnapshot(221, "QVC", 200));
    std::cout.rdbuf(original_output_buffer);

    // 2 行目は後から届いた完全 snapshot なので、先のサービスを含めない。
    const auto first_line_end = output.str().find('\n');
    const auto second_line = output.str().substr(first_line_end + 1);
    return ExpectContains(second_line, "\"service_id\":221", "streaming current service") &&
        ExpectNotContains(second_line, "\"service_id\":161", "streaming stale service");
}

bool TestEventVariantsFromDifferentTablesAreRetained() {
    MetadataSink sink;
    aribtlv::EventInfo present;
    present.context_id = 1;
    present.service_id = 221;
    present.event_id = 5720;
    present.table_id = 0x8b;
    present.title = "QVC programme";
    present.input_offset = 100;

    auto schedule = present;
    schedule.table_id = 0x94;
    schedule.title.clear();
    schedule.input_offset = 200;
    sink.onEventInfo(present);
    sink.onEventInfo(schedule);

    std::ostringstream output;
    sink.writeJSON(output);
    return ExpectContains(output.str(), "\"table_id\":139", "present event table") &&
        ExpectContains(output.str(), "\"table_id\":148", "schedule event table") &&
        ExpectContains(output.str(), "\"title\":\"QVC programme\"", "present event title");
}

aribtlv::ApplicationState BuildDatacastApplicationState(const bool entry_ready) {
    aribtlv::ApplicationState state;
    state.application.context_id = 7;
    state.application.application_type = 0x11;
    state.application.organization_id = 16;
    state.application.application_id = 1;
    state.application.control_code = 0x01;
    state.application.entry_path = "startup/html/index.html";
    state.application.transport_urls = {"sh4/40/001/"};
    state.entry_ready = entry_ready;
    state.resource_count = entry_ready ? 1 : 0;
    return state;
}

aribtlv::ApplicationResource BuildDatacastResource(const std::size_t size,
                                                    const std::uint8_t value) {
    aribtlv::ApplicationResource resource;
    resource.context_id = 7;
    resource.path = "sh4/40/001/startup/html/index.html";
    resource.content_type = "text/html";
    resource.data.assign(size, value);
    return resource;
}

bool TestDatacastSnapshotAndDiffFraming() {
    std::ostringstream output;
    std::ostringstream error;
    KonomiTVBS4KDatacast datacast(true, output, error);

    // entryが揃う前は出力せず、resourceとready stateが揃った時点を完全snapshotの境界にする。
    datacast.onApplicationState(BuildDatacastApplicationState(false));
    datacast.onApplicationResource(BuildDatacastResource(20 * 1024, 'a'));
    // 1つのMH-EIT sectionから複数eventが通知されても、完全snapshotでは別々に保持する。
    aribtlv::EventInfo first_event;
    first_event.context_id = 7;
    first_event.service_id = 101;
    first_event.table_id = 0x8c;
    first_event.section_number = 120;
    first_event.event_id = 16317;
    datacast.onEventInfo(first_event);
    auto second_event = first_event;
    second_event.event_id = 17969;
    datacast.onEventInfo(second_event);
    if (!output.str().empty()) {
        std::cerr << "datacast emitted a partial initial state\n";
        return false;
    }
    datacast.onApplicationState(BuildDatacastApplicationState(true));

    const auto snapshot = output.str();
    bool lines_fit = true;
    std::size_t line_start = 0;
    while (line_start < snapshot.size()) {
        const auto line_end = snapshot.find('\n', line_start);
        const auto line_size = (line_end == std::string::npos ? snapshot.size() : line_end) - line_start + 1;
        if (line_size > KonomiTVBS4KDatacast::MAX_JSON_LINE_BYTES) lines_fit = false;
        if (line_end == std::string::npos) break;
        line_start = line_end + 1;
    }
    if (!lines_fit ||
        !ExpectContains(snapshot, "{\"type\":\"snapshot_begin\"}\n", "datacast snapshot begin") ||
        !ExpectContains(snapshot, "\"type\":\"application_state\"", "datacast application state") ||
        !ExpectContains(snapshot, "\"seq\":0,\"total\":2", "datacast first resource chunk") ||
        !ExpectContains(snapshot, "\"seq\":1,\"total\":2", "datacast final resource chunk") ||
        !ExpectContains(snapshot, "\"eventId\":16317", "datacast first same-section event") ||
        !ExpectContains(snapshot, "\"eventId\":17969", "datacast second same-section event") ||
        !ExpectContains(snapshot, "{\"type\":\"snapshot_end\"}\n", "datacast snapshot end")) {
        return false;
    }

    // 初回snapshot後は更新、削除、resetを差分行として即時出力する。
    output.str("");
    output.clear();
    datacast.onApplicationResource(BuildDatacastResource(3, 'b'));
    datacast.onApplicationResourceRemoved(aribtlv::ApplicationResourceRemoval{
        7, 0, 0, 0, 0, 0, "sh4/40/001/startup/html/index.html",
    });
    datacast.onApplicationResourcesReset();
    const auto diff = output.str();
    return ExpectContains(diff, "\"type\":\"resource_chunk\"", "datacast update") &&
        ExpectContains(diff, "\"type\":\"application_resource_removed\"", "datacast removal") &&
        ExpectContains(diff, "\"type\":\"application_resources_reset\"", "datacast reset") &&
        error.str().empty();
}

bool TestDatacastResourceLimitResetsState() {
    std::ostringstream output;
    std::ostringstream error;
    KonomiTVBS4KDatacast datacast(true, output, error);

    datacast.onApplicationResource(BuildDatacastResource(
        KonomiTVBS4KDatacast::MAX_CONTEXT_RESOURCE_BYTES + 1,
        'x'
    ));
    const auto reset_emitted = ExpectContains(
        output.str(),
        "\"type\":\"application_resources_reset\",\"reason\":\"resource_limit_exceeded\"",
        "datacast resource limit reset"
    );
    const auto limit_logged = ExpectContains(
        error.str(),
        "KonomiTV-BS4K datacast state reset: resource_limit_exceeded",
        "datacast resource limit log"
    );
    const auto oversized_resource_dropped = ExpectNotContains(
        output.str(),
        "\"type\":\"resource_chunk\"",
        "datacast oversized resource"
    );
    if (!reset_emitted || !limit_logged || !oversized_resource_dropped) {
        return false;
    }

    // assembler 側には既存resourceが残るため、正規source resetまでは不完全な再snapshotを出さない。
    output.str("");
    output.clear();
    datacast.onApplicationResource(BuildDatacastResource(3, 'y'));
    datacast.onApplicationState(BuildDatacastApplicationState(true));
    if (!ExpectEqual(output.str(), "", "datacast suspended after resource limit")) {
        return false;
    }

    datacast.onApplicationResourcesReset();
    output.str("");
    output.clear();
    error.str("");
    error.clear();
    datacast.onError(aribtlv::Error{
        aribtlv::ErrorCode::ResourceLimit,
        100,
        true,
        "application resource exceeds size limit",
    });
    return ExpectContains(
        output.str(),
        "\"reason\":\"parser_resource_limit_exceeded\"",
        "datacast parser resource limit reset"
    ) && ExpectContains(
        error.str(),
        "KonomiTV-BS4K datacast parser resource limit: application resource exceeds size limit",
        "datacast parser resource limit log"
    );
}

bool TestDatacastTotalResourceLimitResetsState() {
    std::ostringstream output;
    std::ostringstream error;
    KonomiTVBS4KDatacast datacast(true, output, error);

    // 各contextは8MiB以内でも、全contextのraw resource合計が16MiBを超えたら全状態を破棄する。
    auto first = BuildDatacastResource(KonomiTVBS4KDatacast::MAX_CONTEXT_RESOURCE_BYTES, 'a');
    first.context_id = 1;
    datacast.onApplicationResource(std::move(first));
    auto second = BuildDatacastResource(KonomiTVBS4KDatacast::MAX_CONTEXT_RESOURCE_BYTES, 'b');
    second.context_id = 2;
    datacast.onApplicationResource(std::move(second));
    auto excess = BuildDatacastResource(1, 'c');
    excess.context_id = 3;
    datacast.onApplicationResource(std::move(excess));

    return ExpectContains(
        output.str(),
        "\"reason\":\"resource_limit_exceeded\"",
        "datacast total resource limit reset"
    ) && ExpectContains(
        error.str(),
        "KonomiTV-BS4K datacast state reset: resource_limit_exceeded",
        "datacast total resource limit log"
    );
}

}  // namespace

int main() {
    return TestServiceStateResetEmitsEmptyMptWithNullContext() &&
        TestRecordingSinkRetainsServicesAcrossProbeWindows() &&
        TestStreamingSinkKeepsOnlyCurrentServiceSnapshot() &&
        TestEventVariantsFromDifferentTablesAreRetained() &&
        TestDatacastSnapshotAndDiffFraming() &&
        TestDatacastResourceLimitResetsState() &&
        TestDatacastTotalResourceLimitResetsState() ? 0 : 1;
}
