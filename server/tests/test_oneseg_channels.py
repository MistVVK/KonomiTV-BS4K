import asyncio
import importlib
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app import schemas
from app.constants import JST
from app.models.Channel import Channel
from app.models.Program import Program
from app.routers import ChannelsRouter, ProgramsRouter
from app.utils.TSInformation import TSInformation


MX_NETWORK_ID = 32391
MX_FULLSEG_SERVICE_ID = 23608
MX_ONESEG_SERVICE_ID = 23992
MX_ADDITIONAL_ONESEG_SERVICE_ID = 23993
MX_DATA_SERVICE_ID = 23994
MX_INCONSISTENT_SERVICE_ID = 23995


def test_oneseg_service_classifier_rejects_non_oneseg_data_services() -> None:
    assert TSInformation.isOneSegService(MX_NETWORK_ID, 0xC0, MX_ONESEG_SERVICE_ID) is True
    assert TSInformation.isOneSegService(MX_NETWORK_ID, 0xC0, MX_ONESEG_SERVICE_ID, partial_reception=True) is True
    assert TSInformation.isOneSegService(MX_NETWORK_ID, 0xC0, MX_ONESEG_SERVICE_ID, partial_reception=False) is False

    # 地上波 G ガイドは type=0xC0 でも SID のサービス種別ビットがワンセグ型ではない
    assert TSInformation.isOneSegService(MX_NETWORK_ID, 0xC0, 1183) is False
    # BS データサービスは SID のビットが一致しても地上波ネットワークではない
    assert TSInformation.isOneSegService(4, 0xC0, 0x0180) is False
    # 通常のデジタル TV サービスは SID が一致しても type=0xC0 ではない
    assert TSInformation.isOneSegService(MX_NETWORK_ID, 0x01, MX_ONESEG_SERVICE_ID) is False


def test_oneseg_channel_keeps_gr_identity_and_uses_own_subchannel_number() -> None:
    assert TSInformation.isOneSegChannel('GR', MX_ONESEG_SERVICE_ID) is True
    assert TSInformation.calculateOneSegParentServiceID(MX_ONESEG_SERVICE_ID) == MX_FULLSEG_SERVICE_ID
    assert TSInformation.calculateIsSubchannel('GR', MX_ONESEG_SERVICE_ID) is False
    assert TSInformation.calculateIsSubchannel('GR', MX_ONESEG_SERVICE_ID + 1) is True


def test_channel_schema_exposes_derived_is_oneseg_without_database_column() -> None:
    channel = Channel(
        id=f'NID{MX_NETWORK_ID}-SID{MX_ONESEG_SERVICE_ID:03d}',
        display_channel_id='gr094',
        network_id=MX_NETWORK_ID,
        service_id=MX_ONESEG_SERVICE_ID,
        transport_stream_id=MX_NETWORK_ID,
        remocon_id=9,
        channel_number='094',
        type='GR',
        name='MXワンセグ1',
        jikkyo_force=None,
        is_subchannel=False,
        is_radiochannel=False,
        is_watchable=True,
    )

    assert channel.is_oneseg is True
    assert schemas.Channel.model_validate(channel).is_oneseg is True


