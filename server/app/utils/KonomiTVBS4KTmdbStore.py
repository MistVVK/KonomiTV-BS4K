"""TMDb API キーを Fernet 暗号化して保持する共有ストア。

保存先は DATA_DIR/konomitv-bs4k-tmdb.json。平文の API キーは API 応答・ログ・設定
ファイルへ出さず、復号は TMDb 経路の実行時だけ行う。書き込みは Bangumi 共有ストアと
同じく同一ディレクトリ内の一時ファイル + fsync + os.replace で原子的に置き換える。
"""

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
    DATA_DIR,
    TMDB_API_KEY_ENCRYPTION_PREFIX,
    TMDB_API_KEY_FERNET,
)


class _TmdbStorePayload(TypedDict):
    """TMDb ストアファイルの JSON 構造。暗号化済み API キーだけを保持する。"""

    tmdb_api_key: str | None


class KonomiTVBS4KTmdbStore:
    """管理者1件の TMDb API キーを Fernet 暗号化して DATA_DIR へ保持する。"""

    STORE_PATH = DATA_DIR / 'konomitv-bs4k-tmdb.json'
    # UI からの貼り付けで混入する空白を除いたあとの受理上限。TMDb v3 キーは 32 文字。
    API_KEY_MAX_LENGTH = 512
    # API 応答へ返すマスク表示。末尾数文字も含め、キーの一部も応答へ出さない。
    MASKED_API_KEY = '*' * 12
    # save / clear / load を同一プロセス内で直列化し、読取り中の差し替えを避ける。
    _lock = threading.Lock()


    @classmethod
    def getAPIKey(cls) -> str | None:
        """
        保存済みの TMDb API キーを復号して返す。

        Returns:
            str | None: 復号済み API キー。未設定または復号失敗時は None。
        """

        encrypted_text = cls._load()['tmdb_api_key'] or ''
        # 接頭辞が無い値は平文の混入なので、TMDb 経路では使わない。
        if encrypted_text.startswith(TMDB_API_KEY_ENCRYPTION_PREFIX) is False:
            return None
        cipher_text = encrypted_text[len(TMDB_API_KEY_ENCRYPTION_PREFIX):].encode('utf-8')
        try:
            api_key = TMDB_API_KEY_FERNET.decrypt(cipher_text).decode('utf-8')
        except (InvalidToken, UnicodeError):
            # JWT シークレット再生成後などは復号できない。秘密の中身を出さずに失敗だけ記録する。
            logging.error('[KonomiTVBS4KTmdbStore] Failed to decrypt TMDb API key.')
            return None
        if api_key.strip() == '':
            return None
        return api_key


    @classmethod
    def isConfigured(cls) -> bool:
        """
        TMDb 経路を実行できる API キーが保存されているかを返す。

        Returns:
            bool: 復号可能な API キーがあれば True。
        """

        return cls.getAPIKey() is not None


    @classmethod
    def getMaskedAPIKey(cls) -> str | None:
        """
        設定 API 応答へ返すマスク済み表示を生成する。

        Returns:
            str | None: 設定済みなら固定のマスク文字列。未設定なら None。
        """

        if cls.isConfigured() is False:
            return None
        return cls.MASKED_API_KEY


    @classmethod
    def save(cls, *, api_key: str) -> None:
        """
        UI から受け取った API キーを暗号化して保存する。

        Args:
            api_key (str): 平文の TMDb API キー。前後空白は除去してから検証する。

        Returns:
            None

        Raises:
            ValueError: キーが空または上限超過の場合。
        """

        normalized_key = api_key.strip()
        if normalized_key == '' or len(normalized_key) > cls.API_KEY_MAX_LENGTH:
            raise ValueError('TMDb API key is invalid.')
        encrypted_text = TMDB_API_KEY_FERNET.encrypt(normalized_key.encode('utf-8')).decode('utf-8')
        payload = _TmdbStorePayload(
            tmdb_api_key=f'{TMDB_API_KEY_ENCRYPTION_PREFIX}{encrypted_text}',
        )
        content = json.dumps(payload, ensure_ascii=False)
        with cls._lock:
            cls._writeAtomic(content)


    @classmethod
    def clear(cls) -> bool:
        """
        保存済みの TMDb API キーを削除する。

        Returns:
            bool: 削除前にストアファイルが存在した場合は True。
        """

        with cls._lock:
            if cls.STORE_PATH.is_file() is False:
                return False
            cls.STORE_PATH.unlink()
            return True


    @classmethod
    def _load(cls) -> _TmdbStorePayload:
        """
        ストアファイルを読む。

        Returns:
            _TmdbStorePayload: ファイルが無い・壊れている場合は未設定状態。
        """

        empty = _TmdbStorePayload(tmdb_api_key=None)
        with cls._lock:
            if cls.STORE_PATH.is_file() is False:
                return empty
            try:
                raw = json.loads(cls.STORE_PATH.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError) as ex:
                logging.error('[KonomiTVBS4KTmdbStore] Failed to read TMDb store:', exc_info=ex)
                return empty
        if isinstance(raw, dict) is False:
            return empty
        typed_raw = cast(dict[str, object], raw)
        raw_key = typed_raw.get('tmdb_api_key')
        return _TmdbStorePayload(
            tmdb_api_key=raw_key if isinstance(raw_key, str) else None,
        )


    @classmethod
    def _writeAtomic(cls, content: str) -> None:
        """
        同一ディレクトリの一時ファイルへ全量書き、fsync 後に os.replace する。

        Args:
            content (str): 暗号化済み API キーを含む検証済み JSON。

        Returns:
            None
        """

        # DATA_DIR 全体の権限は変えない。API キーファイルだけ 0600 にする。
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
