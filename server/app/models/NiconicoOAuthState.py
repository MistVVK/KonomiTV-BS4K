# Type Hints を指定できるように
# ref: https://stackoverflow.com/a/33533514/17124142
from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, ClassVar

from tortoise import fields, timezone
from tortoise.exceptions import IntegrityError
from tortoise.models import Model as TortoiseModel


if TYPE_CHECKING:
    from app.models.User import User


class NiconicoOAuthState(TortoiseModel):
    """
    ニコニコ OAuth 連携用の短命・使い捨て state。

    ログイン用 JWT を OAuth state / リダイレクト URL に載せないためのサーバ側セッション。
    state 平文は DB に保存せず、SHA-256 ハッシュだけを保持する。
    """

    # OAuth 往復に十分な余裕を持たせつつ、漏えい時の有効窓を短くする
    DEFAULT_TTL: ClassVar[timedelta] = timedelta(minutes=10)
    # token_urlsafe(32) → 約 43 文字。ハッシュは hex 64 文字
    STATE_ID_BYTES: ClassVar[int] = 32
    # token_urlsafe(64) → 約 86 文字。RFC 7636 の code_verifier 上限 128 文字以内
    PKCE_VERIFIER_BYTES: ClassVar[int] = 64

    class Meta(TortoiseModel.Meta):
        table: str = 'niconico_oauth_states'

    id = fields.IntField(pk=True)
    # state_id の SHA-256 hex。平文 state は外部 URL に一度だけ載せ、DB には残さない
    state_hash = fields.CharField(64, unique=True)
    # 1 ユーザーにつき常に最新の OAuth 試行 1 件だけを保持し、DB 増大と旧 state の併存を防ぐ
    user: fields.OneToOneRelation[User] = fields.OneToOneField(
        'models.User',
        related_name='niconico_oauth_state',
        on_delete=fields.CASCADE,
    )
    user_id: int
    # PKCE verifier は callback 消費時に DB から消去する。戻り値のモデルだけが token 交換まで保持する
    code_verifier = fields.TextField(null=True)
    # 連携完了後のフロントリダイレクト先（発行時に正規化した Origin を正とする）
    client_url = fields.TextField()
    expires_at = fields.DatetimeField()
    consumed_at = fields.DatetimeField(null=True)
    issued_at = fields.DatetimeField(auto_now=True)

    @staticmethod
    def hashStateId(state_id: str) -> str:
        """state 平文の SHA-256 hex を返す。"""

        return hashlib.sha256(state_id.encode('utf-8')).hexdigest()

    @staticmethod
    def buildCodeChallenge(code_verifier: str) -> str:
        """PKCE code_verifier から S256 code_challenge を生成する。"""

        digest = hashlib.sha256(code_verifier.encode('ascii')).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')

    @classmethod
    async def cleanupExpired(cls, *, now: datetime | None = None) -> int:
        """
        期限切れ state に残った PKCE verifier を DB から消去する。

        state 行自体は OneToOne 制約と期限切れ判定のために残し、再発行時に同じ行を置換する。

        Args:
            now: 期限切れ判定に使う現在時刻。省略時は呼び出し時刻。

        Returns:
            PKCE verifier を消去した state 行数。
        """

        cleanup_before = now if now is not None else timezone.now()

        # 未消費のまま放置された state でも、必要期間を過ぎた verifier の平文は保持し続けない。
        # consumed_at の有無にかかわらず期限切れかつ verifier が残る行を対象にし、異常な残存も回収する。
        return await cls.filter(
            expires_at__lte=cleanup_before,
            code_verifier__isnull=False,
        ).update(code_verifier=None)

    @classmethod
    async def issue(
        cls,
        *,
        user_id: int,
        client_url: str,
        ttl: timedelta | None = None,
    ) -> tuple[str, str]:
        """
        新しい OAuth state と PKCE challenge を発行する。

        同一ユーザーの既存レコードは置換し、常に最新の認証試行だけを有効にする。

        Args:
            user_id: 連携対象の KonomiTV ユーザー ID。
            client_url: 連携完了後に戻す、パスと末尾 / を含まないクライアント Origin。
            ttl: 有効期限。省略時は DEFAULT_TTL。

        Returns:
            外部 OAuth state に埋め込む state_id と、認証 URL に載せる S256 code_challenge。
        """

        lifetime = ttl if ttl is not None else cls.DEFAULT_TTL
        now = timezone.now()

        # 新しい state の発行を、他ユーザーが放置した期限切れ verifier の回収機会にもする。
        await cls.cleanupExpired(now=now)

        # state_hash の衝突や同時初回発行が起きても、最新値が DB に反映された場合だけ返す。
        # user_id の OneToOne 制約により、発行回数にかかわらずレコード数はユーザー数を超えない。
        for _ in range(3):
            state_id = secrets.token_urlsafe(cls.STATE_ID_BYTES)
            state_hash = cls.hashStateId(state_id)
            code_verifier = secrets.token_urlsafe(cls.PKCE_VERIFIER_BYTES)
            try:
                record, _ = await cls.update_or_create(
                    user_id=user_id,
                    defaults={
                        'state_hash': state_hash,
                        'code_verifier': code_verifier,
                        'client_url': client_url,
                        'expires_at': now + lifetime,
                        'consumed_at': None,
                    },
                )
            except IntegrityError:
                continue
            # 同時初回発行の競合で別 state が採用された場合は、新しい値で再度置換する。
            if record.state_hash == state_hash:
                return state_id, cls.buildCodeChallenge(code_verifier)
        raise RuntimeError('Failed to issue a unique Niconico OAuth state')

    @classmethod
    async def consume(cls, state_id: str) -> NiconicoOAuthState:
        """
        state を一度だけ消費し、紐づくレコードを返す。

        期限切れ・未登録・再使用はすべて ValueError にする（詳細はログ側で区別）。

        Args:
            state_id: コールバックで受け取った平文 state。

        Returns:
            消費済みになった NiconicoOAuthState。

        Raises:
            ValueError: state が無効なとき。
        """

        if not state_id or not state_id.strip():
            raise ValueError('OAuth state is empty')

        state_hash = cls.hashStateId(state_id.strip())
        now = timezone.now()

        # PKCE verifier は token 交換に必要なため、消去前の値をメモリ上へ保持する。
        record = await cls.filter(
            state_hash=state_hash,
            consumed_at=None,
            expires_at__gt=now,
            code_verifier__isnull=False,
        ).get_or_none()
        if record is None or record.code_verifier is None:
            raise ValueError('OAuth state is invalid, expired, or already consumed')

        # 条件付き UPDATE でワンタイム性を担保し、PKCE verifier も DB から即時消去する。
        # 再発行で同じ user_id の行が置換された場合も state_hash 条件で旧 callback を拒否する。
        updated = await cls.filter(
            id=record.id,
            state_hash=state_hash,
            consumed_at=None,
            expires_at__gt=now,
            code_verifier__isnull=False,
        ).update(
            consumed_at=now,
            code_verifier=None,
        )
        if updated != 1:
            raise ValueError('OAuth state is invalid, expired, or already consumed')

        # QuerySet.update() は取得済みモデルへ値を反映しないため、返却モデルの消費時刻だけ同期する。
        # code_verifier はこの callback の token 交換までメモリ上で保持する。
        record.consumed_at = now
        return record
