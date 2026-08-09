"""独立 AI バックエンド設定と秘密ストア（OpenCode 専用・クリーンブレーク）。

保存先:
- DATA_DIR/ai-backend-settings.json … service 定義（非秘密）
- DATA_DIR/secrets/ai-api-keys.json … service_id → API キー（0600・atomic・非返却）

旧 OpenAICompatible / ACP 設定からのマイグレーションは行わない。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.constants import DATA_DIR


AIBackendKind = Literal['OpenCode']
AIAuthMode = Literal['ApiKey', 'OAuthSubscription', 'VertexAdc', 'NoneLocal']
AIBillingMode = Literal['Metered', 'Subscription', 'Local']
KonomiTVBS4KOpenCodeProviderType = Literal['Catalog', 'OpenAICompatible', 'AnthropicCompatible']
KonomiTVBS4KStructuredOutputMode = Literal['Auto', 'StructuredOutput', 'JSONText']

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
    re.IGNORECASE,
)
_PROVIDER_ID_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')
_MODEL_VARIANT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
_LEGACY_BACKEND_VALUES = frozenset({
    'OpenAICompatible',
    'AcpCodex',
    'AcpGrok',
    'AcpGemini',
    'AcpCustom',
    'LiteLLM',
})


def NormalizeMonthlyCostLimitInput(value: object) -> object:
    """月次料金上限の入力を正規化する。空文字・0 は上限なし (None)。"""

    if value is None or value == '':
        return None
    try:
        as_decimal = Decimal(str(value))
    except Exception:
        return value
    if as_decimal == 0:
        return None
    return value


def NormalizeMonthlyTokenLimitInput(value: object) -> object:
    """月次 token 上限の入力を正規化する。空文字・0 は上限なし (None)。"""

    if value is None or value == '':
        return None
    try:
        as_int = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return value
    if as_int == 0:
        return None
    return value


def NewAIBackendServiceID() -> str:
    """新しい service_id（UUID4）を発行する。

    Returns:
        小文字 UUID 文字列。
    """

    return str(uuid.uuid4())


def BuildKonomiTVBS4KOpenCodeProviderID(service_id: str) -> str:
    """カスタム provider に割り当てる service 固有 OpenCode provider ID を返す。

    Args:
        service_id: AI バックエンド service UUID。

    Returns:
        OpenCode 設定と auth API で共用する provider ID。
    """

    normalized_service_id = service_id.strip().lower()
    if _UUID_RE.fullmatch(normalized_service_id) is None:
        raise ValueError('service_id は UUID である必要があります。')
    return f'konomitv-bs4k-{normalized_service_id}'


def IsKonomiTVBS4KManagedOpenCodeProviderID(provider_id: str) -> bool:
    """KonomiTV-BS4K が生成したカスタム provider ID かを返す。

    Args:
        provider_id: 判定する OpenCode provider ID。

    Returns:
        管理対象 ID なら True。
    """

    normalized_provider_id = provider_id.strip().lower()
    prefix = 'konomitv-bs4k-'
    if normalized_provider_id.startswith(prefix) is False:
        return False
    return _UUID_RE.fullmatch(normalized_provider_id[len(prefix):]) is not None


def NormalizeAPIBaseURL(api_base_url: str) -> str:
    """OpenAI / Anthropic 互換カスタム base URL を正規化する。

    Args:
        api_base_url: 入力 URL。

    Returns:
        末尾スラッシュなしの HTTP/HTTPS URL。

    Raises:
        ValueError: 形式不正。
    """

    normalized_url = api_base_url.strip().rstrip('/')
    parsed_url = urlsplit(normalized_url)
    if parsed_url.scheme not in ('http', 'https') or parsed_url.hostname is None:
        raise ValueError('API ベース URL には HTTP/HTTPS URL を指定してください。')
    if parsed_url.username is not None or parsed_url.password is not None:
        raise ValueError('API ベース URL に認証情報を含めることはできません。')
    if parsed_url.query != '' or parsed_url.fragment != '':
        raise ValueError('API ベース URL にクエリやフラグメントを含めることはできません。')
    try:
        port = parsed_url.port
    except ValueError as error:
        raise ValueError('API ベース URL のポート番号が不正です。') from error
    if port is not None and (port < 1 or port > 65535):
        raise ValueError('API ベース URL のポート番号は 1 以上 65535 以下である必要があります。')
    return normalized_url


class AIBackendService(BaseModel):
    """1 件の OpenCode service 定義（秘密を含まない）。"""

    model_config = ConfigDict(extra='forbid')

    service_id: Annotated[str, Field(min_length=36, max_length=36)]
    service_name: Annotated[str, Field(min_length=1, max_length=128)]
    # OpenCode 以外は拒否（クリーンブレーク）
    backend_kind: Annotated[AIBackendKind, Field()] = 'OpenCode'
    # Catalog は OpenCode 標準カタログ、残り2種は KonomiTV-BS4K 管理のカスタム provider。
    opencode_provider_type: Annotated[KonomiTVBS4KOpenCodeProviderType, Field()] = 'Catalog'
    opencode_provider_id: Annotated[str, Field(min_length=1, max_length=64)]
    opencode_model_id: Annotated[str, Field(min_length=1, max_length=255)]
    # OpenCode がモデルごとに広告する variant。推論モデルでは思考の深さに対応する。
    opencode_model_variant: Annotated[str | None, Field(max_length=64)] = None
    # OpenCode の StructuredOutput tool と JSON text の選択方式。話数 Web 検索にも適用する。
    structured_output_mode: Annotated[KonomiTVBS4KStructuredOutputMode, Field()] = 'Auto'
    auth_mode: Annotated[AIAuthMode, Field()]
    billing_mode: Annotated[AIBillingMode, Field()]
    api_base_url: Annotated[str | None, Field(max_length=2048)] = None
    google_cloud_project: Annotated[str | None, Field(max_length=255)] = None
    google_cloud_location: Annotated[str | None, Field(max_length=255)] = None
    # 0 は UI「空欄=なし」と衝突するため validator で None へ正規化する（上限なし）。
    monthly_cost_limit_usd: Annotated[Decimal | None, Field(ge=0, le=1_000_000)] = None
    monthly_token_limit: Annotated[int | None, Field(ge=0, le=10_000_000_000)] = None
    # OAuth 接続済みフラグ（token 本体は OpenCode auth 側。KonomiTV は状態だけ）
    oauth_connected: Annotated[bool, Field()] = False

    @field_validator('monthly_cost_limit_usd', mode='before')
    @classmethod
    def normalizeMonthlyCostLimit(cls, value: object) -> object:
        """空文字・0 を上限なし (None) にする。"""

        return NormalizeMonthlyCostLimitInput(value)

    @field_validator('monthly_token_limit', mode='before')
    @classmethod
    def normalizeMonthlyTokenLimit(cls, value: object) -> object:
        """空文字・0 を上限なし (None) にする。"""

        return NormalizeMonthlyTokenLimitInput(value)

    @field_validator('service_id')
    @classmethod
    def validateServiceID(cls, value: str) -> str:
        """service_id を UUID として検証する。"""

        normalized = value.strip().lower()
        if _UUID_RE.fullmatch(normalized) is None:
            raise ValueError('service_id は UUID である必要があります。')
        return normalized

    @field_validator('service_name')
    @classmethod
    def validateServiceName(cls, value: str) -> str:
        """表示名の前後空白を除去する。"""

        normalized = value.strip()
        if normalized == '':
            raise ValueError('service_name は空にできません。')
        return normalized

    @field_validator('backend_kind')
    @classmethod
    def validateBackendKind(cls, value: str) -> str:
        """OpenCode 以外を拒否する。"""

        if value in _LEGACY_BACKEND_VALUES:
            raise ValueError('旧 AI バックエンド種別はサポートされていません。OpenCode のみ利用できます。')
        if value != 'OpenCode':
            raise ValueError('AI バックエンド種別は OpenCode のみです。')
        return value

    @field_validator('opencode_provider_id')
    @classmethod
    def validateProviderID(cls, value: str) -> str:
        """provider ID を小文字スラッグとして検証する。"""

        normalized = value.strip().lower()
        if _PROVIDER_ID_RE.fullmatch(normalized) is None:
            raise ValueError('opencode_provider_id の形式が不正です。')
        return normalized

    @field_validator('opencode_model_id')
    @classmethod
    def validateModelID(cls, value: str) -> str:
        """model ID の前後空白を除去する。"""

        normalized = value.strip()
        if normalized == '':
            raise ValueError('opencode_model_id は空にできません。')
        return normalized

    @field_validator('opencode_model_variant')
    @classmethod
    def validateModelVariant(cls, value: str | None) -> str | None:
        """OpenCode model variant を安全な識別子として正規化する。"""

        if value is None:
            return None
        normalized = value.strip()
        if normalized == '':
            return None
        if _MODEL_VARIANT_RE.fullmatch(normalized) is None:
            raise ValueError('opencode_model_variant の形式が不正です。')
        return normalized

    @field_validator('api_base_url')
    @classmethod
    def validateAPIBaseURL(cls, value: str | None) -> str | None:
        """api_base_url を正規化する。"""

        if value is None:
            return None
        trimmed = value.strip()
        if trimmed == '':
            return None
        return NormalizeAPIBaseURL(trimmed)

    @field_validator('google_cloud_project', 'google_cloud_location')
    @classmethod
    def validateOptionalCloudString(cls, value: str | None) -> str | None:
        """空文字を None にする。"""

        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None

    @model_validator(mode='after')
    def validateModeConsistency(self) -> AIBackendService:
        """provider 種別 / auth_mode / billing_mode / Vertex 条件を検証する。"""

        if self.opencode_provider_type == 'Catalog':
            if IsKonomiTVBS4KManagedOpenCodeProviderID(self.opencode_provider_id):
                raise ValueError('Catalog では管理対象カスタム provider ID を指定できません。')
        else:
            expected_provider_id = BuildKonomiTVBS4KOpenCodeProviderID(self.service_id)
            if self.opencode_provider_id != expected_provider_id:
                raise ValueError('カスタム provider ID が service_id と一致しません。')
            if self.api_base_url is None:
                raise ValueError('カスタム provider では api_base_url が必須です。')
            if self.auth_mode not in {'ApiKey', 'NoneLocal'}:
                raise ValueError('カスタム provider の認証方式は ApiKey または NoneLocal です。')

        if self.auth_mode == 'VertexAdc':
            if self.google_cloud_project is None:
                raise ValueError('VertexAdc では google_cloud_project が必須です。')
            if self.google_cloud_location is None:
                self.google_cloud_location = 'global'
            if self.billing_mode == 'Local':
                raise ValueError('VertexAdc では billing_mode=Local を指定できません。')

        if self.auth_mode == 'NoneLocal':
            if self.billing_mode not in {'Local', 'Metered'}:
                raise ValueError('NoneLocal の billing_mode は Local または Metered です。')
            if self.api_base_url is None:
                raise ValueError('NoneLocal では api_base_url が必須です。')

        if self.auth_mode == 'OAuthSubscription' and self.billing_mode != 'Subscription':
            raise ValueError('OAuthSubscription では billing_mode=Subscription が必要です。')

        if self.auth_mode == 'ApiKey' and self.billing_mode == 'Subscription':
            # API キー従量を Subscription 扱いにはしない（台帳分岐を壊す）
            raise ValueError('ApiKey では billing_mode=Subscription を指定できません。')

        if self.billing_mode != 'Metered':
            # 料金上限は Metered のみ意味を持つ
            if self.billing_mode == 'Subscription':
                self.monthly_cost_limit_usd = None
                self.monthly_token_limit = None
            elif self.billing_mode == 'Local':
                self.monthly_cost_limit_usd = None

        if self.auth_mode != 'OAuthSubscription':
            self.oauth_connected = False

        return self

    def getAuditModelLabel(self) -> str:
        """provider・model・variant を含む監査ラベルを返す。

        Returns:
            OpenCode の実行条件を識別できる監査ラベル。
        """

        label = f'opencode:{self.opencode_provider_id}/{self.opencode_model_id}'
        if self.opencode_model_variant is not None:
            return f'{label}[{self.opencode_model_variant}]'
        return label


class AIBackendServiceCreate(BaseModel):
    """service 作成リクエスト（service_id はサーバー発行）。"""

    model_config = ConfigDict(extra='forbid')

    service_name: Annotated[str, Field(min_length=1, max_length=128)]
    opencode_provider_type: Annotated[KonomiTVBS4KOpenCodeProviderType, Field()] = 'Catalog'
    opencode_provider_id: Annotated[str | None, Field(min_length=1, max_length=64)] = None
    opencode_model_id: Annotated[str, Field(min_length=1, max_length=255)]
    opencode_model_variant: Annotated[str | None, Field(max_length=64)] = None
    structured_output_mode: Annotated[KonomiTVBS4KStructuredOutputMode, Field()] = 'Auto'
    auth_mode: Annotated[AIAuthMode, Field()]
    billing_mode: Annotated[AIBillingMode, Field()]
    api_base_url: Annotated[str | None, Field(max_length=2048)] = None
    google_cloud_project: Annotated[str | None, Field(max_length=255)] = None
    google_cloud_location: Annotated[str | None, Field(max_length=255)] = None
    monthly_cost_limit_usd: Annotated[Decimal | None, Field(ge=0, le=1_000_000)] = None
    monthly_token_limit: Annotated[int | None, Field(ge=0, le=10_000_000_000)] = None

    @field_validator('monthly_cost_limit_usd', mode='before')
    @classmethod
    def normalizeMonthlyCostLimit(cls, value: object) -> object:
        return NormalizeMonthlyCostLimitInput(value)

    @field_validator('monthly_token_limit', mode='before')
    @classmethod
    def normalizeMonthlyTokenLimit(cls, value: object) -> object:
        return NormalizeMonthlyTokenLimitInput(value)


class AIBackendServiceUpdate(BaseModel):
    """service 更新リクエスト（未指定フィールドは維持）。"""

    model_config = ConfigDict(extra='forbid')

    service_name: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    opencode_provider_type: Annotated[KonomiTVBS4KOpenCodeProviderType | None, Field()] = None
    opencode_provider_id: Annotated[str | None, Field(min_length=1, max_length=64)] = None
    opencode_model_id: Annotated[str | None, Field(min_length=1, max_length=255)] = None
    opencode_model_variant: Annotated[str | None, Field(max_length=64)] = None
    structured_output_mode: Annotated[KonomiTVBS4KStructuredOutputMode | None, Field()] = None
    auth_mode: Annotated[AIAuthMode | None, Field()] = None
    billing_mode: Annotated[AIBillingMode | None, Field()] = None
    api_base_url: Annotated[str | None, Field(max_length=2048)] = None
    google_cloud_project: Annotated[str | None, Field(max_length=255)] = None
    google_cloud_location: Annotated[str | None, Field(max_length=255)] = None
    monthly_cost_limit_usd: Annotated[Decimal | None, Field(ge=0, le=1_000_000)] = None
    monthly_token_limit: Annotated[int | None, Field(ge=0, le=10_000_000_000)] = None
    # True のとき opencode_model_variant を明示的に null へ
    clear_opencode_model_variant: Annotated[bool, Field()] = False
    # True のとき api_base_url を明示的に null へ
    clear_api_base_url: Annotated[bool, Field()] = False
    clear_monthly_cost_limit_usd: Annotated[bool, Field()] = False
    clear_monthly_token_limit: Annotated[bool, Field()] = False

    @field_validator('monthly_cost_limit_usd', mode='before')
    @classmethod
    def normalizeMonthlyCostLimit(cls, value: object) -> object:
        return NormalizeMonthlyCostLimitInput(value)

    @field_validator('monthly_token_limit', mode='before')
    @classmethod
    def normalizeMonthlyTokenLimit(cls, value: object) -> object:
        return NormalizeMonthlyTokenLimitInput(value)


class AIBackendServiceResponse(AIBackendService):
    """API 応答用。キー本体は返さず設定済みフラグのみ。"""

    auth_configured: Annotated[bool, Field()]
    # EpisodeLookup proof は Phase 4。ここでは常に False を返す土台。
    episode_lookup_ready: Annotated[bool, Field()] = False


class AIBackendSettingsDocument(BaseModel):
    """ai-backend-settings.json のルート。"""

    model_config = ConfigDict(extra='forbid')

    version: Annotated[int, Field(ge=1, le=1)] = 1
    services: list[AIBackendService] = Field(default_factory=list)

    @model_validator(mode='after')
    def validateUniqueIDs(self) -> AIBackendSettingsDocument:
        """service_id の一意性を検証する。"""

        seen: set[str] = set()
        for service in self.services:
            if service.service_id in seen:
                raise ValueError('service_id が重複しています。')
            seen.add(service.service_id)
        return self


class AIBackendSettingsStore:
    """AI バックエンド設定と API キー秘密を分離して永続化する。"""

    SETTINGS_PATH = DATA_DIR / 'ai-backend-settings.json'
    SECRETS_PATH = DATA_DIR / 'secrets' / 'ai-api-keys.json'

    _SECRETS_MAX_BYTES = 256 * 1024
    _SECRETS_MAX_ENTRIES = 64
    _API_KEY_MAX_LENGTH = 8192
    _lock = threading.RLock()

    @classmethod
    def getDocument(cls) -> AIBackendSettingsDocument:
        """設定ドキュメントを取得する。

        Returns:
            永続化済み設定。未作成時は空ドキュメント。

        Raises:
            ValueError: JSON/スキーマ不正（旧形式含む）。
            OSError: 読込失敗。
        """

        with cls._lock:
            if cls.SETTINGS_PATH.is_file() is False:
                return AIBackendSettingsDocument()
            raw = json.loads(cls.SETTINGS_PATH.read_text(encoding='utf-8'))
            if isinstance(raw, dict) is False:
                raise ValueError('AI backend settings document is invalid.')
            # 旧 backend 文字列が残っていたらクリーンブレークで拒否
            services = raw.get('services')
            if isinstance(services, list):
                for item in services:
                    if isinstance(item, dict) is False:
                        continue
                    kind = item.get('backend_kind') or item.get('ai_backend')
                    if kind in _LEGACY_BACKEND_VALUES:
                        raise ValueError(
                            'Legacy AI backend settings are not supported. '
                            'Delete ai-backend-settings.json and re-register services.',
                        )
            return AIBackendSettingsDocument.model_validate(raw)

    @classmethod
    def listServices(cls) -> list[AIBackendService]:
        """service 一覧を返す。"""

        return list(cls.getDocument().services)

    @classmethod
    def getService(cls, service_id: str) -> AIBackendService | None:
        """service_id で 1 件取得する。"""

        normalized = service_id.strip().lower()
        for service in cls.listServices():
            if service.service_id == normalized:
                return service
        return None

    @classmethod
    def saveDocument(cls, document: AIBackendSettingsDocument) -> None:
        """設定ドキュメントを atomic 保存する。"""

        with cls._lock:
            payload = document.model_dump(mode='json')
            content = json.dumps(payload, ensure_ascii=False, indent=4) + '\n'
            cls._writeAtomic(cls.SETTINGS_PATH, content)

    @classmethod
    def createService(cls, create: AIBackendServiceCreate) -> AIBackendService:
        """service を追加する。"""

        with cls._lock:
            document = cls.getDocument()
            service_id = NewAIBackendServiceID()
            provider_id = create.opencode_provider_id
            if create.opencode_provider_type != 'Catalog':
                provider_id = BuildKonomiTVBS4KOpenCodeProviderID(service_id)
            elif provider_id is None:
                raise ValueError('Catalog provider では opencode_provider_id が必須です。')
            service = AIBackendService(
                service_id=service_id,
                service_name=create.service_name,
                opencode_provider_type=create.opencode_provider_type,
                opencode_provider_id=provider_id,
                opencode_model_id=create.opencode_model_id,
                opencode_model_variant=create.opencode_model_variant,
                structured_output_mode=create.structured_output_mode,
                auth_mode=create.auth_mode,
                billing_mode=create.billing_mode,
                api_base_url=create.api_base_url,
                google_cloud_project=create.google_cloud_project,
                google_cloud_location=create.google_cloud_location,
                monthly_cost_limit_usd=create.monthly_cost_limit_usd,
                monthly_token_limit=create.monthly_token_limit,
            )
            document.services.append(service)
            # 再検証（一意性）
            document = AIBackendSettingsDocument.model_validate(document.model_dump())
            cls.saveDocument(document)
            return service

    @classmethod
    def updateService(cls, service_id: str, update: AIBackendServiceUpdate) -> AIBackendService:
        """service を更新する。

        Raises:
            KeyError: 存在しない。
            ValueError: 検証失敗。
        """

        with cls._lock:
            document = cls.getDocument()
            normalized = service_id.strip().lower()
            index = next(
                (i for i, item in enumerate(document.services) if item.service_id == normalized),
                None,
            )
            if index is None:
                raise KeyError(normalized)
            current = document.services[index]
            data = current.model_dump()
            if update.service_name is not None:
                data['service_name'] = update.service_name
            if update.opencode_provider_type is not None:
                data['opencode_provider_type'] = update.opencode_provider_type
            if update.opencode_provider_id is not None:
                data['opencode_provider_id'] = update.opencode_provider_id
            if update.opencode_model_id is not None:
                data['opencode_model_id'] = update.opencode_model_id
            if update.clear_opencode_model_variant:
                data['opencode_model_variant'] = None
            elif update.opencode_model_variant is not None:
                data['opencode_model_variant'] = update.opencode_model_variant
            if update.structured_output_mode is not None:
                data['structured_output_mode'] = update.structured_output_mode
            if update.auth_mode is not None:
                data['auth_mode'] = update.auth_mode
            if update.billing_mode is not None:
                data['billing_mode'] = update.billing_mode
            if update.clear_api_base_url:
                data['api_base_url'] = None
            elif update.api_base_url is not None:
                data['api_base_url'] = update.api_base_url
            if update.google_cloud_project is not None:
                data['google_cloud_project'] = update.google_cloud_project
            if update.google_cloud_location is not None:
                data['google_cloud_location'] = update.google_cloud_location
            if update.clear_monthly_cost_limit_usd:
                data['monthly_cost_limit_usd'] = None
            elif update.monthly_cost_limit_usd is not None:
                data['monthly_cost_limit_usd'] = update.monthly_cost_limit_usd
            if update.clear_monthly_token_limit:
                data['monthly_token_limit'] = None
            elif update.monthly_token_limit is not None:
                data['monthly_token_limit'] = update.monthly_token_limit

            # カスタム provider ID は利用者入力にせず service_id から一意に導出する。
            if data['opencode_provider_type'] != 'Catalog':
                data['opencode_provider_id'] = BuildKonomiTVBS4KOpenCodeProviderID(current.service_id)
            updated = AIBackendService.model_validate(data)
            document.services[index] = updated
            document = AIBackendSettingsDocument.model_validate(document.model_dump())
            cls.saveDocument(document)
            return updated

    @classmethod
    def deleteService(cls, service_id: str) -> AIBackendService:
        """settings から service を削除し、secrets も消す（OpenCode auth は呼び出し側）。

        Raises:
            KeyError: 存在しない。
        """

        with cls._lock:
            document = cls.getDocument()
            normalized = service_id.strip().lower()
            index = next(
                (i for i, item in enumerate(document.services) if item.service_id == normalized),
                None,
            )
            if index is None:
                raise KeyError(normalized)
            removed = document.services.pop(index)
            cls.saveDocument(document)
            cls.deleteAPIKey(normalized)
            return removed

    @classmethod
    def setOAuthConnected(cls, service_id: str, connected: bool) -> AIBackendService:
        """OAuth 接続フラグだけを更新する。"""

        with cls._lock:
            document = cls.getDocument()
            normalized = service_id.strip().lower()
            for index, service in enumerate(document.services):
                if service.service_id != normalized:
                    continue
                data = service.model_dump()
                data['oauth_connected'] = connected
                updated = AIBackendService.model_validate(data)
                document.services[index] = updated
                cls.saveDocument(document)
                return updated
            raise KeyError(normalized)

    # ----- secrets -----

    @classmethod
    def _invalidSecrets(cls) -> ValueError:
        return ValueError('AI API key secrets record is invalid.')

    @classmethod
    def _readSecretsMap(cls) -> dict[str, str]:
        """service_id → api_key map を読む。"""

        if cls.SECRETS_PATH.is_file() is False:
            return {}
        try:
            size = cls.SECRETS_PATH.stat().st_size
        except OSError:
            raise
        if size > cls._SECRETS_MAX_BYTES:
            raise cls._invalidSecrets()
        content = cls.SECRETS_PATH.read_text(encoding='utf-8')
        if content.strip() == '':
            return {}
        try:
            data = json.loads(content)
        except json.JSONDecodeError as error:
            raise cls._invalidSecrets() from error
        if isinstance(data, dict) is False:
            raise cls._invalidSecrets()
        # 旧 recorded-series-api.key 形式を拒否
        if set(data.keys()) == {'api_base_url', 'api_key'}:
            raise cls._invalidSecrets()
        if len(data) > cls._SECRETS_MAX_ENTRIES:
            raise cls._invalidSecrets()
        result: dict[str, str] = {}
        for raw_id, raw_key in data.items():
            if isinstance(raw_id, str) is False or isinstance(raw_key, str) is False:
                raise cls._invalidSecrets()
            service_id = raw_id.strip().lower()
            if _UUID_RE.fullmatch(service_id) is None:
                raise cls._invalidSecrets()
            key = raw_key.strip()
            if key == '' or len(key) > cls._API_KEY_MAX_LENGTH:
                raise cls._invalidSecrets()
            if service_id in result:
                raise cls._invalidSecrets()
            result[service_id] = key
        return result

    @classmethod
    def _writeSecretsMap(cls, secrets_map: dict[str, str]) -> None:
        """secrets map を atomic・0600 で保存。空ならファイル削除。"""

        if not secrets_map:
            if cls.SECRETS_PATH.is_file():
                cls.SECRETS_PATH.unlink()
                cls._fsyncParentDirectory(cls.SECRETS_PATH)
            return
        if len(secrets_map) > cls._SECRETS_MAX_ENTRIES:
            raise cls._invalidSecrets()
        content = json.dumps(secrets_map, ensure_ascii=False, indent=4) + '\n'
        if len(content.encode('utf-8')) > cls._SECRETS_MAX_BYTES:
            raise cls._invalidSecrets()
        cls._writeAtomic(cls.SECRETS_PATH, content, secret=True)

    @classmethod
    def getAPIKey(cls, service_id: str) -> str | None:
        """service の API キーを取得する（内部専用・API 非返却）。"""

        with cls._lock:
            return cls._readSecretsMap().get(service_id.strip().lower())

    @classmethod
    def hasAPIKey(cls, service_id: str) -> bool:
        """API キーが設定済みか。"""

        return cls.getAPIKey(service_id) is not None

    @classmethod
    def setAPIKey(cls, service_id: str, api_key: str) -> None:
        """API キーを保存する。"""

        normalized_id = service_id.strip().lower()
        if _UUID_RE.fullmatch(normalized_id) is None:
            raise ValueError('service_id は UUID である必要があります。')
        key = api_key.strip()
        if key == '' or len(key) > cls._API_KEY_MAX_LENGTH:
            raise ValueError('API key is invalid.')
        with cls._lock:
            secrets_map = cls._readSecretsMap()
            secrets_map[normalized_id] = key
            cls._writeSecretsMap(secrets_map)

    @classmethod
    def deleteAPIKey(cls, service_id: str) -> bool:
        """API キーを削除する。

        Returns:
            削除前に存在していれば True。
        """

        normalized_id = service_id.strip().lower()
        with cls._lock:
            secrets_map = cls._readSecretsMap()
            if normalized_id not in secrets_map:
                return False
            del secrets_map[normalized_id]
            cls._writeSecretsMap(secrets_map)
            return True

    @classmethod
    def isAuthConfigured(cls, service: AIBackendService) -> bool:
        """service の認証が利用可能とみなせるか。"""

        if service.auth_mode == 'ApiKey':
            return cls.hasAPIKey(service.service_id)
        if service.auth_mode == 'OAuthSubscription':
            return service.oauth_connected
        if service.auth_mode == 'VertexAdc':
            return service.google_cloud_project is not None
        if service.auth_mode == 'NoneLocal':
            return service.api_base_url is not None
        return False

    @classmethod
    def toResponse(cls, service: AIBackendService) -> AIBackendServiceResponse:
        """秘密を含まない応答モデルへ変換する。"""

        return AIBackendServiceResponse(
            **service.model_dump(),
            auth_configured=cls.isAuthConfigured(service),
            episode_lookup_ready=False,
        )

    @classmethod
    def countServicesUsingProvider(cls, provider_id: str, *, excluding_service_id: str | None = None) -> int:
        """同一 opencode_provider_id を参照する service 数（参照カウント）。"""

        normalized_provider = provider_id.strip().lower()
        excluded = (excluding_service_id or '').strip().lower() or None
        count = 0
        for service in cls.listServices():
            if excluded is not None and service.service_id == excluded:
                continue
            if service.opencode_provider_id == normalized_provider:
                count += 1
        return count

    @classmethod
    def _fsyncParentDirectory(cls, path: Path) -> None:
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY,
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)

    @classmethod
    def _writeAtomic(cls, destination: Path, content: str, *, secret: bool = False) -> None:
        """同一ディレクトリ内 atomic write。secret 時は 0600 と親 0700。"""

        destination.parent.mkdir(mode=0o700 if secret else 0o755, parents=True, exist_ok=True)
        if secret:
            os.chmod(destination.parent, 0o700)
        temporary_path = destination.with_name(
            f'.{destination.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp',
        )
        file_descriptor: int | None = None
        encoded = content.encode('utf-8')
        mode = 0o600 if secret else 0o644
        try:
            file_descriptor = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                mode,
            )
            written = 0
            while written < len(encoded):
                written += os.write(file_descriptor, encoded[written:])
            os.fsync(file_descriptor)
            os.close(file_descriptor)
            file_descriptor = None
            os.chmod(temporary_path, mode)
            os.replace(temporary_path, destination)
            if secret:
                cls._fsyncParentDirectory(destination)
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def IsRecordedSeriesReferencingService(service_id: str) -> bool:
    """録画シリーズ設定が service_id を参照しているか。

    循環 import を避けるため関数内で RecordedSeriesSettings を読む。

    Args:
        service_id: 調査対象 UUID。

    Returns:
        参照中なら True。設定読込失敗時は安全側で True（削除拒否）。
    """

    try:
        from app.metadata.RecordedSeriesSettings import RecordedSeriesSettingsStore
        settings = RecordedSeriesSettingsStore.getSettings()
    except Exception:
        return True
    referenced = getattr(settings, 'ai_backend_service_id', None)
    if referenced is None:
        return False
    return str(referenced).strip().lower() == service_id.strip().lower()
