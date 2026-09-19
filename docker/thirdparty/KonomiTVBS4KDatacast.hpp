#pragma once

#include <aribtlv/application_resources.hpp>
#include <aribtlv/types.hpp>

#include <cstddef>
#include <cstdint>
#include <iosfwd>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <tuple>
#include <utility>

class KonomiTVBS4KDatacast final {
public:
    static constexpr std::size_t MAX_CONTEXT_RESOURCE_BYTES = 8 * 1024 * 1024;
    static constexpr std::size_t MAX_TOTAL_RESOURCE_BYTES = 16 * 1024 * 1024;
    static constexpr std::size_t RESOURCE_CHUNK_BYTES = 16 * 1024;
    static constexpr std::size_t MAX_JSON_LINE_BYTES = 32 * 1024;

    explicit KonomiTVBS4KDatacast(bool enabled, std::ostream& output, std::ostream& error);

    void onApplicationState(const aribtlv::ApplicationState& state);
    void onApplicationResource(aribtlv::ApplicationResource&& resource);
    void onApplicationResourceRemoved(const aribtlv::ApplicationResourceRemoval& removal);
    void onApplicationResourcesReset();
    void onBroadcastClock(const aribtlv::BroadcastClock& clock);
    void onLayoutConfiguration(const aribtlv::LayoutConfiguration& layout);
    void onEventInfo(const aribtlv::EventInfo& event);
    void onStreamEvent(const aribtlv::StreamEvent& event);
    void onViewerParticipationNotification(
        const aribtlv::ViewerParticipationNotification& notification);
    void onError(const aribtlv::Error& error);
    void emitCurrentSnapshot();

private:
    using ApplicationKey =
        std::tuple<std::uint32_t, std::uint16_t, std::uint16_t, std::uint32_t>;
    using ResourceKey = std::pair<std::uint32_t, std::string>;
    using EventKey = std::tuple<std::uint32_t, std::uint8_t, std::uint8_t>;

    struct StoredResource {
        aribtlv::ApplicationResource resource;
        std::uint64_t resource_id;
    };

    [[nodiscard]] bool canStoreResource(const aribtlv::ApplicationResource& resource) const;
    [[nodiscard]] bool canFrameResource(const aribtlv::ApplicationResource& resource,
                                        std::uint64_t resource_id) const;
    [[nodiscard]] std::size_t contextResourceBytes(std::uint32_t context_id) const;
    [[nodiscard]] bool hasReadyApplication() const;
    void emitSnapshot();
    void emitApplicationState(const aribtlv::ApplicationState& state);
    void emitResource(const StoredResource& resource);
    void emitBroadcastClock(const aribtlv::BroadcastClock& clock);
    void emitLayoutConfiguration(const aribtlv::LayoutConfiguration& layout);
    void emitEventInfo(const aribtlv::EventInfo& event);
    void emitStreamEvent(const aribtlv::StreamEvent& event);
    void emitViewerParticipationNotification(
        const aribtlv::ViewerParticipationNotification& notification);
    void emitReset(std::string_view reason);
    void resetState(std::string_view reason, bool report_error);
    void writeLine(std::string line);

    bool enabled_;
    std::ostream& output_;
    std::ostream& error_;
    std::map<ApplicationKey, aribtlv::ApplicationState> applications_;
    std::map<ResourceKey, StoredResource> resources_;
    std::map<EventKey, aribtlv::EventInfo> events_;
    std::optional<aribtlv::BroadcastClock> broadcast_clock_;
    std::optional<aribtlv::LayoutConfiguration> layout_configuration_;
    std::size_t total_resource_bytes_ = 0;
    std::uint64_t next_resource_id_ = 1;
    bool snapshot_emitted_ = false;
    bool suspended_until_source_reset_ = false;
};
