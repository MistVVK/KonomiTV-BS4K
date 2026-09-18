#include <iostream>
#include <optional>
#include <sstream>
#include <string>

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

}  // namespace

int main() {
    return TestServiceStateResetEmitsEmptyMptWithNullContext() &&
        TestRecordingSinkRetainsServicesAcrossProbeWindows() &&
        TestStreamingSinkKeepsOnlyCurrentServiceSnapshot() &&
        TestEventVariantsFromDifferentTablesAreRetained() ? 0 : 1;
}
