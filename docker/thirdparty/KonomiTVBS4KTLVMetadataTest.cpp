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
        "\"input_offset\":0,\"services\":[],\"tracks\":[]}\n";
    return ExpectEqual(output.str(), expected, "service state reset snapshot");
}

}  // namespace

int main() {
    return TestServiceStateResetEmitsEmptyMptWithNullContext() ? 0 : 1;
}
