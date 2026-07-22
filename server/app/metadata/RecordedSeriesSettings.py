from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.constants import DATA_DIR


RecordedEpisodeNumberAcceptanceMode = Literal['HighConfidenceOnly', 'Always']


class RecordedSeriesSettings(BaseModel):
    """録画シリーズ判定の永続化される設定。"""

    model_config = ConfigDict(extra='forbid')

    enabled: Annotated[bool, Field()] = True
    ai_enabled: Annotated[bool, Field()] = False
    ai_candidate_selection_enabled: Annotated[bool, Field()] = True
    ai_episode_number_search_enabled: Annotated[bool, Field()] = True
    ai_episode_number_acceptance_mode: Annotated[
        RecordedEpisodeNumberAcceptanceMode,
        Field(),
    ] = 'HighConfidenceOnly'
    api_base_url: Annotated[str, Field(min_length=1, max_length=2048)] = 'https://api.openai.com/v1'
    model: Annotated[str, Field(min_length=1, max_length=255)] = 'gpt-5.6-luna'
    daily_ai_request_limit: Annotated[int, Field(ge=0, le=1000)] = 20

    @field_validator('api_base_url')
    @classmethod
    def validateAPIBaseURL(cls, api_base_url: str) -> str:
        """OpenAI 互換 API のベース URL を正規化する。

        Args:
            api_base_url: Web UI から入力された API のベース URL。

        Returns:
            末尾のスラッシュを取り除いたベース URL。

        Raises:
            ValueError: HTTP/HTTPS URL でないか、認証情報などを URL に含む場合。
        """

        normalized_url = api_base_url.strip().rstrip('/')
        parsed_url = urlsplit(normalized_url)
        if parsed_url.scheme not in ('http', 'https') or parsed_url.hostname is None:
            raise ValueError('API ベース URL には HTTP/HTTPS URL を指定してください。')
        # URL 内の認証情報は設定取得 API やログから漏れるおそれがあるため許可しない。
        if parsed_url.username is not None or parsed_url.password is not None:
            raise ValueError('API ベース URL に認証情報を含めることはできません。')
        if parsed_url.query != '' or parsed_url.fragment != '':
            raise ValueError('API ベース URL にクエリやフラグメントを含めることはできません。')
        return normalized_url

    @field_validator('model')
    @classmethod
    def validateModel(cls, model: str) -> str:
        """OpenAI 互換 API の任意のモデル ID を正規化する。

        Args:
            model: Web UI のプリセットまたは自由入力から受け取ったモデル ID。

        Returns:
            前後の空白を取り除いたモデル ID。

        Raises:
            ValueError: モデル ID が空の場合。
        """

        normalized_model = model.strip()
        if normalized_model == '':
            raise ValueError('モデル ID は空にできません。')
        return normalized_model


class RecordedSeriesSettingsResponse(RecordedSeriesSettings):
    """秘密情報を含まない録画シリーズ判定設定 API レスポンス。"""

    api_key_configured: Annotated[bool, Field()]


class RecordedSeriesProviderKeyMismatchError(ValueError):
    """保存済みキーを異なるprovider URLへ暗黙転送しようとした場合の例外。"""


