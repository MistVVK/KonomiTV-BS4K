from __future__ import annotations

import asyncio
import math
import secrets
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, cast
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response, status
from jose import JWTError, jwt
from starlette.requests import ClientDisconnect

from app import logging
from app.constants import JST, JWT_SECRET_KEY, QUALITY, QUALITY_TYPES
from app.streams.KonomiTVBS4KPlaybackEncoding import (
    KonomiTVBS4KVideoCodec,
    ResolveKonomiTVBS4KPlaybackVideoBitrate,
)


KONOMITV_BS4K_SPEED_TEST_COOKIE_NAME = 'KonomiTV-BS4K-SpeedTest-Session'
KONOMITV_BS4K_SPEED_TEST_COOKIE_PATH = '/api/konomitv-bs4k/speed-test'
KONOMITV_BS4K_SPEED_TEST_JWT_TYPE = 'SpeedTestSession'
KONOMITV_BS4K_SPEED_TEST_JWT_ISSUER = 'KonomiTV Server'
KONOMITV_BS4K_SPEED_TEST_SESSION_TTL_SECONDS = 90
KONOMITV_BS4K_SPEED_TEST_MAX_SESSIONS_GLOBAL = 2
KONOMITV_BS4K_SPEED_TEST_MAX_DOWNLOAD_STREAMS = 5
KONOMITV_BS4K_SPEED_TEST_MAX_UPLOAD_STREAMS = 3
KONOMITV_BS4K_SPEED_TEST_MAX_PING_STREAMS = 1
KONOMITV_BS4K_SPEED_TEST_DOWNLOAD_SECONDS = 10
KONOMITV_BS4K_SPEED_TEST_UPLOAD_SECONDS = 10
KONOMITV_BS4K_SPEED_TEST_DOWNLOAD_GRACE_SECONDS = 1.5
KONOMITV_BS4K_SPEED_TEST_UPLOAD_GRACE_SECONDS = 3.0
KONOMITV_BS4K_SPEED_TEST_TRANSFER_WINDOW_MARGIN_SECONDS = 1.0
KONOMITV_BS4K_SPEED_TEST_DOWNLOAD_WINDOW_SECONDS = (
    KONOMITV_BS4K_SPEED_TEST_DOWNLOAD_SECONDS +
    KONOMITV_BS4K_SPEED_TEST_DOWNLOAD_GRACE_SECONDS +
    KONOMITV_BS4K_SPEED_TEST_TRANSFER_WINDOW_MARGIN_SECONDS
)
KONOMITV_BS4K_SPEED_TEST_UPLOAD_WINDOW_SECONDS = (
    KONOMITV_BS4K_SPEED_TEST_UPLOAD_SECONDS +
    KONOMITV_BS4K_SPEED_TEST_UPLOAD_GRACE_SECONDS +
    KONOMITV_BS4K_SPEED_TEST_TRANSFER_WINDOW_MARGIN_SECONDS
)
KONOMITV_BS4K_SPEED_TEST_DEFAULT_CK_SIZE = 4
KONOMITV_BS4K_SPEED_TEST_MAX_CK_SIZE = 100
KONOMITV_BS4K_SPEED_TEST_CHUNK_BYTES = 1024 * 1024
KONOMITV_BS4K_SPEED_TEST_MAX_UPLOAD_REQUEST_BYTES = 20 * 1024 * 1024
KONOMITV_BS4K_SPEED_TEST_MAX_SESSION_DOWNLOAD_BYTES = 16 * 1024 * 1024 * 1024
KONOMITV_BS4K_SPEED_TEST_MAX_SESSION_UPLOAD_BYTES = 16 * 1024 * 1024 * 1024
KONOMITV_BS4K_SPEED_TEST_DRAIN_TIMEOUT_SECONDS = 30.0
KONOMITV_BS4K_SPEED_TEST_VARIABLE_BITRATE_SAFETY_FACTOR = 1.30

KonomiTVBS4KSpeedTestBroadcastType = Literal['Terrestrial', 'BS4K']
KonomiTVBS4KSpeedTestCodec = Literal['AVC', 'HEVC', 'VP9', 'AV1']
KonomiTVBS4KSpeedTestBasis = Literal['VariableBitrate', 'FixedMuxrate']
KonomiTVBS4KSpeedTestTransferKind = Literal['download', 'upload', 'ping']

