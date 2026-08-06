"""録画シリーズ判定の固有設定（AI バックエンド種別と EpisodeLookup 設定）。

AI バックエンド接続・秘密・月次上限は AIBackendSettings 側が正本。
OpenCode 時は ai_backend_service_id で AI バックエンド service を参照する。
AcpCodex / AcpGrok は従来どおり ACP フィールドで実行する（Phase 2 以降も併存）。
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


RecordedEpisodeNumberAcceptanceMode = Literal['HighConfidenceOnly', 'Always']
# Phase 1 時点の AI バックエンド種別。OpenCode に加えて ACP の Codex / Grok を併存させる。
# OpenAICompatible / AcpGemini はクリーンブレークで拒否する。
AIBackendKind = Literal['OpenCode', 'AcpCodex', 'AcpGrok']

# ACP 共通の推論深さ。CLI / agent には lowercase で渡す。
# Codex は XHigh/Max/Ultra まで。ただし Ultra は Sol 系統だけで使用する。
# Grok は Low/Medium/High のみ。
AcpReasoningEffort = Literal['Low', 'Medium', 'High', 'XHigh', 'Max', 'Ultra']
_ACP_REASONING_EFFORT_FROM_LOWER: dict[str, AcpReasoningEffort] = {
    'low': 'Low',
    'medium': 'Medium',
    'high': 'High',
    'xhigh': 'XHigh',
    'max': 'Max',
    'ultra': 'Ultra',
}
# 旧 UI の連結 ID（gpt-5.6-luna[medium]）を model + effort に分解する。
_ACP_COMPOSITE_MODEL_RE = re.compile(
    r'^(?P<model>[^\[\]]+?)(?:\[(?P<effort>low|medium|high|xhigh|max|ultra)\])?$',
    re.IGNORECASE,
)
_ACP_BASIC_REASONING_EFFORTS: frozenset[AcpReasoningEffort] = frozenset({
    'Low',
    'Medium',
    'High',
})
# backend ごとの未設定時デフォルト（CLI 既定ではなく明示プリセット）。
_ACP_DEFAULT_MODEL_BY_BACKEND: dict[str, str | None] = {
    'AcpCodex': 'gpt-5.6-luna',
    'AcpGrok': None,
}
_ACP_DEFAULT_REASONING_EFFORT_BY_BACKEND: dict[str, AcpReasoningEffort] = {
    'AcpCodex': 'Medium',
    'AcpGrok': 'High',
}

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
# 未リリース WIP の旧 home / Symlink / Copy / Custom ACP 設定。読取時のみ除外する。
_LEGACY_ACP_AUTH_SETTINGS_KEYS = frozenset({
    'acp_user_codex_home',
    'acp_user_grok_home',
    'acp_user_gemini_home',
    'acp_auth_share_mode',
})
_LEGACY_ACP_CUSTOM_SETTINGS_KEYS = frozenset({
    'acp_cwd',
    'acp_command',
    'acp_args',
    'acp_env',
})


def _isKonomiTVBS4KCodexSolModel(konomitv_bs4k_model: str | None) -> bool:
    """Codex のモデル ID が Sol 系統かを判定する。"""

    if konomitv_bs4k_model is None:
        return False
    konomitv_bs4k_normalized_model = konomitv_bs4k_model.strip().lower()
    return (
        konomitv_bs4k_normalized_model == 'sol' or
        konomitv_bs4k_normalized_model.endswith('-sol')
    )


def ParseAcpCompositeModel(
    raw_model: str | None,
) -> tuple[str | None, AcpReasoningEffort | None]:
    """連結モデル ID（model[effort]）を model と effort に分解する。

    Args:
        raw_model: UI または旧設定のモデル文字列。

    Returns:
        (model, effort)。effort 括弧が無い場合は effort は None。
        空文字は (None, None)。

    Raises:
        ValueError: 角括弧の形が不正な場合。
    """

    if raw_model is None:
        return None, None
    trimmed = raw_model.strip()
    if trimmed == '':
        return None, None
    matched = _ACP_COMPOSITE_MODEL_RE.fullmatch(trimmed)
    if matched is None:
        raise ValueError(
            'ACP モデル ID の形式が不正です。'
            'モデル名、または model[effort] 形式で指定してください。'
        )
    model = matched.group('model').strip()
    if model == '':
        return None, None
    effort_raw = matched.group('effort')
    if effort_raw is None:
        return model, None
    effort = _ACP_REASONING_EFFORT_FROM_LOWER.get(effort_raw.lower())
    if effort is None:
        raise ValueError('ACP 推論深さの値が不正です。')
    return model, effort


class RecordedSeriesSettings(BaseModel):
    """録画シリーズ判定の永続化される設定。"""

    model_config = ConfigDict(extra='forbid')

    enabled: Annotated[bool, Field()] = True
    ai_enabled: Annotated[bool, Field()] = False
    # 旧 JSON の読取互換だけを保つ。保存・API 応答・Resolver 分岐では使用しない。
    ai_candidate_selection_enabled: Annotated[bool, Field(exclude=True)] = True
    ai_episode_number_search_enabled: Annotated[bool, Field()] = False
    ai_episode_number_acceptance_mode: Annotated[
        RecordedEpisodeNumberAcceptanceMode,
        Field(),
    ] = 'Always'
    # AI バックエンド種別。OpenCode / AcpCodex / AcpGrok の 3 種のみ。
    ai_backend: Annotated[AIBackendKind, Field()] = 'AcpCodex'
    # OpenCode 時のみ参照する AIBackendSettings の service_id（UUID）。
    ai_backend_service_id: Annotated[str | None, Field(max_length=36)] = None

    # ACP 共通（モデル名と推論深さは分離して保存する）
    acp_model: Annotated[str | None, Field(max_length=255)] = None
    # Codex: Low…Ultra / Grok: Low…High。backend ごとの default は model_validator で埋める。
    acp_reasoning_effort: Annotated[AcpReasoningEffort | None, Field()] = None
    # KonomiTV-BS4K 固有。Codex の専用 profile へ Fast service tier を設定する。
    konomitv_bs4k_acp_codex_fast_mode_enabled: Annotated[bool, Field()] = False
    # 壁時計の総実行上限ではなく、ACP stdio の無通信打ち切り秒数。
    # thought / tool update などの NDJSON 行が届くたびにタイマーはリセットされる。
    acp_timeout_sec: Annotated[int, Field(ge=30, le=600)] = 120

    # Gemini CLI / Vertex AI 用（資格情報ではない環境依存設定）。
    # AcpGemini はクリーンブレークで拒否済みだが、旧設定読取互換のため Phase 6 まで維持する。
    google_cloud_project: Annotated[str | None, Field(max_length=255)] = None
    google_cloud_location: Annotated[str | None, Field(max_length=255)] = None

    @field_validator('ai_backend_service_id')
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

    @field_validator('acp_model')
    @classmethod
    def validateAcpModel(cls, model: str | None) -> str | None:
        """ACP モデル ID の空文字を未指定へ正規化する。

        連結 ID の分解は model_validator 側で行い、ここは前後空白のみ整える。
        """

        if model is None:
            return None
        return model.strip() or None

    @field_validator('google_cloud_project', 'google_cloud_location')
    @classmethod
    def validateGoogleCloudSetting(cls, value: str | None) -> str | None:
        """Google Cloud の環境依存値を空文字から未指定へ正規化する。"""

        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode='after')
    def normalizeBackendCapabilities(self) -> RecordedSeriesSettings:
        """ACP 能力とプリセットごとの固定実行経路を正規化する。

        OpenCode 時は ACP の model / effort / Fast tier を使わず、service_id が必須。
        AcpCodex / AcpGrok は従来どおりの ACP 正規化を適用する。
        """

        if self.ai_backend == 'OpenCode':
            # OpenCode では ACP の model / effort / Fast service tier を持ち越さない。
            self.acp_model = None
            self.acp_reasoning_effort = None
            self.konomitv_bs4k_acp_codex_fast_mode_enabled = False
            if self.ai_enabled and self.ai_backend_service_id is None:
                raise ValueError('OpenCode を利用する場合は ai_backend_service_id が必要です。')
            return self

        if self.ai_backend != 'AcpCodex':
            # Fast service tier は Codex 専用で、他 provider へ持ち越さない。
            self.konomitv_bs4k_acp_codex_fast_mode_enabled = False

        # 旧 UI の連結 ID を model + effort に分解する。
        # 明示された acp_reasoning_effort がある場合は括弧側より優先する。
        if self.acp_model is not None:
            bare_model, embedded_effort = ParseAcpCompositeModel(self.acp_model)
            self.acp_model = bare_model
            if self.acp_reasoning_effort is None and embedded_effort is not None:
                self.acp_reasoning_effort = embedded_effort

        if self.ai_backend == 'AcpGrok':
            # Grok Build の ACP は grok-4.5 固定。モデル ID は保存せず深さだけを持つ。
            self.acp_model = None
            if self.acp_reasoning_effort is None:
                self.acp_reasoning_effort = _ACP_DEFAULT_REASONING_EFFORT_BY_BACKEND['AcpGrok']
            if self.acp_reasoning_effort not in _ACP_BASIC_REASONING_EFFORTS:
                raise ValueError('Grok の推論深さは Low / Medium / High のみです。')
        elif self.ai_backend == 'AcpCodex':
            if self.acp_model is None:
                self.acp_model = _ACP_DEFAULT_MODEL_BY_BACKEND['AcpCodex']
            if self.acp_reasoning_effort is None:
                self.acp_reasoning_effort = _ACP_DEFAULT_REASONING_EFFORT_BY_BACKEND['AcpCodex']
            if (
                self.acp_reasoning_effort == 'Ultra' and
                _isKonomiTVBS4KCodexSolModel(self.acp_model) is False
            ):
                # 旧保存値や直接 API 入力も、非 Sol では Max へ安全に補正する。
                self.acp_reasoning_effort = 'Max'

        return self


class RecordedSeriesSettingsResponse(RecordedSeriesSettings):
    """録画シリーズ判定設定 API レスポンス。

    旧 api_key_configured は廃止。認証状態は AI バックエンド API を参照する。
    """

    # 参照先 service の表示名（存在しない・未設定時は None）
    ai_backend_service_name: Annotated[str | None, Field()] = None
    # 参照先 service の認証が設定済みか（未参照時は False）
    ai_backend_auth_configured: Annotated[bool, Field()] = False


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
            # 未リリース WIP の旧 home / Symlink / Copy / Custom ACP 設定だけを
            # 保存済み JSON 読取時に除外する。API 入力は extra='forbid' のままにし、
            # 旧クライアントの危険な path・command 指定を受理しない。
            settings_json = {
                key: value
                for key, value in settings_json.items()
                if (
                    key not in _LEGACY_ACP_AUTH_SETTINGS_KEYS and
                    key not in _LEGACY_ACP_CUSTOM_SETTINGS_KEYS
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
        auth_configured = False
        if settings.ai_backend == 'OpenCode' and settings.ai_backend_service_id is not None:
            try:
                from app.metadata.ai.AIBackendSettings import AIBackendSettingsStore
                service = AIBackendSettingsStore.getService(settings.ai_backend_service_id)
                if service is not None:
                    service_name = service.service_name
                    auth_configured = AIBackendSettingsStore.isAuthConfigured(service)
            except (OSError, ValueError):
                service_name = None
                auth_configured = False
        return RecordedSeriesSettingsResponse(
            **settings.model_dump(),
            ai_backend_service_name=service_name,
            ai_backend_auth_configured=auth_configured,
        )

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