class RecordedSeriesSettingsStore:
    """録画シリーズ判定設定と API キーを分離して安全に永続化する。"""

    SETTINGS_PATH = DATA_DIR / 'recorded-series-settings.json'
    API_KEY_PATH = DATA_DIR / 'secrets' / 'recorded-series-api.key'

    # 設定と API キーの組を同一プロセス内で一貫して更新するために使う。
    # ルーターの async エンドポイントと Resolver のどちらからも呼べるよう同期 lock にする。
    _lock = threading.RLock()

    @classmethod
    def getSettings(cls) -> RecordedSeriesSettings:
        """現在の録画シリーズ判定設定を取得する。

        Returns:
            永続化済み設定。未作成時はデフォルト設定。

        Raises:
            ValueError: 永続化済み設定が JSON またはスキーマとして不正な場合。
            OSError: 設定ファイルを読み込めない場合。
        """

        with cls._lock:
            if cls.SETTINGS_PATH.is_file() is False:
                return RecordedSeriesSettings()
            settings_json = json.loads(cls.SETTINGS_PATH.read_text(encoding='utf-8'))
            return RecordedSeriesSettings.model_validate(settings_json)

    @classmethod
    def saveSettings(
        cls,
        settings: RecordedSeriesSettings,
        *,
        api_key: str | None = None,
    ) -> None:
        """設定を永続化し、指定時だけ API キーを置き換える。

        Args:
            settings: 永続化するシリーズ判定設定。
            api_key: 置き換える API キー。None の場合は保存済みキーを維持する。

        Returns:
            None

        Raises:
            ValueError: 置き換える API キーが空の場合。
            OSError: 設定または API キーを保存できない場合。
        """

        normalized_api_key = None
        if api_key is not None:
            normalized_api_key = api_key.strip()
            if normalized_api_key == '':
                raise ValueError('API キーは空にできません。')

        with cls._lock:
            if normalized_api_key is None:
                existing_api_key = cls.getAPIKey()
                if existing_api_key is not None:
                    previous_settings = cls.getSettings()
                    if settings.api_base_url != previous_settings.api_base_url:
                        raise RecordedSeriesProviderKeyMismatchError(
                            'API base URL changes require replacing or deleting the saved API key.'
                        )
            # キー更新が失敗したときに新設定だけが有効化されることを避けるため、
            # 機密ファイルを先に保存してから非機密設定を公開する。
            settings_data = json.dumps(
                settings.model_dump(mode='json'),
                ensure_ascii=False,
                indent=4,
            ) + '\n'
            previous_api_key: str | None = None
            api_key_existed = False
            if normalized_api_key is not None and cls.API_KEY_PATH.is_file():
                previous_api_key = cls.API_KEY_PATH.read_text(encoding='utf-8')
                api_key_existed = True
            try:
                if normalized_api_key is not None:
                    cls._writeAtomic(
                        cls.API_KEY_PATH,
                        json.dumps(
                            {
                                'api_base_url': settings.api_base_url,
                                'api_key': normalized_api_key,
                            },
                            ensure_ascii=False,
                        ) + '\n',
                    )
                cls._writeAtomic(cls.SETTINGS_PATH, settings_data)
            except OSError as save_error:
                # 2ファイルをOSレベルで同時replaceはできないため、設定側の保存失敗時は
                # 直前のキーへ戻し「新キー＋旧URL/model」の部分更新を残さない。
                if normalized_api_key is not None:
                    try:
                        if api_key_existed:
                            assert previous_api_key is not None
                            cls._writeAtomic(cls.API_KEY_PATH, previous_api_key)
                        else:
                            try:
                                cls.API_KEY_PATH.unlink()
                            except FileNotFoundError:
                                pass
                    except OSError as rollback_error:
                        raise OSError('Failed to save settings and roll back the API key.') from rollback_error
                raise save_error

    @classmethod
    def getAPIKey(cls) -> str | None:
        """保存済み API キーを Resolver 向けに取得する。

        Returns:
            保存済み API キー。未設定または空ファイルの場合は None。

        Raises:
            OSError: API キーファイルを読み込めない場合。
        """

        with cls._lock:
            settings = cls.getSettings()
            return cls._readAPIKeyForURL(settings.api_base_url)

    @classmethod
    def _readAPIKeyForURL(cls, api_base_url: str) -> str | None:
        """保存時のprovider URLと現設定が一致するキーだけを読み込む。"""

        if cls.API_KEY_PATH.is_file() is False:
            return None
        secret_content = cls.API_KEY_PATH.read_text(encoding='utf-8').strip()
        if secret_content == '':
            return None
        try:
            secret_record = json.loads(secret_content)
        except json.JSONDecodeError as ex:
            raise ValueError('Recorded series API key record is invalid.') from ex
        if (
            not isinstance(secret_record, dict) or
            set(secret_record) != {'api_base_url', 'api_key'} or
            not isinstance(secret_record['api_base_url'], str) or
            not isinstance(secret_record['api_key'], str) or
            secret_record['api_key'].strip() == ''
        ):
            raise ValueError('Recorded series API key record is invalid.')
        if secret_record['api_base_url'] != api_base_url:
            # 2ファイル更新の間にクラッシュしても、新キーを旧providerへ送らない。
            return None
        return secret_record['api_key'].strip()

    @classmethod
    def isAPIKeyConfigured(cls) -> bool:
        """API キーが設定済みかを、キー本体を公開せず取得する。

        Returns:
            非空の API キーが保存されている場合は True。
        """

        return cls.getAPIKey() is not None

    @classmethod
    def getSettingsAndAPIKey(cls) -> tuple[RecordedSeriesSettings, str | None]:
        """判定1回で使用する設定とAPIキーを、同じlock世代の組として取得する。

        Returns:
            同時点の設定とAPIキー。URL更新とキー更新の途中状態は返さない。
        """

        with cls._lock:
            settings = cls.getSettings()
            return settings, cls._readAPIKeyForURL(settings.api_base_url)

    @classmethod
    def deleteAPIKey(cls) -> None:
        """保存済み API キーを削除する。

        Returns:
            None

        Raises:
            OSError: API キーファイルを削除できない場合。
        """

        with cls._lock:
            try:
                cls.API_KEY_PATH.unlink()
            except FileNotFoundError:
                pass

    @classmethod
    def _writeAtomic(cls, destination: Path, content: str) -> None:
        """同一ディレクトリ内でファイルをアトミックに置き換える。

        Args:
            destination: 置き換え先のファイルパス。
            content: UTF-8 で保存する文字列。

        Returns:
            None

        Raises:
            OSError: 一時ファイル作成または置き換えに失敗した場合。
        """

        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination == cls.API_KEY_PATH:
            # mkdirのmodeは既存directoryへ効かないため、group書込によるキー差し替えも明示的に防ぐ。
            os.chmod(destination.parent, 0o700)
        temporary_path = destination.with_name(
            f'.{destination.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp'
        )
        file_descriptor: int | None = None
        try:
            file_descriptor = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(file_descriptor, mode='w', encoding='utf-8') as file:
                file_descriptor = None
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, destination)
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