# クライアント SettingsStore の通常放送 / BS4K 画質集合と一致させる。
# 閾値生成はここだけを正本にし、bitrate 定義自体は再生関数へ委譲する。
KONOMITV_BS4K_SPEED_TEST_TERRESTRIAL_QUALITIES: tuple[QUALITY_TYPES, ...] = (
    '1080p-60fps',
    '1080p',
    '810p',
    '720p',
    '540p',
    '480p',
    '360p',
    '240p',
)
KONOMITV_BS4K_SPEED_TEST_BS4K_QUALITIES: tuple[QUALITY_TYPES, ...] = (
    '4320p',
    '2160p',
    '1440p',
    '1080p-60fps',
    '1080p-30fps',
    '810p-60fps',
    '810p-30fps',
    '720p-60fps',
    '720p-30fps',
    '540p-30fps',
    '480p-30fps',
    '360p-30fps',
    '240p-30fps',
)
KONOMITV_BS4K_SPEED_TEST_CODECS: tuple[tuple[KonomiTVBS4KSpeedTestCodec, KonomiTVBS4KVideoCodec], ...] = (
    ('AVC', 'avc'),
    ('HEVC', 'hevc'),
    ('VP9', 'vp9'),
    ('AV1', 'av1'),
)


@dataclass(slots=True)
class KonomiTVBS4KSpeedTestQualityThreshold:
    """session 応答へ載せる codec / 画質ごとの必要 Mbps。"""

    broadcast_type: KonomiTVBS4KSpeedTestBroadcastType
    codec: KonomiTVBS4KSpeedTestCodec
    quality: str
    required_mbps: float
    basis: KonomiTVBS4KSpeedTestBasis


@dataclass(frozen=True, slots=True)
class KonomiTVBS4KSpeedTestTransferLease:
    """1本の測定転送と、それを実行する ASGI タスクを対応付ける。"""

    lease_id: str
    session_id: str
    kind: KonomiTVBS4KSpeedTestTransferKind
    owner_task: asyncio.Task[None]


@dataclass(slots=True)
class KonomiTVBS4KSpeedTestSessionState:
    """プロセス内にだけ保持する測定枠。"""

    # 短命 JWT の sid と一致する測定枠 ID。releaseSession() と cookie 検証が参照する。
    session_id: str
    # 単調時刻での期限。壁時計の巻き戻しで枠が残らないようにする。
    expires_at_monotonic: float
    # 下り / 上り / ping の lease。同時数制限と、解放時に ASGI タスクを停止するために参照する。
    download_leases: dict[str, KonomiTVBS4KSpeedTestTransferLease]
    upload_leases: dict[str, KonomiTVBS4KSpeedTestTransferLease]
    ping_leases: dict[str, KonomiTVBS4KSpeedTestTransferLease]
    # session 寿命中の累積転送量。16GiB を超えたらその方向の転送を止める。
    downloaded_bytes: int
    uploaded_bytes: int
    # 期限 Task。DELETE と期限切れの二重解放を防ぐために cancel する。
    expiry_task: asyncio.Task[None] | None
    # 既に解放済みなら True。二重 DELETE と期限 Task の競合を無視する。
    released: bool
    # 最初の下り / 上りを開始した単調時刻。grace time 込み測定窓の起点。未開始は None。
    download_started_monotonic: float | None
    upload_started_monotonic: float | None
    # grace time 込みの方向別測定窓を閉じ、slow reader も停止する Task。
    download_window_task: asyncio.Task[None] | None
    upload_window_task: asyncio.Task[None] | None


