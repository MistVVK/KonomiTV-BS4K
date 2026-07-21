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
from app.routers import ProgramsRouter
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


class _FakeChannelQuery:

    def __init__(self, channels: list[Channel] | None = None, first_channel: Channel | None = None) -> None:
        self.channels = channels or []
        self.first_channel = first_channel

    def __await__(self):
        async def Resolve() -> list[Channel]:
            return self.channels
        return Resolve().__await__()

    async def first(self) -> Channel | None:
        return self.first_channel


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
