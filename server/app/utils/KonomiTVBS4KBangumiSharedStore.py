from __future__ import annotations

import json
import os
import secrets
import threading
from typing import cast

from cryptography.fernet import InvalidToken
from typing_extensions import TypedDict

from app import logging
from app.constants import (
    BANGUMI_ACCESS_TOKEN_ENCRYPTION_PREFIX,
    BANGUMI_ACCESS_TOKEN_FERNET,
    DATA_DIR,
)


class BangumiTokenDecryptionError(RuntimeError):
    """共有 Bangumi トークンを復号できない状態を表す。"""


class KonomiTVBS4KBangumiSharedProfile(TypedDict):
    """共有 Bangumi 連携の公開プロフィール。トークンは含めない。"""

    bangumi_user_id: int | None
    bangumi_user_name: str | None
    bangumi_user_nickname: str | None
    bangumi_user_avatar_url: str | None


class KonomiTVBS4KBangumiSharedCredentials(TypedDict):
    """同じ共有ストアスナップショットから取得した Bangumi 認証情報。"""

    access_token: str | None
    bangumi_user_id: int | None


class _SharedStorePayload(TypedDict):
    bangumi_user_id: int | None
    bangumi_user_name: str | None
    bangumi_user_nickname: str | None
    bangumi_user_avatar_url: str | None
    bangumi_access_token: str | None