class KonomiTVBS4KSpeedTestSessionManager:
    """測定枠の確保・同時転送制限・期限回収をプロセス内で管理する。"""

    def __init__(self) -> None:
        # 枠の作成・解放・転送開始を直列化する。await 中に別リクエストが枠を壊さない前提。
        self._lock = asyncio.Lock()
        # session_id から枠状態を引く。cookie 検証と転送 API が参照する。
        self._sessions: dict[str, KonomiTVBS4KSpeedTestSessionState] = {}

    async def resetForTests(self) -> None:
        """単体テスト間でプロセス内枠を空にする。"""

        async with self._lock:
            for session in list(self._sessions.values()):
                session.released = True
                self._cancelExpiryTask(session)
                self._cancelTransferWindowTasks(session)
                current_task = asyncio.current_task()
                for leases in (session.download_leases, session.upload_leases, session.ping_leases):
                    for lease in leases.values():
                        if lease.owner_task is not current_task and lease.owner_task.done() is False:
                            lease.owner_task.cancel()
            self._sessions.clear()

    async def createSession(self) -> KonomiTVBS4KSpeedTestSessionState:
        """
        全体枠に空きがあれば測定枠を作る。

        Returns:
            作成した測定枠。JWT と cookie の sid に使う。

        Raises:
            HTTPException: 全体の枠が埋まっている場合は 429。
        """

        async with self._lock:
            self._collectExpiredSessionsLocked()
            if len(self._sessions) >= KONOMITV_BS4K_SPEED_TEST_MAX_SESSIONS_GLOBAL:
                retry_after = min(
                    self._retryAfterSeconds(session)
                    for session in self._sessions.values()
                )
                logging.warning(
                    '[KonomiTVBS4KSpeedTest] Rejected session create because the global session limit is full. '
                    f'[active_sessions: {len(self._sessions)}]'
                )
                raise self._tooManyRequests(retry_after)

            session_id = uuid.uuid4().hex
            session = KonomiTVBS4KSpeedTestSessionState(
                session_id = session_id,
                expires_at_monotonic = time.monotonic() + KONOMITV_BS4K_SPEED_TEST_SESSION_TTL_SECONDS,
                download_leases = {},
                upload_leases = {},
                ping_leases = {},
                downloaded_bytes = 0,
                uploaded_bytes = 0,
                expiry_task = None,
                released = False,
                download_started_monotonic = None,
                upload_started_monotonic = None,
                download_window_task = None,
                upload_window_task = None,
            )
            self._sessions[session_id] = session
            session.expiry_task = asyncio.create_task(
                self._expireSession(session_id),
                name = f'konomitv-bs4k-speed-test-expire-{session_id}',
            )
            return session

    async def releaseSession(self, session_id: str) -> bool:
        """
        測定枠を解放する。DELETE と期限切れのどちらからでも安全に呼べる。

        Args:
            session_id: 解放する測定枠 ID。

        Returns:
            この呼び出しで実際に枠を消した場合は True。既に無い場合は False。
        """

        current_task = asyncio.current_task()
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.released:
                return False
            leases_to_join = [
                lease
                for leases in (session.download_leases, session.upload_leases, session.ping_leases)
                for lease in leases.values()
                if lease.owner_task is not current_task
            ]
            released = self._releaseSessionLocked(session_id)

        # DELETE の応答前にキャンセル完了を待ち、直後の再測定が古い in-flight 枠との競合で 429 にならないようにする。
        owner_tasks = {lease.owner_task for lease in leases_to_join}
        if owner_tasks:
            await asyncio.gather(*owner_tasks, return_exceptions=True)
        for lease in leases_to_join:
            await self._endTransfer(lease)
        return released

    async def getActiveSession(self, session_id: str) -> KonomiTVBS4KSpeedTestSessionState | None:
        """
        期限内の測定枠を返す。

        Args:
            session_id: cookie / JWT から取り出した測定枠 ID。

        Returns:
            有効な枠。期限切れか未登録なら None。
        """

        async with self._lock:
            self._collectExpiredSessionsLocked()
            session = self._sessions.get(session_id)
            if session is None or session.released:
                return None
            return session

    async def beginTransfer(
        self,
        session_id: str,
        kind: KonomiTVBS4KSpeedTestTransferKind,
    ) -> KonomiTVBS4KSpeedTestTransferLease:
        """
        転送枠を確保する。StreamingResponse 開始前に呼び、HTTP 状態を正しく返す。

        Args:
            session_id: 測定枠 ID。
            kind: 確保する転送の種類。

        Returns:
            確保した転送 lease。

        Raises:
            HTTPException: 枠が無い場合は 401、同時実行上限や測定時間超過なら 429。
        """

        _, lease = await self._beginTransfer(session_id, kind)
        return lease

    async def isDirectionCancelled(
        self,
        session_id: str,
        kind: Literal['download', 'upload'],
    ) -> bool:
        """
        指定方向の転送を今すぐ止めるべきか返す。

        Args:
            session_id: 測定枠 ID。
            kind: 下りまたは上り。

        Returns:
            解放済み、またはその方向の grace time 込み測定窓が切れていれば True。
        """

        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.released:
                return True
            return self._isTransferWindowExpired(session, kind)

    @asynccontextmanager
    async def acquireTransfer(
        self,
        session_id: str,
        kind: KonomiTVBS4KSpeedTestTransferKind,
    ) -> AsyncGenerator[KonomiTVBS4KSpeedTestSessionState, None]:
        """
        下り / 上り / ping の同時実行枠を確保し、終了時に返す。

        Args:
            session_id: 測定枠 ID。
            kind: 確保する転送の種類。

        Yields:
            確保した測定枠。

        Raises:
            HTTPException: 枠が無い場合は 401、同時実行上限なら 429。
        """

        session, lease = await self._beginTransfer(session_id, kind)
        try:
            yield session
        finally:
            await self._endTransfer(lease)

    async def addTransferredBytes(
        self,
        session_id: str,
        kind: Literal['download', 'upload'],
        byte_count: int,
    ) -> bool:
        """
        session 累積転送量を加算し、上限内なら True を返す。

        Args:
            session_id: 測定枠 ID。
            kind: 下りまたは上り。
            byte_count: 今回加算するバイト数。

        Returns:
            加算後も上限内なら True。枠が消えているか上限超過なら False。
        """

        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.released:
                return False
            if self._isTransferWindowExpired(session, kind):
                return False
            if kind == 'download':
                if session.downloaded_bytes + byte_count > KONOMITV_BS4K_SPEED_TEST_MAX_SESSION_DOWNLOAD_BYTES:
                    return False
                session.downloaded_bytes += byte_count
                return True
            if session.uploaded_bytes + byte_count > KONOMITV_BS4K_SPEED_TEST_MAX_SESSION_UPLOAD_BYTES:
                return False
            session.uploaded_bytes += byte_count
            return True

    async def _beginTransfer(
        self,
        session_id: str,
        kind: KonomiTVBS4KSpeedTestTransferKind,
    ) -> tuple[KonomiTVBS4KSpeedTestSessionState, KonomiTVBS4KSpeedTestTransferLease]:
        async with self._lock:
            self._collectExpiredSessionsLocked()
            session = self._sessions.get(session_id)
            if session is None or session.released:
                raise HTTPException(
                    status_code = status.HTTP_401_UNAUTHORIZED,
                    detail = 'Unauthorized',
                )
            if kind in ('download', 'upload') and self._isTransferWindowExpired(session, kind):
                logging.warning(
                    '[KonomiTVBS4KSpeedTest] Rejected transfer because the measurement window expired. '
                    f'[session_id: {session_id} / kind: {kind}]'
                )
                raise self._tooManyRequests(self._retryAfterSeconds(session))
            leases, maximum = self._transferLeasesAndMaximum(session, kind)
            in_flight = len(leases)
            if in_flight >= maximum:
                logging.warning(
                    '[KonomiTVBS4KSpeedTest] Rejected transfer because the stream limit is full. '
                    f'[session_id: {session_id} / kind: {kind} / in_flight: {in_flight}]'
                )
                raise self._tooManyRequests(self._retryAfterSeconds(session))
            now = time.monotonic()
            if kind == 'download':
                if session.download_started_monotonic is None:
                    session.download_started_monotonic = now
                    session.download_window_task = asyncio.create_task(
                        self._expireTransferWindow(
                            session_id,
                            'download',
                            KONOMITV_BS4K_SPEED_TEST_DOWNLOAD_WINDOW_SECONDS,
                        ),
                        name = f'konomitv-bs4k-speed-test-download-window-{session_id}',
                    )
            elif kind == 'upload':
                if session.upload_started_monotonic is None:
                    session.upload_started_monotonic = now
                    session.upload_window_task = asyncio.create_task(
                        self._expireTransferWindow(
                            session_id,
                            'upload',
                            KONOMITV_BS4K_SPEED_TEST_UPLOAD_WINDOW_SECONDS,
                        ),
                        name = f'konomitv-bs4k-speed-test-upload-window-{session_id}',
                    )

            # ASGI request Task の完了 callback でも lease を返すことで、StreamingResponse が開始前に失敗しても漏らさない。
            owner_task = cast(asyncio.Task[None] | None, asyncio.current_task())
            if owner_task is None:
                raise RuntimeError('Speed test transfer must run inside an asyncio Task.')
            lease = KonomiTVBS4KSpeedTestTransferLease(
                lease_id = uuid.uuid4().hex,
                session_id = session_id,
                kind = kind,
                owner_task = owner_task,
            )
            leases[lease.lease_id] = lease
            owner_task.add_done_callback(lambda _: self._scheduleTransferLeaseRelease(lease))
            return session, lease

    async def _endTransfer(
        self,
        lease: KonomiTVBS4KSpeedTestTransferLease,
    ) -> None:
        async with self._lock:
            session = self._sessions.get(lease.session_id)
            if session is None:
                return
            leases, _ = self._transferLeasesAndMaximum(session, lease.kind)
            leases.pop(lease.lease_id, None)
            # 解放済みで転送が残っていない枠だけを辞書から外し、再作成で上限を迂回させない。
            if session.released and self._hasInFlightTransfers(session) is False:
                self._forgetSessionLocked(session)

    def _scheduleTransferLeaseRelease(self, lease: KonomiTVBS4KSpeedTestTransferLease) -> None:
        """
        ASGI request Task の完了後に、残っている転送 lease を非同期で返す。

        Args:
            lease: request Task が所有していた転送 lease。

        Returns:
            None
        """

        try:
            asyncio.get_running_loop().create_task(
                self._endTransfer(lease),
                name = f'konomitv-bs4k-speed-test-release-{lease.lease_id}',
            )
        except RuntimeError:
            # イベントループ終了時はプロセス自体が破棄されるため、registry の回収は不要。
            return

    async def _expireTransferWindow(
        self,
        session_id: str,
        kind: Literal['download', 'upload'],
        delay_seconds: float,
    ) -> None:
        """
        grace time 込みの測定窓が閉じたら、その方向の ASGI request Task を停止する。

        Args:
            session_id: 測定枠 ID。
            kind: 下りまたは上り。
            delay_seconds: 最初の転送開始から停止までの秒数。

        Returns:
            None
        """

        try:
            await asyncio.sleep(delay_seconds)
        except asyncio.CancelledError:
            return
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            leases, _ = self._transferLeasesAndMaximum(session, kind)
            owner_tasks = {lease.owner_task for lease in leases.values()}
            if kind == 'download':
                session.download_window_task = None
            else:
                session.upload_window_task = None
        for owner_task in owner_tasks:
            if owner_task.done() is False:
                owner_task.cancel()

    async def _expireSession(self, session_id: str) -> None:
        try:
            await asyncio.sleep(KONOMITV_BS4K_SPEED_TEST_SESSION_TTL_SECONDS)
        except asyncio.CancelledError:
            return
        await self.releaseSession(session_id)

    def _releaseSessionLocked(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session is None or session.released:
            return False
        # 転送中でも枠を「解放済み」にして新規 acquire を拒み、既存 generator へ停止を伝える。
        session.released = True
        self._cancelExpiryTask(session)
        self._cancelTransferWindowTasks(session)
        current_task = asyncio.current_task()
        owner_tasks = {
            lease.owner_task
            for leases in (session.download_leases, session.upload_leases, session.ping_leases)
            for lease in leases.values()
        }
        for owner_task in owner_tasks:
            if owner_task is not current_task and owner_task.done() is False:
                owner_task.cancel()
        if self._hasInFlightTransfers(session) is False:
            self._forgetSessionLocked(session)
        return True

    def _forgetSessionLocked(self, session: KonomiTVBS4KSpeedTestSessionState) -> None:
        self._sessions.pop(session.session_id, None)

    @staticmethod
    def _hasInFlightTransfers(session: KonomiTVBS4KSpeedTestSessionState) -> bool:
        return (
            len(session.download_leases) > 0 or
            len(session.upload_leases) > 0 or
            len(session.ping_leases) > 0
        )

    @staticmethod
    def _isTransferWindowExpired(
        session: KonomiTVBS4KSpeedTestSessionState,
        kind: Literal['download', 'upload'],
    ) -> bool:
        started = (
            session.download_started_monotonic
            if kind == 'download'
            else session.upload_started_monotonic
        )
        if started is None:
            return False
        limit = (
            KONOMITV_BS4K_SPEED_TEST_DOWNLOAD_WINDOW_SECONDS
            if kind == 'download'
            else KONOMITV_BS4K_SPEED_TEST_UPLOAD_WINDOW_SECONDS
        )
        return time.monotonic() - started >= limit

    def _collectExpiredSessionsLocked(self) -> None:
        now = time.monotonic()
        expired_session_ids = [
            session_id
            for session_id, session in self._sessions.items()
            if session.expires_at_monotonic <= now
        ]
        for session_id in expired_session_ids:
            self._releaseSessionLocked(session_id)

    @staticmethod
    def _cancelExpiryTask(session: KonomiTVBS4KSpeedTestSessionState) -> None:
        current_task = asyncio.current_task()
        if (
            session.expiry_task is not None and
            session.expiry_task is not current_task and
            session.expiry_task.done() is False
        ):
            session.expiry_task.cancel()
        session.expiry_task = None

    @staticmethod
    def _cancelTransferWindowTasks(session: KonomiTVBS4KSpeedTestSessionState) -> None:
        current_task = asyncio.current_task()
        for window_task in (session.download_window_task, session.upload_window_task):
            if window_task is not None and window_task is not current_task and window_task.done() is False:
                window_task.cancel()
        session.download_window_task = None
        session.upload_window_task = None

    @staticmethod
    def _transferLeasesAndMaximum(
        session: KonomiTVBS4KSpeedTestSessionState,
        kind: KonomiTVBS4KSpeedTestTransferKind,
    ) -> tuple[dict[str, KonomiTVBS4KSpeedTestTransferLease], int]:
        if kind == 'download':
            return session.download_leases, KONOMITV_BS4K_SPEED_TEST_MAX_DOWNLOAD_STREAMS
        if kind == 'upload':
            return session.upload_leases, KONOMITV_BS4K_SPEED_TEST_MAX_UPLOAD_STREAMS
        return session.ping_leases, KONOMITV_BS4K_SPEED_TEST_MAX_PING_STREAMS

    @staticmethod
    def _retryAfterSeconds(session: KonomiTVBS4KSpeedTestSessionState) -> int:
        remaining = session.expires_at_monotonic - time.monotonic()
        return max(1, math.ceil(remaining))

    @staticmethod
    def _tooManyRequests(retry_after: int) -> HTTPException:
        return HTTPException(
            status_code = status.HTTP_429_TOO_MANY_REQUESTS,
            detail = 'Too many requests',
            headers = {'Retry-After': str(retry_after)},
        )


SPEED_TEST_SESSION_MANAGER = KonomiTVBS4KSpeedTestSessionManager()


def ParseKonomiTVBS4KSpeedTestBitrateKbps(value: str) -> int:
    """
    `13000K` 形式の bitrate を kbps 整数へ変換する。

    Args:
        value: QUALITY や muxrate が使う `NNNK` 文字列。

    Returns:
        kbps 整数。

    Raises:
        ValueError: 形式が不正、または非正の場合。
    """

    if value.endswith('K') is False:
        raise ValueError(f'Invalid bitrate: {value}')
    try:
        bitrate_kbps = int(value[:-1])
    except ValueError as ex:
        raise ValueError(f'Invalid bitrate: {value}') from ex
    if bitrate_kbps <= 0:
        raise ValueError(f'Invalid bitrate: {value}')
    return bitrate_kbps


def ResolveKonomiTVBS4KSpeedTestAudioBitrateKbps(
    quality: QUALITY_TYPES,
    codec: KonomiTVBS4KVideoCodec,
) -> int:
    """
    画質と codec に対応する音声 bitrate を返す。

    Args:
        quality: 通常放送または BS4K の画質キー。
        codec: 再生 codec。

    Returns:
        音声 bitrate の kbps。
    """

    quality_without_codec = quality.removesuffix('-hevc')
    quality_key = cast(
        QUALITY_TYPES,
        quality_without_codec if codec == 'avc' else f'{quality_without_codec}-hevc',
    )
    if quality_key not in QUALITY:
        raise ValueError(f'Audio bitrate is not defined for quality: {quality}')
    return ParseKonomiTVBS4KSpeedTestBitrateKbps(QUALITY[quality_key].audio_bitrate)


def BuildKonomiTVBS4KSpeedTestQualityThresholds() -> list[KonomiTVBS4KSpeedTestQualityThreshold]:
    """
    再生映像 bitrate 正本から、視聴に必要な Mbps の閾値表を作る。

    ライブ VP9 / AV1 の固定 muxrate は T-STD 用 stuffing 余力を含む。
    推奨画質は視聴ペイロード（映像最大 + 音声）で比較し、
    AV1 < VP9 < HEVC < AVC の再生 bitrate 順を崩さない。

    Returns:
        通常放送と BS4K、AVC / HEVC / VP9 / AV1、各画質の必要 Mbps。
    """

    thresholds: list[KonomiTVBS4KSpeedTestQualityThreshold] = []
    broadcast_groups: tuple[
        tuple[KonomiTVBS4KSpeedTestBroadcastType, tuple[QUALITY_TYPES, ...]],
        ...,
    ] = (
        ('Terrestrial', KONOMITV_BS4K_SPEED_TEST_TERRESTRIAL_QUALITIES),
        ('BS4K', KONOMITV_BS4K_SPEED_TEST_BS4K_QUALITIES),
    )
    for broadcast_type, qualities in broadcast_groups:
        for display_codec, video_codec in KONOMITV_BS4K_SPEED_TEST_CODECS:
            for quality in qualities:
                video_bitrate = ResolveKonomiTVBS4KPlaybackVideoBitrate(quality, video_codec)
                audio_bitrate_kbps = ResolveKonomiTVBS4KSpeedTestAudioBitrateKbps(quality, video_codec)
                required_kbps = (
                    ParseKonomiTVBS4KSpeedTestBitrateKbps(video_bitrate.video_bitrate_max) +
                    audio_bitrate_kbps
                ) * KONOMITV_BS4K_SPEED_TEST_VARIABLE_BITRATE_SAFETY_FACTOR
                thresholds.append(KonomiTVBS4KSpeedTestQualityThreshold(
                    broadcast_type = broadcast_type,
                    codec = display_codec,
                    quality = quality,
                    required_mbps = required_kbps / 1000.0,
                    basis = 'VariableBitrate',
                ))
    return thresholds


def GenerateKonomiTVBS4KSpeedTestSessionToken(session_id: str) -> str:
    """
    測定専用の短命 JWT を発行する。

    Args:
        session_id: プロセス内 registry の測定枠 ID。

    Returns:
        HttpOnly Cookie へ入れる JWT。JSON 応答には含めない。
    """

    issued_at = datetime.now(JST)
    return jwt.encode(
        claims = {
            'iss': KONOMITV_BS4K_SPEED_TEST_JWT_ISSUER,
            'typ': KONOMITV_BS4K_SPEED_TEST_JWT_TYPE,
            'sid': session_id,
            'iat': issued_at,
            'exp': issued_at + timedelta(seconds=KONOMITV_BS4K_SPEED_TEST_SESSION_TTL_SECONDS),
        },
        key = JWT_SECRET_KEY,
        algorithm = 'HS256',
    )


def SetKonomiTVBS4KSpeedTestSessionCookie(response: Response, token: str) -> None:
    """測定専用 Cookie を Path 限定で付ける。"""

    response.set_cookie(
        key = KONOMITV_BS4K_SPEED_TEST_COOKIE_NAME,
        value = token,
        max_age = KONOMITV_BS4K_SPEED_TEST_SESSION_TTL_SECONDS,
        httponly = True,
        secure = True,
        samesite = 'strict',
        path = KONOMITV_BS4K_SPEED_TEST_COOKIE_PATH,
    )


def NormalizeRequestAuthority(host_header: str) -> tuple[str, int | None] | None:
    """
    Host ヘッダーを Origin 比較用の host と port へ正規化する。

    Args:
        host_header: ブラウザまたは TLS 終端が付けた Host。

    Returns:
        小文字の host と、非標準 port。既定 port は None。不正なら全体を None。
    """

    host = host_header.strip().lower()
    if host == '':
        return None
    try:
        parsed = urlsplit(f'//{host}')
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        hostname is None or
        parsed.username is not None or
        parsed.password is not None or
        parsed.path != '' or
        parsed.query != '' or
        parsed.fragment != ''
    ):
        return None
    if port is None or port in {80, 443}:
        return hostname, None
    return hostname, port


