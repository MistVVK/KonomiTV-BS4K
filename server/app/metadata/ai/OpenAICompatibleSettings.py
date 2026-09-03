"""OpenAI 互換 HTTP バックエンドの共有設定と秘密を管理する。

保存先:
- DATA_DIR/openai-compatible-settings.json: 接続先とモデル（非秘密）
- DATA_DIR/secrets/openai-compatible-api.key: API キー（0600・atomic・非返却）
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.constants import DATA_DIR
from app.metadata.ai.AIBackendSettings import NormalizeAPIBaseURL


# EpisodeLookupResult の監査ラベル上限255文字から `openai-compatible:` の18文字を除く。
OPENAI_COMPATIBLE_MODEL_MAX_LENGTH = 237


class OpenAICompatibleSettings(BaseModel):
    """OpenAI 互換 HTTP バックエンドの非秘密設定。"""

    model_config = ConfigDict(extra='forbid')

    api_base_url: Annotated[str | None, Field(max_length=2048)] = None
    model: Annotated[
        str | None,
        Field(max_length=OPENAI_COMPATIBLE_MODEL_MAX_LENGTH),
    ] = None

    @field_validator('api_base_url')
    @classmethod
    def validateAPIBaseURL(cls, value: str | None) -> str | None:
        """API ベース URL を正規化する。

        Args:
            value: 利用者が入力した provider root または既知 endpoint URL。

        Returns:
            末尾 slash を除いた HTTP(S) URL。空入力は None。
        """

        if value is None or value.strip() == '':
            return None
        return NormalizeAPIBaseURL(value)

    @field_validator('model')
    @classmethod
    def validateModel(cls, value: str | None) -> str | None:
        """モデル ID の空白を正規化する。

        Args:
            value: 利用者が入力したモデル ID。

        Returns:
            前後空白を除いた ID。空入力は None。
        """

        if value is None:
            return None
        return value.strip() or None


class OpenAICompatibleSettingsResponse(OpenAICompatibleSettings):
    """秘密本体を含まない OpenAI 互換設定 API 応答。"""

    api_key_configured: Annotated[bool, Field()]


class OpenAICompatibleSettingsStore:
    """OpenAI 互換 HTTP の設定と専用 API キーを原子的に永続化する。"""

    SETTINGS_PATH = DATA_DIR / 'openai-compatible-settings.json'
    API_KEY_PATH = DATA_DIR / 'secrets' / 'openai-compatible-api.key'

    _API_KEY_MAX_LENGTH = 8192
    _lock = threading.RLock()

    @classmethod
    def getSettings(cls) -> OpenAICompatibleSettings:
        """現在の非秘密設定を取得する。

        Returns:
            保存済み設定。未作成時は空の既定値。

        Raises:
            OSError: 設定ファイルを読み込めない場合。
            ValueError: JSON または設定 schema が不正な場合。
        """

        with cls._lock:
            if cls.SETTINGS_PATH.is_file() is False:
                return OpenAICompatibleSettings()
            raw = json.loads(cls.SETTINGS_PATH.read_text(encoding='utf-8'))
            if isinstance(raw, dict) is False:
                raise ValueError('OpenAI-compatible settings document is invalid.')
            return OpenAICompatibleSettings.model_validate(raw)

    @classmethod
    def saveSettings(cls, settings: OpenAICompatibleSettings) -> None:
        """非秘密設定を atomic 保存する。

        Args:
            settings: 保存する接続先とモデル。

        Returns:
            None
        """

        with cls._lock:
            validated = OpenAICompatibleSettings.model_validate(settings.model_dump())
            content = json.dumps(validated.model_dump(mode='json'), ensure_ascii=False, indent=4) + '\n'
            cls._writeAtomic(cls.SETTINGS_PATH, content.encode('utf-8'), mode=0o600)

    @classmethod
    def getAPIKey(cls) -> str | None:
        """専用秘密ストアから API キーを取得する。

        Returns:
            保存済み API キー。未作成時は None。

        Raises:
            OSError: 秘密ファイルを読み込めない場合。
            ValueError: 秘密ファイルが上限超過または空の場合。
        """

        with cls._lock:
            if cls.API_KEY_PATH.is_file() is False:
                return None
            if cls.API_KEY_PATH.stat().st_size > cls._API_KEY_MAX_LENGTH + 1:
                raise ValueError('OpenAI-compatible API key is invalid.')
            key = cls.API_KEY_PATH.read_text(encoding='utf-8').strip()
            if key == '' or len(key) > cls._API_KEY_MAX_LENGTH:
                raise ValueError('OpenAI-compatible API key is invalid.')
            return key

    @classmethod
    def getSettingsAndAPIKey(cls) -> tuple[OpenAICompatibleSettings, str | None]:
        """同じ lock 世代の設定と API キーを取得する。

        Returns:
            非秘密設定と API キーの immutable snapshot。
        """

        with cls._lock:
            return cls.getSettings(), cls.getAPIKey()

    @classmethod
    def setAPIKey(cls, api_key: str) -> None:
        """API キーを専用秘密ファイルへ atomic 保存する。

        Args:
            api_key: Bearer 認証へ使用するキー。

        Returns:
            None

        Raises:
            ValueError: キーが空または上限超過の場合。
        """

        key = api_key.strip()
        if key == '' or len(key) > cls._API_KEY_MAX_LENGTH:
            raise ValueError('OpenAI-compatible API key is invalid.')
        with cls._lock:
            cls._writeAtomic(cls.API_KEY_PATH, key.encode('utf-8'), mode=0o600, secret=True)

    @classmethod
    def deleteAPIKey(cls) -> bool:
        """専用秘密ファイルを削除する。

        Returns:
            削除前にファイルが存在した場合は True。
        """

        with cls._lock:
            if cls.API_KEY_PATH.is_file() is False:
                return False
            cls.API_KEY_PATH.unlink()
            cls._fsyncParentDirectory(cls.API_KEY_PATH)
            return True

    @classmethod
    def getSettingsResponse(cls) -> OpenAICompatibleSettingsResponse:
        """秘密本体を含まない API 応答を構築する。

        Returns:
            接続先・モデル・キー設定済みフラグ。
        """

        with cls._lock:
            settings, api_key = cls.getSettingsAndAPIKey()
            return OpenAICompatibleSettingsResponse(
                **settings.model_dump(),
                api_key_configured=api_key is not None,
            )

    @classmethod
    def isConfigured(cls) -> bool:
        """実処理に必要な接続情報がすべて設定済みかを返す。

        Returns:
            URL・モデル・API キーが揃っている場合は True。
        """

        try:
            settings, api_key = cls.getSettingsAndAPIKey()
        except (OSError, ValueError):
            return False
        return (
            settings.api_base_url is not None
            and settings.model is not None
            and api_key is not None
        )

    @classmethod
    def _fsyncParentDirectory(cls, path: Path) -> None:
        """ファイル追加・置換・削除後の親 directory entry を同期する。"""

        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY,
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)

    @classmethod
    def _writeAtomic(
        cls,
        destination: Path,
        content: bytes,
        *,
        mode: int,
        secret: bool = False,
    ) -> None:
        """同一 directory の一時ファイルから atomic に置き換える。

        Args:
            destination: 完成ファイルの保存先。
            content: 書き込む完成済み bytes。
            mode: 完成ファイルの permission。
            secret: 親 directory も 0700 に固定するか。

        Returns:
            None
        """

        destination.parent.mkdir(
            mode=0o700 if secret else 0o755,
            parents=True,
            exist_ok=True,
        )
        if secret:
            os.chmod(destination.parent, 0o700)
        temporary_path = destination.with_name(
            f'.{destination.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp',
        )
        file_descriptor: int | None = None
        try:
            file_descriptor = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                mode,
            )
            written = 0
            while written < len(content):
                written += os.write(file_descriptor, content[written:])
            os.fsync(file_descriptor)
            os.close(file_descriptor)
            file_descriptor = None
            os.chmod(temporary_path, mode)
            os.replace(temporary_path, destination)
            cls._fsyncParentDirectory(destination)
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
