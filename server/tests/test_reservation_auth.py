"""本線の予約 API 認証境界と、KomorebiV1 互換 API の無認証 allowlist を検証する。"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Iterator

from fastapi.routing import APIRoute
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient

from app import config as config_module
from app.CompatibilityAPI import CreateCompatibilityAPI
from app.routers import ReservationConditionsRouter, ReservationsRouter
from app.routers.UsersRouter import GetCurrentUser


# app.app は通常 KonomiTV.py で初期化済みの設定を参照するため、単体テストでは安全な既定値を先に設定する。
if config_module._CONFIG is None:
    config_module._CONFIG = config_module.HostServerSettings().toServerSettings(bypass_validation=True)
app_module = importlib.import_module('app.app')

# テストプロセス終了時の atexit fallback が実サービス用 cleanup を起動しないよう、既定状態を完了済みにする。
app_module._shutdown_completed = True
main_app = app_module.app

_MAIN_RESERVATION_PATH_PREFIXES = (
    '/api/recording/reservations',
    '/api/recording/conditions',
)


class _RegularUser:
    """一般ユーザー相当の最小スタブ。"""

    id = 7
    name = 'regular-user'
    is_admin = False


class _EmptyCtrlCmdUtil:
    """EDCB 通信を行わず、DB なしで空一覧レスポンスへ落とす最小スタブ。"""

    async def sendEnumReserve(self):
        # ReservationsAPI は None を空一覧として返す（DB アクセス前）
        return None

    async def sendEnumAutoAdd(self):
        # ReservationConditionsAPI は None を空一覧として返す（DB アクセス前）
        return None

    async def sendEnumRecInfo(self, _reserve_id: int | None = None):
        return None

    async def sendFileCopy(self, _filename: str):
        return None

    async def sendDelReserve(self, _reserve_ids: list[int]):
        return False

    async def sendGetRecFilePath(self, _reserve_id: int):
        return None


async def _GetRegularUser() -> _RegularUser:
    return _RegularUser()


async def _GetEmptyCtrlCmdUtil() -> _EmptyCtrlCmdUtil:
    return _EmptyCtrlCmdUtil()


def _IterMainReservationRoutes() -> Iterator[APIRoute]:
    """本線アプリに登録された予約・自動予約 APIRoute を列挙する。"""

    for route in main_app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.path.startswith(_MAIN_RESERVATION_PATH_PREFIXES):
            continue
        yield route


def _RouteDependsOnGetCurrentUser(route: APIRoute) -> bool:
    """ルート依存に GetCurrentUser が含まれるかを返す。"""

    for dependency in route.dependencies:
        if dependency.dependency is GetCurrentUser:
            return True
    return False


def _ClearMainAppOverrides() -> None:
    """本線アプリへ付けた dependency_overrides を必ず外す。"""

    main_app.dependency_overrides.pop(GetCurrentUser, None)
    main_app.dependency_overrides.pop(ReservationsRouter.GetCtrlCmdUtil, None)
    main_app.dependency_overrides.pop(ReservationConditionsRouter.GetCtrlCmdUtil, None)


def test_main_app_reservation_routes_depend_on_get_current_user() -> None:
    """本番 app.py が本線予約ルートへ GetCurrentUser を付与していることを構造検証する。"""

    routes = list(_IterMainReservationRoutes())
    assert routes, '本線アプリに予約 API ルートが見つからない'
    missing_paths = sorted({
        f'{method} {route.path}'
        for route in routes
        if not _RouteDependsOnGetCurrentUser(route)
        for method in sorted(route.methods or set())
    })
    assert missing_paths == [], f'GetCurrentUser 未付与の本線予約ルート: {missing_paths}'


def test_main_reservation_apis_require_authentication() -> None:
    """本番本線アプリの予約・自動予約 API は未認証で 401 になる。"""

    _ClearMainAppOverrides()
    reservation_record_settings = {
        'is_enabled': True,
        'priority': 2,
        'recording_folders': [],
        'recording_start_margin': None,
        'recording_end_margin': None,
        'recording_mode': 'SpecifiedService',
        'caption_recording_mode': 'Default',
        'data_broadcasting_recording_mode': 'Default',
        'post_recording_mode': 'Default',
        'post_recording_bat_file_path': None,
        'is_event_relay_follow_enabled': True,
        'is_exact_recording_enabled': False,
        'is_oneseg_separate_output_enabled': False,
        'is_sequential_recording_in_single_file_enabled': False,
        'forced_tuner_id': None,
    }

    async def GetResponses():
        async with HTTPXAsyncClient(transport=ASGITransport(app=main_app), base_url='http://test') as client:
            return (
                await client.get('/api/recording/reservations'),
                await client.post('/api/recording/reservations', json={
                    'program_id': 'NID1-SID2-EID3',
                    'record_settings': reservation_record_settings,
                }),
                await client.put('/api/recording/reservations/1', json={
                    'record_settings': reservation_record_settings,
                }),
                await client.delete('/api/recording/reservations/1'),
                await client.get('/api/recording/conditions'),
                await client.get('/api/recording/conditions/1'),
                await client.post('/api/recording/conditions', json={
                    'program_search_conditions': {
                        'keyword': 'test',
                    },
                    'record_settings': reservation_record_settings,
                }),
                await client.put('/api/recording/conditions/1', json={
                    'program_search_conditions': {
                        'keyword': 'test',
                    },
                    'record_settings': reservation_record_settings,
                }),
                await client.delete('/api/recording/conditions/1'),
            )

    try:
        responses = asyncio.run(GetResponses())
    finally:
        _ClearMainAppOverrides()
    assert [response.status_code for response in responses] == [401] * len(responses)


def test_main_reservation_apis_accept_regular_authenticated_user() -> None:
    """本番本線アプリの予約 API は一般ユーザーの認証で到達できる。"""

    _ClearMainAppOverrides()
    main_app.dependency_overrides[GetCurrentUser] = _GetRegularUser
    main_app.dependency_overrides[ReservationsRouter.GetCtrlCmdUtil] = _GetEmptyCtrlCmdUtil
    main_app.dependency_overrides[ReservationConditionsRouter.GetCtrlCmdUtil] = _GetEmptyCtrlCmdUtil

    async def GetResponses():
        async with HTTPXAsyncClient(transport=ASGITransport(app=main_app), base_url='http://test') as client:
            reservations = await client.get('/api/recording/reservations')
            conditions = await client.get('/api/recording/conditions')
            return reservations, conditions

    try:
        reservations, conditions = asyncio.run(GetResponses())
    finally:
        _ClearMainAppOverrides()

    assert reservations.status_code == 200
    assert reservations.json() == {'total': 0, 'reservations': []}
    assert conditions.status_code == 200
    assert conditions.json() == {'total': 0, 'reservation_conditions': []}


def test_source_reservation_routers_remain_unauthenticated_for_compatibility_reuse() -> None:
    """source router 自体には認証依存がなく、互換 API が無認証のまま利用できる。"""

    assert list(ReservationsRouter.router.dependencies) == []
    assert list(ReservationConditionsRouter.router.dependencies) == []

    for route in (*ReservationsRouter.router.routes, *ReservationConditionsRouter.router.routes):
        assert isinstance(route, APIRoute)
        assert list(route.dependencies) == []

    app = CreateCompatibilityAPI()
    app.dependency_overrides[ReservationsRouter.GetCtrlCmdUtil] = _GetEmptyCtrlCmdUtil
    app.dependency_overrides[ReservationConditionsRouter.GetCtrlCmdUtil] = _GetEmptyCtrlCmdUtil

    async def GetResponses():
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            return (
                await client.get('/api/recording/reservations'),
                await client.get('/api/recording/conditions'),
                await client.delete('/api/recording/reservations/1'),
            )

    reservations, conditions, delete_reservation = asyncio.run(GetResponses())
    assert reservations.status_code == 200
    assert reservations.json() == {'total': 0, 'reservations': []}
    assert conditions.status_code == 200
    assert conditions.json() == {'total': 0, 'reservation_conditions': []}
    # 未認証のまま依存解決まで進むこと（401 にならないこと）が本テストの要点
    assert delete_reservation.status_code != 401