def GetRequestPublicAuthority(request: Request) -> tuple[str, int | None]:
    """
    ブラウザが見ている公開 host と port を返す。

    Akebi は HTTPS を内部 HTTP へ転送し、Uvicorn の request.url は 127.0.0.77 になる。
    公開 Host は Host ヘッダー側を正本にする。

    Args:
        request: 検証対象のリクエスト。

    Returns:
        正規化済み host と、非標準 port。
    """

    host_header = request.headers.get('host')
    if host_header is not None:
        normalized = NormalizeRequestAuthority(host_header)
        if normalized is not None:
            return normalized
    try:
        hostname = request.url.hostname
        port = request.url.port
    except ValueError as ex:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Forbidden') from ex
    if hostname is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Forbidden')
    if port is None or port in {80, 443}:
        return hostname.lower(), None
    return hostname.lower(), port


def NormalizeOriginAuthority(origin: str) -> tuple[str, int | None] | None:
    """
    Origin ヘッダーを公開 authority 比較用の host と port へ正規化する。

    Args:
        origin: ブラウザが付けた Origin。

    Returns:
        小文字の host と、非標準 port。既定 port は None。不正なら全体を None。
    """

    try:
        parsed = urlsplit(origin)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {'http', 'https'} or hostname is None:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.path not in {'', '/'} or parsed.query != '' or parsed.fragment != '':
        return None
    if port is None or port in {80, 443}:
        return hostname.lower(), None
    return hostname.lower(), port


