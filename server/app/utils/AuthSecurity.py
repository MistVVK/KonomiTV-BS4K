"""認証関連のセキュリティ補助機能。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import math
import secrets
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request

from app.constants import JWT_SECRET_KEY


# ユーザー名が存在しない場合にも実行するbcrypt検証用の固定ハッシュ。
# 実際のパスワードとは一致しない値で、パスワード自体は保存しない。
DUMMY_PASSWORD_HASH = '$2b$12$kK0rtZRugBrpaGfftnW.SOBXnoig/KIrpOVggEPcR.HWMJkcn4hBy'


def GetClientIP(request: Request) -> str:
    """信頼済みプロキシ処理後のリクエストからクライアントIPを取得する。

    X-Forwarded-For はこの関数では直接解釈しない。KonomiTV の起動時に
    Uvicorn が Akebi からの転送ヘッダーだけを処理する設定になっているため、
    ここでは補正済みの ASGI scope.client のみを利用する。
    """

    if request.client is None:
        return 'unknown'

    try:
        return str(ipaddress.ip_address(request.client.host))
    except ValueError:
        return 'unknown'


def GetUsernameRateLimitKey(username: str) -> str:
    """ユーザー名をレート制限用の秘匿キーへ変換する。"""

    normalized_username = unicodedata.normalize('NFKC', username).casefold()
    username_digest = hmac.new(
        JWT_SECRET_KEY.encode('utf-8'),
        normalized_username.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    return f'username:{username_digest}'


def GetUsernameFingerprint(username: str) -> str:
    """監査ログ用のユーザー名フィンガープリントを返す。"""

    return GetUsernameRateLimitKey(username).removeprefix('username:')


def GenerateRefreshToken() -> str:
    """更新トークンとして利用する暗号学的乱数を生成する。"""

    return secrets.token_urlsafe(32)


def GenerateRefreshTokenFamilyID() -> str:
    """更新トークンローテーションのファミリーIDを生成する。"""

    return secrets.token_hex(16)


def HashRefreshToken(refresh_token: str) -> str:
    """更新トークンをDB保存用のHMAC値へ変換する。"""

    return hmac.new(
        JWT_SECRET_KEY.encode('utf-8'),
        refresh_token.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()


@dataclass
class _RateLimitBucket:
    capacity: float
    refill_per_second: float
    tokens: float
    updated_at: float
    last_failure_at: float = 0.0
    consecutive_blocks: int = 0
    blocked_until: float = 0.0


class LoginAttemptLimiter:
    """ログイン失敗をIPとユーザー名の二軸で制限する。

    現行のKonomiTVサーバーは単一Uvicornプロセスで動作するため、プロセス内で
    レート制限を行う。エントリ数には上限を設け、期限切れエントリを回収して
    攻撃者が無限にキーを増やせないようにする。
    """

    def __init__(
        self,
        *,
        ip_capacity: float = 20.0,
        username_capacity: float = 5.0,
        refill_interval: float = 60.0,
        backoff_base: float = 1.0,
        backoff_max: float = 15.0 * 60.0,
        max_entries: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ip_capacity = ip_capacity
        self.username_capacity = username_capacity
        self.refill_interval = refill_interval
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.max_entries = max_entries
        self._clock = clock
        self._buckets: dict[str, _RateLimitBucket] = {}
        self._lock = asyncio.Lock()

    def _CreateBucket(self, key: str, capacity: float, now: float) -> _RateLimitBucket:
        if len(self._buckets) >= self.max_entries:
            oldest_key = min(self._buckets, key=lambda item: self._buckets[item].updated_at)
            del self._buckets[oldest_key]

        bucket = _RateLimitBucket(
            capacity = capacity,
            refill_per_second = 1.0 / self.refill_interval,
            tokens = capacity,
            updated_at = now,
        )
        self._buckets[key] = bucket
        return bucket

    def _GetBucket(self, key: str, capacity: float, now: float) -> _RateLimitBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._CreateBucket(key, capacity, now)

        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(bucket.capacity, bucket.tokens + elapsed * bucket.refill_per_second)
        bucket.updated_at = now

        # 十分に時間が空いた場合は、指数バックオフの段階もリセットする。
        if bucket.last_failure_at > 0.0 and now - bucket.last_failure_at >= self.refill_interval:
            bucket.consecutive_blocks = 0

        return bucket

    def _Cleanup(self, now: float) -> None:
        stale_keys = [
            key for key, bucket in self._buckets.items()
            if now - bucket.updated_at >= self.refill_interval * 2
            and bucket.blocked_until <= now
            and bucket.tokens >= bucket.capacity
        ]
        for key in stale_keys:
            del self._buckets[key]

    @staticmethod
    def _RetryAfter(bucket: _RateLimitBucket, now: float) -> int:
        if bucket.blocked_until > now:
            return max(1, math.ceil(bucket.blocked_until - now))
        if bucket.tokens < 1.0:
            return max(1, math.ceil((1.0 - bucket.tokens) / bucket.refill_per_second))
        return 0

    def _RecordFailure(self, bucket: _RateLimitBucket, now: float) -> int:
        bucket.tokens = max(0.0, bucket.tokens - 1.0)
        bucket.last_failure_at = now

        if bucket.tokens < 1.0:
            bucket.consecutive_blocks += 1
            backoff_seconds = min(
                self.backoff_base * (2 ** (bucket.consecutive_blocks - 1)),
                self.backoff_max,
            )
            bucket.blocked_until = max(bucket.blocked_until, now + backoff_seconds)

        return self._RetryAfter(bucket, now)

    async def Check(self, client_ip: str, username_key: str) -> int | None:
        """制限中なら再試行可能までの秒数を返す。"""

        async with self._lock:
            now = self._clock()
            ip_bucket = self._GetBucket(f'ip:{client_ip}', self.ip_capacity, now)
            username_bucket = self._GetBucket(username_key, self.username_capacity, now)
            self._Cleanup(now)

            retry_after = max(
                self._RetryAfter(ip_bucket, now),
                self._RetryAfter(username_bucket, now),
            )
            return retry_after if retry_after > 0 else None

    async def RecordFailure(self, client_ip: str, username_key: str) -> int | None:
        """認証失敗を記録し、次回以降の再試行待ち時間を返す。"""

        async with self._lock:
            now = self._clock()
            ip_bucket = self._GetBucket(f'ip:{client_ip}', self.ip_capacity, now)
            username_bucket = self._GetBucket(username_key, self.username_capacity, now)
            retry_after = max(
                self._RecordFailure(ip_bucket, now),
                self._RecordFailure(username_bucket, now),
            )
            self._Cleanup(now)
            return retry_after if retry_after > 0 else None

    async def RecordSuccess(self, username_key: str) -> None:
        """正常ログイン時にユーザー名軸の失敗状態を解除する。"""

        async with self._lock:
            self._buckets.pop(username_key, None)
            self._Cleanup(self._clock())

    async def Reset(self) -> None:
        """テスト用に状態を消去する。"""

        async with self._lock:
            self._buckets.clear()


LOGIN_ATTEMPT_LIMITER = LoginAttemptLimiter()