def test_oneseg_current_program_fallback_contains_display_fields_only() -> None:
    parent_present_start = datetime(2026, 7, 21, 23, 50, tzinfo=JST)
    parent_following_start = parent_present_start + timedelta(minutes=45)
    program_rows = [
        {
            'network_id': MX_NETWORK_ID,
            'service_id': MX_FULLSEG_SERVICE_ID,
            'title': '親フルセグの現在番組',
            'description': '表示用に利用する番組概要',
            'start_time': parent_present_start.isoformat(),
            'end_time': parent_following_start.isoformat(),
            'duration': 45 * 60,
            'is_present': 1,
        },
        {
            'network_id': MX_NETWORK_ID,
            'service_id': MX_FULLSEG_SERVICE_ID,
            'title': '重複した親フルセグの現在番組',
            'description': '既存の親局表示と同じく採用しない',
            'start_time': (parent_present_start + timedelta(minutes=5)).isoformat(),
            'end_time': parent_following_start.isoformat(),
            'duration': 40 * 60,
            'is_present': 1,
        },
        {
            'network_id': MX_NETWORK_ID,
            'service_id': MX_FULLSEG_SERVICE_ID,
            'title': '親フルセグの次番組',
            'description': '次番組は利用しない',
            'start_time': parent_following_start.isoformat(),
            'end_time': (parent_following_start + timedelta(minutes=30)).isoformat(),
            'duration': 30 * 60,
            'is_present': 0,
        },
    ]
    current_program_display_map = ChannelsRouter.BuildCurrentProgramDisplayMap(program_rows)
    oneseg_channel = Channel(
        id=f'NID{MX_NETWORK_ID}-SID{MX_ONESEG_SERVICE_ID:03d}',
        display_channel_id='gr094',
        network_id=MX_NETWORK_ID,
        service_id=MX_ONESEG_SERVICE_ID,
        transport_stream_id=MX_NETWORK_ID,
        remocon_id=9,
        channel_number='094',
        type='GR',
        name='MXワンセグ1',
        jikkyo_force=None,
        is_subchannel=False,
        is_radiochannel=False,
        is_watchable=True,
    )

    fallback = ChannelsRouter.GetOneSegProgramPresentFallback(
        oneseg_channel,
        None,
        current_program_display_map,
        {(MX_NETWORK_ID, MX_FULLSEG_SERVICE_ID), (MX_NETWORK_ID, MX_ONESEG_SERVICE_ID)},
    )

    assert fallback == {
        'title': '親フルセグの現在番組',
        'description': '表示用に利用する番組概要',
        'start_time': parent_present_start.isoformat(),
        'end_time': parent_following_start.isoformat(),
        'duration': 45 * 60,
    }
    validated_fallback = schemas.LiveProgramPresentFallback.model_validate(fallback)
    assert set(validated_fallback.model_dump()) == {
        'title',
        'description',
        'start_time',
        'end_time',
        'duration',
    }
    # 番組表や通常番組のスキーマには表示専用フィールドを追加しない
    assert 'program_present_fallback' not in schemas.Program.model_fields
    assert 'program_present_fallback' not in schemas.TimeTableProgram.model_fields


def test_oneseg_current_program_fallback_never_overrides_native_or_uses_unwatchable_parent() -> None:
    oneseg_channel = Channel(
        id=f'NID{MX_NETWORK_ID}-SID{MX_ONESEG_SERVICE_ID:03d}',
        display_channel_id='gr094',
        network_id=MX_NETWORK_ID,
        service_id=MX_ONESEG_SERVICE_ID,
        transport_stream_id=MX_NETWORK_ID,
        remocon_id=9,
        channel_number='094',
        type='GR',
        name='MXワンセグ1',
        jikkyo_force=None,
        is_subchannel=False,
        is_radiochannel=False,
        is_watchable=True,
    )
    parent_program = {
        'title': '親フルセグ番組',
        'description': '親番組概要',
        'start_time': '2026-07-21T23:50:00+09:00',
        'end_time': '2026-07-22T00:35:00+09:00',
        'duration': 2700.0,
    }
    current_program_display_map = {(MX_NETWORK_ID, MX_FULLSEG_SERVICE_ID): parent_program}

    # ワンセグ自身の現在番組がある場合は親番組で上書きしない
    assert ChannelsRouter.GetOneSegProgramPresentFallback(
        oneseg_channel,
        {'title': 'ワンセグ自身の現在番組'},
        current_program_display_map,
        {(MX_NETWORK_ID, MX_FULLSEG_SERVICE_ID)},
    ) is None

    # 親フルセグ局が視聴可能チャンネルでなければ、残存DBの番組を表示に流用しない
    assert ChannelsRouter.GetOneSegProgramPresentFallback(
        oneseg_channel,
        None,
        current_program_display_map,
        {(MX_NETWORK_ID, MX_ONESEG_SERVICE_ID)},
    ) is None

    # 同じ親SIDでも別ネットワークの番組は利用しない
    assert ChannelsRouter.GetOneSegProgramPresentFallback(
        oneseg_channel,
        None,
        {(MX_NETWORK_ID + 1, MX_FULLSEG_SERVICE_ID): parent_program},
        {(MX_NETWORK_ID, MX_FULLSEG_SERVICE_ID)},
    ) is None


class _FakeChannelQuery:

    def __init__(self, channels: list[Channel] | None = None, first_channel: Channel | None = None) -> None:
        self.channels = channels or []
        self.first_channel = first_channel

    def __await__(self):
        async def Resolve() -> list[Channel]:
            return self.channels
        return Resolve().__await__()

    def order_by(self, *_fields: str) -> '_FakeChannelQuery':
        return self

    async def first(self) -> Channel | None:
        return self.first_channel