def RequireSpeedTestFetchMetadata(request: Request) -> None:
    """
    測定 API が同一オリジンのブラウザから来たことを Fetch Metadata で確認する。

    Args:
        request: 検証対象のリクエスト。

    Raises:
        HTTPException: Sec-Fetch-Site が same-origin でない場合は 403。
    """

    sec_fetch_site = request.headers.get('sec-fetch-site')
    if sec_fetch_site != 'same-origin':
        logging.warning('[KonomiTVBS4KSpeedTest] Rejected request because Sec-Fetch-Site is not same-origin.')
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Forbidden')


def RequireSpeedTestOrigin(request: Request) -> None:
    """
    状態変更を伴う測定 API の Origin が公開オリジンと一致することを確認する。

    Args:
        request: 検証対象のリクエスト。

    Raises:
        HTTPException: Origin が無い、または公開オリジンと一致しない場合は 403。
    """

    origin_header = request.headers.get('origin')
    if origin_header is None:
        logging.warning('[KonomiTVBS4KSpeedTest] Rejected request because Origin is missing.')
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Forbidden')
    origin_authority = NormalizeOriginAuthority(origin_header)
    if origin_authority is None:
        logging.warning('[KonomiTVBS4KSpeedTest] Rejected request because Origin is invalid.')
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Forbidden')
    # scheme は内部 HTTP（Akebi）と公開 HTTPS が食い違うため比較しない。Host だけを見る。
    if origin_authority != GetRequestPublicAuthority(request):
        logging.warning('[KonomiTVBS4KSpeedTest] Rejected request because Origin host does not match the public Host.')
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Forbidden')