class KonomiTVBS4KBangumiSharedStore:
    """管理者1件の Bangumi トークンを Fernet 暗号化して DATA_DIR へ保持する。"""

    STORE_PATH = DATA_DIR / 'konomitv-bs4k-bangumi-shared.json'
    # save / clear / load を同一プロセス内で直列化し、読取り中の差し替えを避ける。
    _lock = threading.Lock()


    @classmethod
    def getProfile(cls) -> KonomiTVBS4KBangumiSharedProfile:
        """
        共有 Bangumi 連携の公開プロフィールを返す。

        Returns:
            KonomiTVBS4KBangumiSharedProfile: 未連携なら各欄が None。
        """

        payload = cls._load()
        return KonomiTVBS4KBangumiSharedProfile(
            bangumi_user_id=payload['bangumi_user_id'],
            bangumi_user_name=payload['bangumi_user_name'],
            bangumi_user_nickname=payload['bangumi_user_nickname'],
            bangumi_user_avatar_url=payload['bangumi_user_avatar_url'],
        )


    @classmethod
    def getCredentials(cls) -> KonomiTVBS4KBangumiSharedCredentials:
        """
        共有 Bangumi ユーザー ID と復号済みトークンを同じストア読取りから返す。

        Returns:
            KonomiTVBS4KBangumiSharedCredentials: 未連携またはトークン破損時は該当欄が None。
        """

        # 外部操作と完了記録の identity が読取り間の連携切替で分離しないよう、_load() は一度だけ行う。
        payload = cls._load()
        encrypted_text = payload['bangumi_access_token'] or ''
        if encrypted_text.startswith(BANGUMI_ACCESS_TOKEN_ENCRYPTION_PREFIX) is False:
            return KonomiTVBS4KBangumiSharedCredentials(
                access_token=None,
                bangumi_user_id=payload['bangumi_user_id'],
            )
        token = encrypted_text[len(BANGUMI_ACCESS_TOKEN_ENCRYPTION_PREFIX):].encode('utf-8')
        try:
            access_token = BANGUMI_ACCESS_TOKEN_FERNET.decrypt(token).decode('utf-8')
        except InvalidToken as ex:
            logging.error('[KonomiTVBS4KBangumiSharedStore] Failed to decrypt access token:', exc_info=ex)
            access_token = None
        return KonomiTVBS4KBangumiSharedCredentials(
            access_token=access_token,
            bangumi_user_id=payload['bangumi_user_id'],
        )


    @classmethod
    def save(
        cls,
        *,
        access_token: str,
        bangumi_user_id: int,
        bangumi_user_name: str,
        bangumi_user_nickname: str,
        bangumi_user_avatar_url: str,
    ) -> None:
        """
        検証済みの共有トークンと公開プロフィールを暗号化したうえで保存する。

        Args:
            access_token (str): 平文の個人アクセストークン。保存時に暗号化する。
            bangumi_user_id (int): Bangumi ユーザー ID。
            bangumi_user_name (str): Bangumi ユーザー名。
            bangumi_user_nickname (str): Bangumi 表示名。
            bangumi_user_avatar_url (str): アバター URL。

        Returns:
            None
        """

        encrypted_text = BANGUMI_ACCESS_TOKEN_FERNET.encrypt(access_token.encode('utf-8')).decode('utf-8')
        payload = _SharedStorePayload(
            bangumi_user_id=bangumi_user_id,
            bangumi_user_name=bangumi_user_name,
            bangumi_user_nickname=bangumi_user_nickname,
            bangumi_user_avatar_url=bangumi_user_avatar_url,
            bangumi_access_token=f'{BANGUMI_ACCESS_TOKEN_ENCRYPTION_PREFIX}{encrypted_text}',
        )
        content = json.dumps(payload, ensure_ascii=False)
        with cls._lock:
            cls._writeAtomic(content)


    @classmethod
    def clear(cls) -> None:
        """
        共有 Bangumi 連携を解除する。

        Returns:
            None
        """

        with cls._lock:
            if cls.STORE_PATH.is_file():
                cls.STORE_PATH.unlink()


    @classmethod
    def decryptAccessToken(cls) -> str | None:
        """
        共有トークンを復号する。未連携または破損時は None。

        Returns:
            str | None: 復号済みトークン。未連携なら None。
        """

        return cls.getCredentials()['access_token']


    @classmethod
    def _load(cls) -> _SharedStorePayload:
        """
        保存ファイルを読む。

        Returns:
            _SharedStorePayload: ファイルが無い・壊れている場合は空の未連携状態。
        """

        empty = _SharedStorePayload(
            bangumi_user_id=None,
            bangumi_user_name=None,
            bangumi_user_nickname=None,
            bangumi_user_avatar_url=None,
            bangumi_access_token=None,
        )
        with cls._lock:
            if cls.STORE_PATH.is_file() is False:
                return empty
            try:
                raw = json.loads(cls.STORE_PATH.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError) as ex:
                logging.error('[KonomiTVBS4KBangumiSharedStore] Failed to read shared store:', exc_info=ex)
                return empty
        if isinstance(raw, dict) is False:
            return empty
        typed_raw = cast(dict[str, object], raw)
        raw_id = typed_raw.get('bangumi_user_id')
        raw_name = typed_raw.get('bangumi_user_name')
        raw_nickname = typed_raw.get('bangumi_user_nickname')
        raw_avatar = typed_raw.get('bangumi_user_avatar_url')
        raw_token = typed_raw.get('bangumi_access_token')
        return _SharedStorePayload(
            bangumi_user_id=raw_id if isinstance(raw_id, int) else None,
            bangumi_user_name=raw_name if isinstance(raw_name, str) else None,
            bangumi_user_nickname=raw_nickname if isinstance(raw_nickname, str) else None,
            bangumi_user_avatar_url=raw_avatar if isinstance(raw_avatar, str) else None,
            bangumi_access_token=raw_token if isinstance(raw_token, str) else None,
        )


    @classmethod
    def _writeAtomic(cls, content: str) -> None:
        """
        同一ディレクトリの一時ファイルへ全量書き、fsync 後に os.replace する。

        Args:
            content (str): 検証済み JSON。

        Returns:
            None
        """

        # DATA_DIR 全体の権限は変えない。トークンファイルだけ 0600 にする。
        cls.STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = cls.STORE_PATH.with_name(
            f'.{cls.STORE_PATH.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp',
        )
        file_descriptor: int | None = None
        encoded = content.encode('utf-8')
        try:
            file_descriptor = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            written = 0
            while written < len(encoded):
                written += os.write(file_descriptor, encoded[written:])
            os.fsync(file_descriptor)
            os.close(file_descriptor)
            file_descriptor = None
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, cls.STORE_PATH)
            directory_descriptor = os.open(
                cls.STORE_PATH.parent,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY,
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