class _FakeChannelsAPIConnection:

    def __init__(self, program_rows: list[dict[str, Any]]) -> None:
        self.program_rows = program_rows

    async def execute_query_dict(
        self,
        query: str,
        values: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        assert 'DENSE_RANK()' in query
        assert values is not None
        return [dict(program_row) for program_row in self.program_rows]


def _build_channels_api_program_row(
    channel: Channel,
    event_id: int,
    title: str,
    description: str,
    start_time: datetime,
    end_time: datetime,
    is_present: bool,
    program_order: int,
) -> dict[str, Any]:
    return {
        'program_order': program_order,
        'is_present': int(is_present),
        'id': f'NID{channel.network_id}-SID{channel.service_id:03d}-EID{event_id}',
        'channel_id': channel.id,
        'network_id': channel.network_id,
        'service_id': channel.service_id,
        'event_id': event_id,
        'title': title,
        'description': description,
        'detail': '{}',
        'start_time': start_time.isoformat(),
        'end_time': end_time.isoformat(),
        'duration': (end_time - start_time).total_seconds(),
        'is_free': 1,
        'genres': '[]',
        'video_type': '1080i',
        'video_codec': 'MPEG-2',
        'video_resolution': '1080i',
        'primary_audio_type': '2/0モード（ステレオ）',
        'primary_audio_language': '日本語',
        'primary_audio_sampling_rate': '48kHz',
        'secondary_audio_type': None,
        'secondary_audio_language': None,
        'secondary_audio_sampling_rate': None,
    }


@pytest.mark.parametrize(
    ('parent_service_id', 'oneseg_service_id', 'is_subchannel', 'expected_is_display'),
    [
        (MX_FULLSEG_SERVICE_ID, MX_ONESEG_SERVICE_ID, False, True),
        (MX_FULLSEG_SERVICE_ID + 1, MX_ADDITIONAL_ONESEG_SERVICE_ID, True, False),
    ],
)
def test_channels_api_adds_oneseg_current_program_as_display_only_fallback(
    monkeypatch: pytest.MonkeyPatch,
    parent_service_id: int,
    oneseg_service_id: int,
    is_subchannel: bool,
    expected_is_display: bool,
) -> None:
    parent_channel = Channel(
        id=f'NID{MX_NETWORK_ID}-SID{parent_service_id:03d}',
        display_channel_id='gr091' if is_subchannel is False else 'gr092',
        network_id=MX_NETWORK_ID,
        service_id=parent_service_id,
        transport_stream_id=MX_NETWORK_ID,
        remocon_id=9,
        channel_number='091' if is_subchannel is False else '092',
        type='GR',
        name='TOKYO MX1' if is_subchannel is False else 'TOKYO MX2',
        jikkyo_force=None,
        is_subchannel=False,
        is_radiochannel=False,
        is_watchable=True,
    )
    oneseg_channel = Channel(
        id=f'NID{MX_NETWORK_ID}-SID{oneseg_service_id:03d}',
        display_channel_id='gr094' if is_subchannel is False else 'gr095',
        network_id=MX_NETWORK_ID,
        service_id=oneseg_service_id,
        transport_stream_id=MX_NETWORK_ID,
        remocon_id=9,
        channel_number='094' if is_subchannel is False else '095',
        type='GR',
        name='MXワンセグ1' if is_subchannel is False else 'MXワンセグ2',
        jikkyo_force=None,
        is_subchannel=is_subchannel,
        is_radiochannel=False,
        is_watchable=True,
    )
    parent_present_start = datetime.now(JST) - timedelta(minutes=10)
    parent_present_end = parent_present_start + timedelta(minutes=45)
    parent_program_row = _build_channels_api_program_row(
        parent_channel,
        100,
        '親フルセグの現在番組',
        'ライブ画面用の番組概要',
        parent_present_start,
        parent_present_end,
        True,
        1,
    )
    connection = _FakeChannelsAPIConnection([parent_program_row])

    monkeypatch.setattr(ChannelsRouter.connections, 'get', lambda _: connection)
    monkeypatch.setattr(
        Channel,
        'filter',
        classmethod(lambda cls, *args, **kwargs: _FakeChannelQuery(channels=[parent_channel, oneseg_channel])),
    )
    monkeypatch.setattr(ChannelsRouter.LiveStream, 'getViewerCount', lambda _: 0)

    result = asyncio.run(ChannelsRouter.ChannelsAPI())
    oneseg_result = next(channel for channel in result.GR if channel.is_oneseg is True)

    # 実番組と次番組は欠けたままで、ライブ表示専用フィールドだけに親番組を載せる
    assert oneseg_result.program_present is None
    assert oneseg_result.program_following is None
    assert oneseg_result.program_present_fallback is not None
    assert oneseg_result.program_present_fallback.model_dump() == {
        'title': '親フルセグの現在番組',
        'description': 'ライブ画面用の番組概要',
        'start_time': parent_present_start,
        'end_time': parent_present_end,
        'duration': 45 * 60,
    }
    # フォールバックによって従来の表示・選局対象判定を変更しない
    assert oneseg_result.is_display is expected_is_display


def test_channels_api_keeps_native_oneseg_present_and_following_programs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_channel = Channel(
        id=f'NID{MX_NETWORK_ID}-SID{MX_FULLSEG_SERVICE_ID:03d}',
        display_channel_id='gr091',
        network_id=MX_NETWORK_ID,
        service_id=MX_FULLSEG_SERVICE_ID,
        transport_stream_id=MX_NETWORK_ID,
        remocon_id=9,
        channel_number='091',
        type='GR',
        name='TOKYO MX1',
        jikkyo_force=None,
        is_subchannel=False,
        is_radiochannel=False,
        is_watchable=True,
    )
    oneseg_channel = Channel(
        id=f'NID{MX_NETWORK_ID}-SID{MX_ONESEG_SERVICE_ID:03d}',
        display_channel_id='gr094',
        network_id=MX_NETWORK_ID,
        service_id=MX_ONESEG_SERVICE_ID,
        transport_stream_id=MX_NETWORK_ID,
        remocon_id=9,
        channel_number='094',
        type='GR',
        name='MXワンセグ1',
        jikkyo_force=None,
        is_subchannel=False,
        is_radiochannel=False,
        is_watchable=True,
    )
    present_start = datetime.now(JST) - timedelta(minutes=10)
    present_end = present_start + timedelta(minutes=45)
    following_end = present_end + timedelta(minutes=30)
    program_rows = [
        _build_channels_api_program_row(
            parent_channel,
            100,
            '親フルセグの現在番組',
            '利用してはいけない親番組',
            present_start,
            present_end,
            True,
            1,
        ),
        _build_channels_api_program_row(
            oneseg_channel,
            200,
            'ワンセグ自身の現在番組',
            '優先するワンセグ番組',
            present_start,
            present_end,
            True,
            1,
        ),
        _build_channels_api_program_row(
            oneseg_channel,
            201,
            'ワンセグ自身の次番組',
            '変更しないワンセグ次番組',
            present_end,
            following_end,
            False,
            2,
        ),
    ]
    connection = _FakeChannelsAPIConnection(program_rows)

    monkeypatch.setattr(ChannelsRouter.connections, 'get', lambda _: connection)
    monkeypatch.setattr(
        Channel,
        'filter',
        classmethod(lambda cls, *args, **kwargs: _FakeChannelQuery(channels=[parent_channel, oneseg_channel])),
    )
    monkeypatch.setattr(ChannelsRouter.LiveStream, 'getViewerCount', lambda _: 0)

    result = asyncio.run(ChannelsRouter.ChannelsAPI())
    oneseg_result = next(channel for channel in result.GR if channel.is_oneseg is True)

    assert oneseg_result.program_present is not None
    assert oneseg_result.program_present.title == 'ワンセグ自身の現在番組'
    assert oneseg_result.program_following is not None
    assert oneseg_result.program_following.title == 'ワンセグ自身の次番組'
    assert oneseg_result.program_present_fallback is None


class _FakeTransactionContext:

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return False


def test_channel_updates_support_edcb_mirakurun_and_hybrid_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    channel_module = importlib.import_module('app.models.Channel')
    server_settings = SimpleNamespace(
        general=SimpleNamespace(always_receive_tv_from_mirakurun=True),
        tv=SimpleNamespace(preferred_terrestrial_region=None),
    )

    chset5_lines = [
        f'MX1\tMX\t{MX_NETWORK_ID}\t{MX_NETWORK_ID}\t{MX_FULLSEG_SERVICE_ID}\t1\t0\t1\t1\t9',
        f'MX2\tMX\t{MX_NETWORK_ID}\t{MX_NETWORK_ID}\t{MX_FULLSEG_SERVICE_ID + 1}\t1\t0\t1\t1\t9',
        f'MX3\tMX\t{MX_NETWORK_ID}\t{MX_NETWORK_ID}\t{MX_FULLSEG_SERVICE_ID + 2}\t1\t0\t1\t1\t9',
        f'MXOneSeg1\tMX\t{MX_NETWORK_ID}\t{MX_NETWORK_ID}\t{MX_ONESEG_SERVICE_ID}\t192\t1\t1\t1\t0',
        f'MXOneSeg2\tMX\t{MX_NETWORK_ID}\t{MX_NETWORK_ID}\t{MX_ADDITIONAL_ONESEG_SERVICE_ID}\t192\t1\t1\t1\t0',
        # type=0xC0 でも partial_flag=False のサービスは EDCB のワンセグとして扱わない
        f'MXData\tMX\t{MX_NETWORK_ID}\t{MX_NETWORK_ID}\t{MX_DATA_SERVICE_ID}\t192\t0\t1\t1\t9',
        # SID はワンセグ型だが service_type=0x01 の不整合サービスも登録しない
        f'MXInvalid\tMX\t{MX_NETWORK_ID}\t{MX_NETWORK_ID}\t{MX_INCONSISTENT_SERVICE_ID}\t1\t1\t1\t1\t9',
    ]

    class FakeCtrlCmdUtil:

        def setConnectTimeOutSec(self, timeout: float) -> None:
            pass

        async def sendFileCopy(self, file_name: str) -> bytes:
            return '\n'.join(chset5_lines).encode()

        async def sendEnumService(self) -> list[dict[str, Any]]:
            return []

    mirakurun_services: list[dict[str, Any]] = [
        {
            'networkId': MX_NETWORK_ID,
            'serviceId': MX_FULLSEG_SERVICE_ID + index,
            'type': 0x01,
            'name': f'MX{index + 1}',
            'remoteControlKeyId': 9,
        }
        for index in range(3)
    ]
    mirakurun_services.append({
        'networkId': MX_NETWORK_ID,
        'serviceId': MX_ONESEG_SERVICE_ID,
        'type': 0xC0,
        'name': 'MXOneSeg1',
    })
    mirakurun_services.append({
        'networkId': MX_NETWORK_ID,
        'serviceId': MX_INCONSISTENT_SERVICE_ID,
        'type': 0x01,
        'name': 'MXInvalid',
        'remoteControlKeyId': 9,
    })

    async def FetchMirakurunServices(cls: type[Channel]) -> list[dict[str, Any]]:
        return [dict(service) for service in mirakurun_services]

    # 録画メタデータから先に登録された gr094 を再利用し、別レコードを作らないことも確認する
    recorded_oneseg = Channel(
        id=f'NID{MX_NETWORK_ID}-SID{MX_ONESEG_SERVICE_ID:03d}',
        display_channel_id='gr094',
        network_id=MX_NETWORK_ID,
        service_id=MX_ONESEG_SERVICE_ID,
        transport_stream_id=MX_NETWORK_ID,
        remocon_id=9,
        channel_number='094',
        type='GR',
        name='MXOneSeg1',
        jikkyo_force=None,
        is_subchannel=False,
        is_radiochannel=False,
        is_watchable=False,
    )
    saved_channels: dict[int, dict[str, Any]] = {}
    saved_channel_instances: dict[int, Channel] = {}

    async def SaveChannel(channel: Channel) -> None:
        saved_channel_instances[channel.service_id] = channel
        saved_channels[channel.service_id] = {
            'display_channel_id': channel.display_channel_id,
            'is_watchable': channel.is_watchable,
            'is_subchannel': channel.is_subchannel,
            'is_oneseg': channel.is_oneseg,
        }

    async def RecalculateRecordingOnlyChannelBranchNumbers(
        cls: type[Channel],
        same_network_id_counts: dict[int, int],
        same_remocon_id_counts: dict[int, int],
    ) -> None:
        return None

    def FilterChannels(cls: type[Channel], *args: object, **kwargs: object) -> _FakeChannelQuery:
        if kwargs.get('id') == recorded_oneseg.id and kwargs.get('is_watchable') is False:
            return _FakeChannelQuery(first_channel=recorded_oneseg)
        return _FakeChannelQuery()

    monkeypatch.setattr(channel_module, 'Config', lambda: server_settings)
    monkeypatch.setattr(channel_module, 'CtrlCmdUtil', FakeCtrlCmdUtil)
    monkeypatch.setattr(channel_module.transactions, 'in_transaction', lambda: _FakeTransactionContext())
    monkeypatch.setattr(Channel, 'fetchMirakurunServices', classmethod(FetchMirakurunServices))
    monkeypatch.setattr(Channel, 'filter', classmethod(FilterChannels))
    monkeypatch.setattr(Channel, 'all', classmethod(lambda cls: _FakeChannelQuery(channels=[recorded_oneseg])))
    monkeypatch.setattr(Channel, 'save', SaveChannel)
    monkeypatch.setattr(
        Channel,
        'recalculateRecordingOnlyChannelBranchNumbers',
        classmethod(RecalculateRecordingOnlyChannelBranchNumbers),
    )

    async def RunUpdates() -> None:
        # EDCB メタデータ + Mirakurun 映像では、両方にある正確な NID-SID のワンセグだけを登録する
        await Channel.updateFromEDCB()
        assert saved_channels[MX_ONESEG_SERVICE_ID] == {
            'display_channel_id': 'gr094',
            'is_watchable': True,
            'is_subchannel': False,
            'is_oneseg': True,
        }
        assert saved_channel_instances[MX_ONESEG_SERVICE_ID] is recorded_oneseg
        assert MX_ADDITIONAL_ONESEG_SERVICE_ID not in saved_channels
        assert MX_DATA_SERVICE_ID not in saved_channels
        assert MX_INCONSISTENT_SERVICE_ID not in saved_channels

        # EDCB 完結では、partial_flag=True の EDCB 専用追加ワンセグも登録する
        saved_channels.clear()
        server_settings.general.always_receive_tv_from_mirakurun = False
        await Channel.updateFromEDCB()
        assert saved_channels[MX_ADDITIONAL_ONESEG_SERVICE_ID] == {
            'display_channel_id': 'gr095',
            'is_watchable': True,
            'is_subchannel': True,
            'is_oneseg': True,
        }
        assert MX_DATA_SERVICE_ID not in saved_channels
        assert MX_INCONSISTENT_SERVICE_ID not in saved_channels

        # Mirakurun 完結でも同じ GR ID・チャンネル番号で登録する
        saved_channels.clear()
        mirakurun_services.append({
            'networkId': MX_NETWORK_ID,
            'serviceId': MX_ADDITIONAL_ONESEG_SERVICE_ID,
            'type': 0xC0,
            'name': 'MXOneSeg2',
        })
        await Channel.updateFromMirakurun()
        assert saved_channels[MX_ONESEG_SERVICE_ID]['display_channel_id'] == 'gr094'
        assert saved_channels[MX_ADDITIONAL_ONESEG_SERVICE_ID] == {
            'display_channel_id': 'gr095',
            'is_watchable': True,
            'is_subchannel': True,
            'is_oneseg': True,
        }
        assert MX_INCONSISTENT_SERVICE_ID not in saved_channels

    asyncio.run(RunUpdates())


def test_hybrid_inventory_failure_happens_before_channel_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    channel_module = importlib.import_module('app.models.Channel')
    server_settings = SimpleNamespace(
        general=SimpleNamespace(always_receive_tv_from_mirakurun=True),
        tv=SimpleNamespace(preferred_terrestrial_region=None),
    )

    async def FetchMirakurunServices(cls: type[Channel]) -> list[dict[str, Any]]:
        raise RuntimeError('Mirakurun inventory unavailable')

    def FailOnDatabaseAccess(cls: type[Channel], *args: object, **kwargs: object) -> _FakeChannelQuery:
        raise AssertionError('Channel state must not be read or mutated after inventory failure')

    monkeypatch.setattr(channel_module, 'Config', lambda: server_settings)
    monkeypatch.setattr(channel_module.transactions, 'in_transaction', lambda: _FakeTransactionContext())
    monkeypatch.setattr(Channel, 'fetchMirakurunServices', classmethod(FetchMirakurunServices))
    monkeypatch.setattr(Channel, 'filter', classmethod(FailOnDatabaseAccess))

    with pytest.raises(RuntimeError, match='Mirakurun inventory unavailable'):
        asyncio.run(Channel.updateFromEDCB())


def test_mirakurun_program_update_saves_oneseg_epg_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    program_module = importlib.import_module('app.models.Program')
    channel = Channel(
        id=f'NID{MX_NETWORK_ID}-SID{MX_ONESEG_SERVICE_ID:03d}',
        display_channel_id='gr094',
        network_id=MX_NETWORK_ID,
        service_id=MX_ONESEG_SERVICE_ID,
        transport_stream_id=MX_NETWORK_ID,
        remocon_id=9,
        channel_number='094',
        type='GR',
        name='MXワンセグ1',
        jikkyo_force=None,
        is_subchannel=False,
        is_radiochannel=False,
        is_watchable=True,
    )
    fixed_start_time = datetime(2099, 1, 1, 12, 0, tzinfo=JST)
    program_fixture = {
        'networkId': MX_NETWORK_ID,
        'serviceId': MX_ONESEG_SERVICE_ID,
        'eventId': 1234,
        'name': 'ワンセグ番組',
        'description': 'Mirakurun EPG fixture',
        'startAt': int(fixed_start_time.timestamp() * 1000),
        'duration': 30 * 60 * 1000,
        'isFree': True,
        'audios': [{
            'componentType': 0x03,
            'langs': ['jpn'],
            'samplingRate': 48000,
        }],
    }

    class FakeMirakurunResponse:
        status_code = 200

        def json(self) -> list[dict[str, Any]]:
            return [program_fixture]

    class FakeHTTPXClient:

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> bool:
            return False

        async def get(self, url: str, timeout: float) -> FakeMirakurunResponse:
            assert url == 'http://mirakurun.test/api/programs'
            assert timeout == 10
            return FakeMirakurunResponse()

    saved_programs: list[Program] = []

    async def SaveProgram(program: Program) -> None:
        saved_programs.append(program)

    monkeypatch.setattr(program_module.transactions, 'in_transaction', lambda: _FakeTransactionContext())
    monkeypatch.setattr(program_module, 'GetMirakurunAPIEndpointURL', lambda endpoint: f'http://mirakurun.test{endpoint}')
    monkeypatch.setattr(program_module, 'HTTPX_CLIENT', FakeHTTPXClient)
    monkeypatch.setattr(program_module.logging, 'debug', lambda message: None)
    monkeypatch.setattr(Program, 'all', classmethod(lambda cls: _FakeChannelQuery()))
    monkeypatch.setattr(Program, 'save', SaveProgram)
    monkeypatch.setattr(Channel, 'filter', classmethod(lambda cls, *args, **kwargs: _FakeChannelQuery(channels=[channel])))

    asyncio.run(Program.updateFromMirakurun())

    assert len(saved_programs) == 1
    saved_program = saved_programs[0]
    assert saved_program.id == f'NID{MX_NETWORK_ID}-SID{MX_ONESEG_SERVICE_ID:03d}-EID1234'
    assert saved_program.channel_id == channel.id
    assert saved_program.network_id == MX_NETWORK_ID
    assert saved_program.service_id == MX_ONESEG_SERVICE_ID
    assert saved_program.title == 'ワンセグ番組'
    assert saved_program.start_time == fixed_start_time
    assert saved_program.end_time == fixed_start_time + timedelta(minutes=30)


class _FakeTimeTableConnection:

    def __init__(self) -> None:
        self.program_rows: list[dict[str, Any]] = []
        self.channel_rows = [
            {
                'id': f'NID{MX_NETWORK_ID}-SID{MX_FULLSEG_SERVICE_ID:03d}',
                'display_channel_id': 'gr091',
                'network_id': MX_NETWORK_ID,
                'service_id': MX_FULLSEG_SERVICE_ID,
                'transport_stream_id': MX_NETWORK_ID,
                'remocon_id': 9,
                'channel_number': '091',
                'type': 'GR',
                'name': 'TOKYO MX1',
                'jikkyo_force': None,
                'is_subchannel': 0,
                'is_radiochannel': 0,
                'is_watchable': 1,
            },
            {
                'id': f'NID{MX_NETWORK_ID}-SID{MX_ONESEG_SERVICE_ID:03d}',
                'display_channel_id': 'gr094',
                'network_id': MX_NETWORK_ID,
                'service_id': MX_ONESEG_SERVICE_ID,
                'transport_stream_id': MX_NETWORK_ID,
                'remocon_id': 9,
                'channel_number': '094',
                'type': 'GR',
                'name': 'MXワンセグ1',
                'jikkyo_force': None,
                'is_subchannel': 0,
                'is_radiochannel': 0,
                'is_watchable': 1,
            },
        ]

    async def execute_query_dict(
        self,
        query: str,
        values: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        if 'SELECT MIN(start_time)' in query:
            return [{
                'earliest': '2026-07-21T00:00:00+09:00',
                'latest': '2026-07-22T00:00:00+09:00',
            }]
        if 'FROM channels' in query:
            return [dict(channel_row) for channel_row in self.channel_rows]
        if 'SELECT *' in query and 'FROM programs' in query:
            selected_channel_ids = {
                value for value in values or []
                if isinstance(value, str) and value.startswith('NID')
            }
            return [
                dict(program_row) for program_row in self.program_rows
                if program_row['channel_id'] in selected_channel_ids
            ]
        return []


@pytest.mark.parametrize(
    ('is_oneseg', 'expected_display_channel_id'),
    [
        (False, 'gr091'),
        (True, 'gr094'),
    ],
)
def test_timetable_api_filters_gr_and_oneseg_independently(
    monkeypatch: pytest.MonkeyPatch,
    is_oneseg: bool,
    expected_display_channel_id: str,
) -> None:
    connection = _FakeTimeTableConnection()
    monkeypatch.setattr(ProgramsRouter.connections, 'get', lambda _: connection)
    monkeypatch.setattr(
        ProgramsRouter,
        'Config',
        lambda: SimpleNamespace(general=SimpleNamespace(backend='Mirakurun')),
    )

    result = asyncio.run(ProgramsRouter.TimeTableAPI(
        start_time=datetime.fromisoformat('2026-07-21T00:00:00+09:00'),
        end_time=datetime.fromisoformat('2026-07-22T00:00:00+09:00'),
        channel_type='GR',
        pinned_channel_ids=None,
        is_oneseg=is_oneseg,
    ))

    assert [channel.channel.display_channel_id for channel in result.channels] == [expected_display_channel_id]
    assert result.channels[0].channel.is_oneseg is is_oneseg


def test_timetable_api_never_copies_parent_program_to_oneseg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeTimeTableConnection()
    parent_channel = Channel(**connection.channel_rows[0])
    program_start = datetime.fromisoformat('2026-07-21T12:00:00+09:00')
    parent_program_row = _build_channels_api_program_row(
        parent_channel,
        300,
        '番組表にある親フルセグ番組',
        'ワンセグ番組表にはコピーしない',
        program_start,
        program_start + timedelta(minutes=30),
        True,
        1,
    )
    parent_program_row.pop('is_present')
    parent_program_row.pop('program_order')
    connection.program_rows = [parent_program_row]

    monkeypatch.setattr(ProgramsRouter.connections, 'get', lambda _: connection)
    monkeypatch.setattr(
        ProgramsRouter,
        'Config',
        lambda: SimpleNamespace(general=SimpleNamespace(backend='Mirakurun')),
    )

    fullseg_result = asyncio.run(ProgramsRouter.TimeTableAPI(
        start_time=datetime.fromisoformat('2026-07-21T00:00:00+09:00'),
        end_time=datetime.fromisoformat('2026-07-22T00:00:00+09:00'),
        channel_type='GR',
        pinned_channel_ids=None,
        is_oneseg=False,
    ))
    oneseg_result = asyncio.run(ProgramsRouter.TimeTableAPI(
        start_time=datetime.fromisoformat('2026-07-21T00:00:00+09:00'),
        end_time=datetime.fromisoformat('2026-07-22T00:00:00+09:00'),
        channel_type='GR',
        pinned_channel_ids=None,
        is_oneseg=True,
    ))

    assert [program.title for program in fullseg_result.channels[0].programs] == ['番組表にある親フルセグ番組']
    assert oneseg_result.channels[0].programs == []


def test_timetable_api_pinned_channels_override_oneseg_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _FakeTimeTableConnection()
    monkeypatch.setattr(ProgramsRouter.connections, 'get', lambda _: connection)
    monkeypatch.setattr(
        ProgramsRouter,
        'Config',
        lambda: SimpleNamespace(general=SimpleNamespace(backend='Mirakurun')),
    )

    pinned_channel_ids = ','.join(channel_row['id'] for channel_row in connection.channel_rows)
    result = asyncio.run(ProgramsRouter.TimeTableAPI(
        start_time=datetime.fromisoformat('2026-07-21T00:00:00+09:00'),
        end_time=datetime.fromisoformat('2026-07-22T00:00:00+09:00'),
        channel_type='GR',
        pinned_channel_ids=pinned_channel_ids,
        is_oneseg=False,
    ))

    assert [channel.channel.display_channel_id for channel in result.channels] == ['gr091', 'gr094']
    assert [channel.channel.is_oneseg for channel in result.channels] == [False, True]