async def ResolveSpeedTestSessionFromCookie(request: Request) -> KonomiTVBS4KSpeedTestSessionState:
    """
    専用 Cookie の JWT とプロセス内 registry を照合して測定枠を返す。

    Args:
        request: 測定 Cookie を含むリクエスト。

    Returns:
        有効な測定枠。

    Raises:
        HTTPException: Cookie・JWT・registry のいずれかが無効なら 401。
    """

    token = request.cookies.get(KONOMITV_BS4K_SPEED_TEST_COOKIE_NAME)
    if token is None or token == '':
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Unauthorized')
    try:
        payload = jwt.decode(
            token = token,
            key = JWT_SECRET_KEY,
            algorithms = ['HS256'],
            issuer = KONOMITV_BS4K_SPEED_TEST_JWT_ISSUER,
        )
    except (JWTError, TypeError, ValueError) as ex:
        logging.warning('[KonomiTVBS4KSpeedTest] Speed test session token is invalid:', exc_info=ex)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Unauthorized') from ex

    if payload.get('typ') != KONOMITV_BS4K_SPEED_TEST_JWT_TYPE:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Unauthorized')
    session_id = payload.get('sid')
    if not isinstance(session_id, str) or session_id == '':
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Unauthorized')

    session = await SPEED_TEST_SESSION_MANAGER.getActiveSession(session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Unauthorized')
    return session


def ResolveGarbageChunkCount(ck_size: str | None) -> int:
    """
    LibreSpeed の ckSize を 1MiB chunk 数へ正規化する。

    Args:
        ck_size: クエリ文字列。欠如や非数値、0 以下は 4。100 超は 100。

    Returns:
        1 応答で送る 1MiB chunk 数。
    """

    if ck_size is None:
        return KONOMITV_BS4K_SPEED_TEST_DEFAULT_CK_SIZE
    try:
        parsed = int(ck_size)
    except ValueError:
        return KONOMITV_BS4K_SPEED_TEST_DEFAULT_CK_SIZE
    if parsed <= 0:
        return KONOMITV_BS4K_SPEED_TEST_DEFAULT_CK_SIZE
    return min(parsed, KONOMITV_BS4K_SPEED_TEST_MAX_CK_SIZE)


def BuildSpeedTestTransferHeaders(content_type: str = 'application/octet-stream') -> dict[str, str]:
    """測定応答をキャッシュ・圧縮させないヘッダーを返す。"""

    return {
        'Content-Type': content_type,
        'Cache-Control': 'no-store, no-cache, no-transform, must-revalidate',
        'Content-Encoding': 'identity',
        'Pragma': 'no-cache',
    }


async def IterateSpeedTestGarbage(
    request: Request,
    session_id: str,
    chunk_count: int,
) -> AsyncIterator[bytes]:
    """
    1MiB の乱数を指定回数だけ送る。切断または累積上限で止める。

    Args:
        request: 切断検知に使うリクエスト。
        session_id: 累積下りバイトを加算する測定枠。
        chunk_count: 送る 1MiB chunk 数。

    Yields:
        1MiB の乱数。同一応答内では同じ chunk を再利用する。
    """

    chunk = secrets.token_bytes(KONOMITV_BS4K_SPEED_TEST_CHUNK_BYTES)
    for _ in range(chunk_count):
        if await request.is_disconnected():
            return
        if await SPEED_TEST_SESSION_MANAGER.isDirectionCancelled(session_id, 'download'):
            return
        accepted = await SPEED_TEST_SESSION_MANAGER.addTransferredBytes(
            session_id,
            'download',
            KONOMITV_BS4K_SPEED_TEST_CHUNK_BYTES,
        )
        if accepted is False:
            return
        yield chunk


async def DrainSpeedTestUpload(request: Request, session_id: str) -> None:
    """
    上り本文を保持せず捨てる。20MiB / 30秒 / session 累積上限を超えたら止める。

    Args:
        request: 上り本文を持つリクエスト。
        session_id: 累積上りバイトを加算する測定枠。

    Raises:
        HTTPException: Content-Length 超過や本文超過は 413。timeout も 413 相当で切る。
    """

    content_length = request.headers.get('content-length')
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as ex:
            raise HTTPException(
                status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail = 'Payload too large',
            ) from ex
        if declared_length > KONOMITV_BS4K_SPEED_TEST_MAX_UPLOAD_REQUEST_BYTES:
            raise HTTPException(
                status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail = 'Payload too large',
            )

    async def Drain() -> None:
        received_bytes = 0
        async for chunk in request.stream():
            if await SPEED_TEST_SESSION_MANAGER.isDirectionCancelled(session_id, 'upload'):
                return
            if not chunk:
                continue
            received_bytes += len(chunk)
            if received_bytes > KONOMITV_BS4K_SPEED_TEST_MAX_UPLOAD_REQUEST_BYTES:
                raise HTTPException(
                    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail = 'Payload too large',
                )
            accepted = await SPEED_TEST_SESSION_MANAGER.addTransferredBytes(
                session_id,
                'upload',
                len(chunk),
            )
            if accepted is False:
                raise HTTPException(
                    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail = 'Payload too large',
                )

    try:
        await asyncio.wait_for(Drain(), timeout=KONOMITV_BS4K_SPEED_TEST_DRAIN_TIMEOUT_SECONDS)
    except ClientDisconnect:
        # Worker の abort で upload XHR が切断されるのは正常終了なので、例外ログを残さない。
        return
    except TimeoutError as ex:
        logging.warning('[KonomiTVBS4KSpeedTest] Upload drain timed out.')
        raise HTTPException(
            status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail = 'Payload too large',
        ) from ex
