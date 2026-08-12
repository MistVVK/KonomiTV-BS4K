"""録画シリーズ判定の固有設定（AI バックエンド選択と EpisodeLookup 設定）。

AI バックエンド接続・秘密・月次上限は AIBackendSettings 側が正本。
OpenCode 時は ai_backend_service_id で AI バックエンド service を参照する。
AcpCodex / AcpGrok のモデル・推論深さ・Fast・タイムアウトは ACPSettings 側が正本。
主系失敗時の予備 AI と失敗時ポリシーもここで管理する。
旧 OpenAICompatible / AcpGemini / 日次制限はクリーンブレークで拒否する。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.constants import DATA_DIR


# AI バックエンド種別。OpenCode に加えて ACP の Codex / Grok を併存させる。
# OpenAICompatible / AcpGemini はクリーンブレークで拒否する。
AIBackendKind = Literal['OpenCode', 'AcpCodex', 'AcpGrok']
# 主系 AI 失敗後の回復方針。既定は追加試行なしの Fail。
AIFailureRecoveryStrategy = Literal['FallbackBackend', 'RetrySameBackend', 'Fail']

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
    re.IGNORECASE,
)

# クリーンブレークで拒否する旧 backend 値（読取時に明示エラーにする）。
_REJECTED_BACKEND_VALUES = frozenset({
    'OpenAICompatible',
    'AcpGemini',
    'AcpCustom',
})
# OpenAICompatible 専用フィールドと日次制限。読取時に検出したら拒否する。
_LEGACY_SETTINGS_KEYS = frozenset({
    'api_base_url',
    'model',
    'daily_ai_request_limit',
})
# 話数 Web 検索は固定動作へ移行した。既存ファイルからは除外し、今後は保存しない。
_REMOVED_EPISODE_SEARCH_SETTINGS_KEYS = frozenset({
    'ai_episode_number_search_enabled',
    'ai_episode_number_acceptance_mode',
})
# 未リリース WIP の旧 home / Symlink / Copy / Custom ACP 設定。読取時のみ除外する。
_LEGACY_ACP_AUTH_SETTINGS_KEYS = frozenset({
    'acp_user_codex_home',
    'acp_user_grok_home',
    'acp_user_gemini_home',
    'acp_auth_share_mode',
})
# ACPSettings へ移行した ACP 実行設定（モデル・推論深さ・Fast・タイムアウト）。
# 旧 recorded-series-settings.json に残っていても読取時に無視する（ACPSettings 側が移行して正本化）。
_LEGACY_ACP_EXECUTION_SETTINGS_KEYS = frozenset({
    'acp_model',
    'acp_reasoning_effort',
    'konomitv_bs4k_acp_codex_fast_mode_enabled',
    'acp_timeout_sec',
})
_LEGACY_ACP_CUSTOM_SETTINGS_KEYS = frozenset({
    'acp_cwd',
    'acp_command',
    'acp_args',
    'acp_env',
})
# 旧 AcpGemini 専用の環境依存値。読取時のみ除外する（OpenCode VertexAdc は AIBackendSettings 側）。
_LEGACY_ACP_GEMINI_SETTINGS_KEYS = frozenset({
    'google_cloud_project',
    'google_cloud_location',
})


class RecordedSeriesSettings(BaseModel):
    """録画シリーズ判定の永続化される設定。"""

    model_config = ConfigDict(extra='forbid')

    enabled: Annotated[bool, Field()] = True
    ai_enabled: Annotated[bool, Field()] = False
    # 旧 JSON の読取互換だけを保つ。保存・API 応答・Resolver 分岐では使用しない。
    ai_candidate_selection_enabled: Annotated[bool, Field(exclude=True)] = True
    # AI バックエンド種別（主系）。OpenCode / AcpCodex / AcpGrok の 3 種のみ。
    ai_backend: Annotated[AIBackendKind, Field()] = 'AcpCodex'
    # 主系が OpenCode のとき参照する AIBackendSettings の service_id（UUID）。
    ai_backend_service_id: Annotated[str | None, Field(max_length=36)] = None
    # 主系失敗後の回復方針。既定 Fail では追加の AI 実行を行わない。
    ai_failure_recovery_strategy: Annotated[AIFailureRecoveryStrategy, Field()] = 'Fail'
    # FallbackBackend 時のみ使う予備 AI バックエンド。
    ai_fallback_backend: Annotated[AIBackendKind | None, Field()] = None
    # 予備が OpenCode のとき参照する service_id（UUID）。
    ai_fallback_backend_service_id: Annotated[str | None, Field(max_length=36)] = None

    @field_validator('ai_backend_service_id', 'ai_fallback_backend_service_id')
    @classmethod
    def validateServiceID(cls, value: str | None) -> str | None:
        """service_id を UUID または None に正規化する。"""

        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized == '':
            return None
        if _UUID_RE.fullmatch(normalized) is None:
            raise ValueError('ai_backend_service_id は UUID である必要があります。')
        return normalized

    @model_validator(mode='after')
    def normalizeBackendCapabilities(self) -> RecordedSeriesSettings:
        """主系・予備系のバックエンド選択と失敗時ポリシーを正規化する。

        OpenCode 時は対応する service_id が必須。
        Fail / RetrySameBackend では予備設定を保持せずクリアする。
        FallbackBackend では主系と予備系が同一であってはならない。
        AcpCodex / AcpGrok のモデル・推論深さ・Fast・タイムアウトは
        ACPSettings 側が正本のため、ここでは検証しない。
        """

        if self.ai_backend == 'OpenCode' and self.ai_enabled and self.ai_backend_service_id is None:
            raise ValueError('OpenCode を利用する場合は ai_backend_service_id が必要です。')

        # ACP 主系では OpenCode service_id を保存しない（誤参照を防ぐ）。
        if self.ai_backend != 'OpenCode':
            self.ai_backend_service_id = None

        strategy = self.ai_failure_recovery_strategy
        if strategy != 'FallbackBackend':
            # 予備は FallbackBackend 選択時だけ意味を持つ。他方針では入力を捨てる。
            self.ai_fallback_backend = None
            self.ai_fallback_backend_service_id = None
            return self

        if self.ai_fallback_backend is None:
            raise ValueError('FallbackBackend を選ぶ場合は ai_fallback_backend が必要です。')
        if (
            self.ai_fallback_backend == 'OpenCode'
            and self.ai_enabled
            and self.ai_fallback_backend_service_id is None
        ):
            raise ValueError(
                '予備 AI に OpenCode を使う場合は ai_fallback_backend_service_id が必要です。',
            )
        if self.ai_fallback_backend != 'OpenCode':
            self.ai_fallback_backend_service_id = None

        # 主系と予備が同じ backend（OpenCode なら同じ service）だと切り替えても意味がない。
        if AreAIBackendTargetsIdentical(
            primary_backend=self.ai_backend,
            primary_service_id=self.ai_backend_service_id,
            fallback_backend=self.ai_fallback_backend,
            fallback_service_id=self.ai_fallback_backend_service_id,
        ):
            raise ValueError('主系 AI と予備 AI は異なるバックエンドである必要があります。')
        return self


def AreAIBackendTargetsIdentical(
    *,
    primary_backend: AIBackendKind,
    primary_service_id: str | None,
    fallback_backend: AIBackendKind | None,
    fallback_service_id: str | None,
) -> bool:
    """主系と予備の実行ターゲットが同一かを返す。

    Args:
        primary_backend: 主系バックエンド種別。
        primary_service_id: 主系 OpenCode service_id。
        fallback_backend: 予備バックエンド種別。
        fallback_service_id: 予備 OpenCode service_id。

    Returns:
        同じ実行主体とみなせる場合は True。
    """

    if fallback_backend is None:
        return False
    if primary_backend != fallback_backend:
        return False
    if primary_backend == 'OpenCode':
        primary = (primary_service_id or '').strip().lower()
        fallback = (fallback_service_id or '').strip().lower()
        return primary != '' and primary == fallback
    # 同じ ACP 種別はプロファイル・モデル設定も共有するため同一とみなす。
    return True


class RecordedSeriesSettingsResponse(RecordedSeriesSettings):
    """録画シリーズ判定設定 API レスポンス。

    旧 api_key_configured は廃止。認証状態は AI バックエンド API を参照する。
    """

    # 参照先 service の表示名（存在しない・未設定時は None）
    ai_backend_service_name: Annotated[str | None, Field()] = None
    # 参照先 service の認証が設定済みか（未参照時は False）
    ai_backend_auth_configured: Annotated[bool, Field()] = False
    # 予備 OpenCode service の表示名（未設定・非 OpenCode 時は None）
    ai_fallback_backend_service_name: Annotated[str | None, Field()] = None
    # 予備 backend の認証が設定済みか（未使用時は False）
    ai_fallback_backend_auth_configured: Annotated[bool, Field()] = False


class RecordedSeriesSettingsStore:
    """録画シリーズ判定設定を安全に永続化する。"""

    SETTINGS_PATH = DATA_DIR / 'recorded-series-settings.json'
    # 旧秘密ファイルパス（読取・書込しない。存在検知用メッセージのみ）
    LEGACY_API_KEY_PATH = DATA_DIR / 'secrets' / 'recorded-series-api.key'

    _lock = threading.RLock()

    @classmethod
    def getSettings(cls) -> RecordedSeriesSettings:
        """現在の録画シリーズ判定設定を取得する。

        Returns:
            永続化済み設定。未作成時はデフォルト設定。

        Raises:
            ValueError: 永続化済み設定が JSON またはスキーマとして不正な場合
                （旧 OpenAICompatible / AcpGemini / 日次制限フィールド検出を含む）。
            OSError: 設定ファイルを読み込めない場合。
        """

        with cls._lock:
            if cls.SETTINGS_PATH.is_file() is False:
                return RecordedSeriesSettings()
            settings_json = json.loads(cls.SETTINGS_PATH.read_text(encoding='utf-8'))
            if isinstance(settings_json, dict) is False:
                raise ValueError('Recorded series settings document is invalid.')
            # OpenAICompatible / AcpGemini / AcpCustom はクリーンブレークで拒否する。
            backend_value = settings_json.get('ai_backend')
            if isinstance(backend_value, str) and backend_value in _REJECTED_BACKEND_VALUES:
                raise ValueError(
                    'Legacy AI backend is not supported: '
                    f'{backend_value}. Delete recorded-series-settings.json and reconfigure.',
                )
            # 旧 ACP 実行設定（ACPSettings へ移行）と、未リリース WIP の旧 home /
            # Symlink / Copy / Custom ACP 設定を保存済み JSON 読取時に除外する。
            # API 入力は extra='forbid' のままにし、旧クライアントの危険な
            # path・command 指定を受理しない。
            settings_json = {
                key: value
                for key, value in settings_json.items()
                if (
                    key not in _LEGACY_ACP_AUTH_SETTINGS_KEYS and
                    key not in _LEGACY_ACP_EXECUTION_SETTINGS_KEYS and
                    key not in _LEGACY_ACP_CUSTOM_SETTINGS_KEYS and
                    key not in _LEGACY_ACP_GEMINI_SETTINGS_KEYS and
                    key not in _REMOVED_EPISODE_SEARCH_SETTINGS_KEYS
                )
            }
            # OpenAICompatible 専用フィールド / 日次制限は拒否する。
            legacy_keys = sorted(key for key in settings_json if key in _LEGACY_SETTINGS_KEYS)
            if legacy_keys:
                raise ValueError(
                    'Legacy recorded-series AI settings are not supported. '
                    'Delete recorded-series-settings.json and reconfigure '
                    f'(legacy keys: {", ".join(legacy_keys)}).',
                )
            return RecordedSeriesSettings.model_validate(settings_json)

    @classmethod
    def saveSettings(cls, settings: RecordedSeriesSettings) -> None:
        """設定を atomic 保存する。

        Args:
            settings: 保存する設定。

        Returns:
            None

        Raises:
            OSError: 書き込み失敗。
            ValueError: 検証失敗。
        """

        with cls._lock:
            validated = RecordedSeriesSettings.model_validate(settings.model_dump())
            # AI を有効にして予備へ切り替える設定は、障害発生時に初めて認証不足が
            # 判明しないよう保存時点で拒否する。AI 無効時は設定順序を妨げない。
            if (
                validated.ai_enabled
                and validated.ai_failure_recovery_strategy == 'FallbackBackend'
                and cls.isFallbackAIBackendConfigured(validated) is False
            ):
                raise ValueError('予備 AI バックエンドの認証が設定されていません。')
            payload = validated.model_dump(mode='json')
            content = json.dumps(payload, ensure_ascii=False, indent=4) + '\n'
            cls._writeAtomic(cls.SETTINGS_PATH, content)

    @classmethod
    def getSettingsAndAPIKey(cls) -> tuple[RecordedSeriesSettings, str | None]:
        """設定を取得する（旧 API キー同梱形式の互換 shim）。

        OpenCode 移行後、録画シリーズ設定は API キーを持たない。
        第 2 要素は常に None。秘密は AIBackendSettingsStore を参照すること。

        Returns:
            (settings, None)
        """

        return cls.getSettings(), None

    @classmethod
    def getSettingsResponse(cls) -> RecordedSeriesSettingsResponse:
        """API 応答用に service 表示情報を付与した設定を返す。"""

        settings = cls.getSettings()
        service_name: str | None = None
        fallback_service_name: str | None = None
        if settings.ai_backend == 'OpenCode' and settings.ai_backend_service_id is not None:
            try:
                from app.metadata.ai.AIBackendSettings import AIBackendSettingsStore
                service = AIBackendSettingsStore.getService(settings.ai_backend_service_id)
                if service is not None:
                    service_name = service.service_name
            except (OSError, ValueError):
                service_name = None
        if (
            settings.ai_fallback_backend == 'OpenCode'
            and settings.ai_fallback_backend_service_id is not None
        ):
            try:
                from app.metadata.ai.AIBackendSettings import AIBackendSettingsStore
                fallback_service = AIBackendSettingsStore.getService(
                    settings.ai_fallback_backend_service_id,
                )
                if fallback_service is not None:
                    fallback_service_name = fallback_service.service_name
            except (OSError, ValueError):
                fallback_service_name = None
        return RecordedSeriesSettingsResponse(
            **settings.model_dump(),
            ai_backend_service_name=service_name,
            ai_backend_auth_configured=cls.isAIBackendConfigured(settings),
            ai_fallback_backend_service_name=fallback_service_name,
            ai_fallback_backend_auth_configured=cls.isFallbackAIBackendConfigured(
                settings,
            ),
        )

    @classmethod
    def isAIBackendConfigured(
        cls,
        settings: RecordedSeriesSettings | None = None,
    ) -> bool:
        """録画シリーズ判定で選択中の AI バックエンドに有効な認証があるかを返す。

        Args:
            settings: 検査する保存済み設定。省略時は設定ストアから取得する。

        Returns:
            OpenCode service または ACP 専用プロファイルを利用できる場合は True。
        """

        effective_settings = settings or cls.getSettings()
        if effective_settings.ai_backend == 'OpenCode':
            if effective_settings.ai_backend_service_id is None:
                return False
            try:
                from app.metadata.ai.AIBackendSettings import AIBackendSettingsStore
                service = AIBackendSettingsStore.getService(
                    effective_settings.ai_backend_service_id,
                )
                return (
                    service is not None and
                    AIBackendSettingsStore.isAuthConfigured(service)
                )
            except (OSError, ValueError):
                return False

        # ACP はホスト側認証の存在だけでは実行せず、KonomiTV-BS4K 専用
        # プロファイルへ取り込み済みで有効な世代だけを設定済みとみなす。
        from app.metadata.ai.KonomiTVBS4KACPCredentials import (
            KonomiTVBS4KACPCredentials,
        )
        provider: Literal['codex', 'grok'] = (
            'codex' if effective_settings.ai_backend == 'AcpCodex' else 'grok'
        )
        return KonomiTVBS4KACPCredentials.getCredentialGeneration(provider) != 'missing'

    @classmethod
    def isFallbackAIBackendConfigured(
        cls,
        settings: RecordedSeriesSettings | None = None,
    ) -> bool:
        """失敗時ポリシーで選択中の予備 AI に有効な認証があるかを返す。

        Args:
            settings: 検査する設定。省略時は設定ストアから取得する。

        Returns:
            FallbackBackend の予備を実行できる認証がある場合は True。
            予備を使用しない設定では False。
        """

        effective_settings = settings or cls.getSettings()
        if effective_settings.ai_failure_recovery_strategy != 'FallbackBackend':
            return False
        fallback_backend = effective_settings.ai_fallback_backend
        if fallback_backend is None:
            return False
        if fallback_backend == 'OpenCode':
            service_id = effective_settings.ai_fallback_backend_service_id
            if service_id is None:
                return False
            try:
                from app.metadata.ai.AIBackendSettings import AIBackendSettingsStore
                service = AIBackendSettingsStore.getService(service_id)
                return (
                    service is not None
                    and AIBackendSettingsStore.isAuthConfigured(service)
                )
            except (OSError, ValueError):
                return False

        from app.metadata.ai.KonomiTVBS4KACPCredentials import (
            KonomiTVBS4KACPCredentials,
        )
        provider: Literal['codex', 'grok'] = (
            'codex' if fallback_backend == 'AcpCodex' else 'grok'
        )
        return KonomiTVBS4KACPCredentials.getCredentialGeneration(provider) != 'missing'

    @classmethod
    def _writeAtomic(cls, destination: Path, content: str) -> None:
        """同一ディレクトリ内でファイルをアトミックに置き換える。"""

        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary_path = destination.with_name(
            f'.{destination.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp',
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
            os.replace(temporary_path, destination)
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
