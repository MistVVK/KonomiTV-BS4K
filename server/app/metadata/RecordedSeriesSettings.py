from __future__ import annotations

import json
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.constants import DATA_DIR


RecordedEpisodeNumberAcceptanceMode = Literal['HighConfidenceOnly', 'Always']
AIBackendKind = Literal[
    'OpenAICompatible',
    'AcpCodex',
    'AcpGrok',
    'AcpGemini',
]
# ACP 共通の推論深さ。CLI / agent には lowercase で渡す。
# Codex は XHigh/Max/Ultra まで。ただし Ultra は Sol 系統だけで使用する。
# Grok は Low/Medium/High のみ。
# Gemini CLI 0.52.0 の ACP は推論深さの設定口を広告しないため、Gemini では保存しない。
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
    'AcpGemini': 'gemini-3.6-flash',
}
_ACP_DEFAULT_REASONING_EFFORT_BY_BACKEND: dict[str, AcpReasoningEffort] = {
    'AcpCodex': 'Medium',
    'AcpGrok': 'High',
}


def _isKonomiTVBS4KCodexSolModel(konomitv_bs4k_model: str | None) -> bool:
    """Codex のモデル ID が Sol 系統かを判定する。"""

    if konomitv_bs4k_model is None:
        return False
    konomitv_bs4k_normalized_model = konomitv_bs4k_model.strip().lower()
    return (
        konomitv_bs4k_normalized_model == 'sol' or
        konomitv_bs4k_normalized_model.endswith('-sol')
    )


_LEGACY_ACP_AUTH_SETTINGS_KEYS = {
    'acp_user_codex_home',
    'acp_user_grok_home',
    'acp_user_gemini_home',
    'acp_auth_share_mode',
}
_LEGACY_ACP_CUSTOM_SETTINGS_KEYS = {
    'acp_cwd',
    'acp_command',
    'acp_args',
    'acp_env',
}


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
    daily_ai_request_limit: Annotated[int, Field(ge=0, le=1000)] = 20

    # AI バックエンド種別（既存設定には存在しないため、既定で OpenAICompatible）
    ai_backend: Annotated[AIBackendKind, Field()] = 'OpenAICompatible'

    # OpenAICompatible 用（既存互換）
    api_base_url: Annotated[str, Field(min_length=1, max_length=2048)] = 'https://api.openai.com/v1'
    model: Annotated[str, Field(min_length=1, max_length=255)] = 'gpt-5.6-luna'

    # ACP 共通（モデル名と推論深さは分離して保存する）
    acp_model: Annotated[str | None, Field(max_length=255)] = None
    # Codex: Low…Ultra / Grok: Low…High。backend ごとの default は model_validator で埋める。
    acp_reasoning_effort: Annotated[AcpReasoningEffort | None, Field()] = None
    # KonomiTV-BS4K 固有。Codex の専用 profile へ Fast service tier を設定する。
    konomitv_bs4k_acp_codex_fast_mode_enabled: Annotated[bool, Field()] = False
    # 壁時計の総実行上限ではなく、ACP stdio の無通信打ち切り秒数。
    # thought / tool update などの NDJSON 行が届くたびにタイマーはリセットされる。
    acp_timeout_sec: Annotated[int, Field(ge=30, le=600)] = 120

    # Gemini CLI / Vertex AI 用（資格情報ではない環境依存設定）
    google_cloud_project: Annotated[str | None, Field(max_length=255)] = None
    google_cloud_location: Annotated[str | None, Field(max_length=255)] = None

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
        # 不正 port は httpx 接続時に未捕捉例外になるため、保存時に 1-65535 で検証する
        try:
            port = parsed_url.port
        except ValueError as error:
            raise ValueError('API ベース URL のポート番号が不正です。') from error
        if port is not None and (port < 1 or port > 65535):
            raise ValueError('API ベース URL のポート番号は 1 以上 65535 以下である必要があります。')
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
        """ACP 能力とプリセットごとの固定実行経路を正規化する。"""

        if self.ai_backend != 'AcpCodex':
            # Fast service tier は Codex 専用で、他 provider へ持ち越さない。
            self.konomitv_bs4k_acp_codex_fast_mode_enabled = False

        if self.ai_backend == 'OpenAICompatible':
            # OpenAI 互換では ACP の model / effort を使わない。
            self.acp_reasoning_effort = None
            return self

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
        elif self.ai_backend == 'AcpGemini':
            if self.acp_model is None:
                self.acp_model = _ACP_DEFAULT_MODEL_BY_BACKEND['AcpGemini']
            # Gemini CLI 0.52.0 の ACP は session/new で models だけを広告し、
            # thought_level / reasoning_effort の configOption を提供しない。
            # model[effort] をモデル ID として渡すとそのまま API へ流れるため、深度は保存しない。
            self.acp_reasoning_effort = None

        if (
            self.ai_backend == 'AcpGemini' and
            (self.google_cloud_project is None or self.google_cloud_location is None)
        ):
            raise ValueError('AcpGemini では Google Cloud プロジェクト ID とリージョンが必要です。')
        return self


class RecordedSeriesSettingsResponse(RecordedSeriesSettings):
    """秘密情報を含まない録画シリーズ判定設定 API レスポンス。"""

    api_key_configured: Annotated[bool, Field()]


class RecordedSeriesSettingsStore:
    """録画シリーズ判定設定と API キーを分離して安全に永続化する。"""

    SETTINGS_PATH = DATA_DIR / 'recorded-series-settings.json'
    API_KEY_PATH = DATA_DIR / 'secrets' / 'recorded-series-api.key'

    # 秘密 map の上限。内容をログへ出さず固定エラーにする。
    _API_KEY_MAP_MAX_BYTES = 256 * 1024
    _API_KEY_MAP_MAX_ENTRIES = 32
    _API_KEY_MAX_LENGTH = 8192

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
            if isinstance(settings_json, dict):
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
                if settings_json.get('ai_backend') == 'AcpCustom':
                    # 任意 command の再実行を防ぐため、旧 Custom 設定は AI を停止して
                    # 明示的なバックエンド再選択が必要な OpenAI 互換設定へ移行する。
                    settings_json['ai_backend'] = 'OpenAICompatible'
                    settings_json['ai_enabled'] = False
            return RecordedSeriesSettings.model_validate(settings_json)

    @classmethod
    def _invalidAPIKeyRecord(cls) -> ValueError:
        """秘密ファイル内容を含めない固定エラーを返す。"""

        return ValueError('Recorded series API key record is invalid.')

    @staticmethod
    def _jsonObjectPairsNoDuplicates(
        pairs: list[tuple[Any, Any]],
    ) -> dict[Any, Any]:
        """JSON object の重複キーを辞書化前に fail-closed で検出する。"""

        result: dict[Any, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON object key')
            result[key] = value
        return result

    @classmethod
    def _readAPIKeysMap(cls) -> dict[str, str]:
        """保存済みの全 URL/API キーのマッピングを取得する。

        受理するのは次の3状態だけ。
        1. ファイルなし、または空ファイル → 空 map
        2. 旧形式（キーが厳密に api_base_url / api_key の2つ、両方 str）
        3. 新 map 形式（root が dict、全キー・全値が非空 str、URL は正規化済み）

        不正項目の黙殺や str() による型変換は行わない。
        読込成功後は canonical シリアライズでもサイズ上限内であることを要求し、
        書戻し不能な境界ファイルを受理しない。

        Returns:
            dict[str, str]: 正規化済みベース URL をキーとする API キー map。

        Raises:
            ValueError: 構造不正・サイズ超過・正規化衝突時（内容は含めない）。
            OSError: API キーファイルを読み込めない場合。
        """

        if cls.API_KEY_PATH.is_file() is False:
            return {}

        try:
            file_size = cls.API_KEY_PATH.stat().st_size
        except OSError:
            raise
        if file_size > cls._API_KEY_MAP_MAX_BYTES:
            raise cls._invalidAPIKeyRecord()

        content = cls.API_KEY_PATH.read_text(encoding='utf-8')
        if content.strip() == '':
            return {}

        try:
            data = json.loads(
                content,
                object_pairs_hook=cls._jsonObjectPairsNoDuplicates,
            )
        except json.JSONDecodeError as ex:
            raise cls._invalidAPIKeyRecord() from ex
        except ValueError as ex:
            # 重複キー検出。内容は例外メッセージへ含めない。
            raise cls._invalidAPIKeyRecord() from ex

        # 旧形式 {"api_base_url": "...", "api_key": "..."} — キー集合と型を厳密に要求する
        if isinstance(data, dict) and set(data.keys()) == {'api_base_url', 'api_key'}:
            raw_url = data['api_base_url']
            raw_key = data['api_key']
            if isinstance(raw_url, str) is False or isinstance(raw_key, str) is False:
                raise cls._invalidAPIKeyRecord()
            # Pydantic field 上限と同等の raw 長検査（validator 直呼びでは field max が効かない）
            if len(raw_url) > 2048:
                raise cls._invalidAPIKeyRecord()
            try:
                url = RecordedSeriesSettings.validateAPIBaseURL(raw_url)
            except ValueError as ex:
                raise cls._invalidAPIKeyRecord() from ex
            key = raw_key.strip()
            if key == '' or len(key) > cls._API_KEY_MAX_LENGTH:
                raise cls._invalidAPIKeyRecord()
            keys_map = {url: key}
            # 移行後の canonical 書式でも上限内であることを読込時に保証する。
            cls._serializeAPIKeysMap(keys_map)
            return keys_map

        if isinstance(data, dict) is False:
            raise cls._invalidAPIKeyRecord()
        if len(data) > cls._API_KEY_MAP_MAX_ENTRIES:
            raise cls._invalidAPIKeyRecord()

        keys_map: dict[str, str] = {}
        for raw_url, raw_key in data.items():
            if isinstance(raw_url, str) is False or isinstance(raw_key, str) is False:
                raise cls._invalidAPIKeyRecord()
            # 新 map は保存キー自体が長さ上限内かつ既に正規化済みであることを要求する。
            # 黙って正規化し直して受理すると、制約を破った不正な保存状態を残してしまう。
            if len(raw_url) > 2048:
                raise cls._invalidAPIKeyRecord()
            try:
                url = RecordedSeriesSettings.validateAPIBaseURL(raw_url)
            except ValueError as ex:
                raise cls._invalidAPIKeyRecord() from ex
            if raw_url != url:
                raise cls._invalidAPIKeyRecord()
            key = raw_key.strip()
            if key == '' or len(key) > cls._API_KEY_MAX_LENGTH:
                raise cls._invalidAPIKeyRecord()
            if url in keys_map:
                # 正規化後に同一 URL へ衝突する map は拒否する
                raise cls._invalidAPIKeyRecord()
            keys_map[url] = key
        if keys_map:
            # compact 保存でも canonical 再シリアライズが上限内であることを要求する。
            cls._serializeAPIKeysMap(keys_map)
        return keys_map

    @classmethod
    def _serializeAPIKeysMap(cls, keys_map: dict[str, str]) -> str:
        """API キー map を UTF-8 バイト長上限付きで JSON 文字列化する。

        Args:
            keys_map: 正規化済み URL をキーとする非空 API キー map。

        Returns:
            str: 保存用 JSON 文字列（末尾改行付き）。

        Raises:
            ValueError: エントリ数またはシリアライズ後のバイト長が上限を超える場合。
        """

        if len(keys_map) > cls._API_KEY_MAP_MAX_ENTRIES:
            raise cls._invalidAPIKeyRecord()
        content = json.dumps(keys_map, ensure_ascii=False, indent=4) + '\n'
        if len(content.encode('utf-8')) > cls._API_KEY_MAP_MAX_BYTES:
            raise cls._invalidAPIKeyRecord()
        return content

    @classmethod
    def _writeAPIKeysMap(cls, keys_map: dict[str, str]) -> None:
        """API キー map をアトミック書き込みで永続化する。

        Args:
            keys_map: 正規化済み URL をキーとする非空 API キー map。

        Raises:
            OSError: ファイル書き込みに失敗した場合。
            ValueError: エントリ数またはシリアライズ後のバイト長が上限を超える場合。
        """

        content = cls._serializeAPIKeysMap(keys_map)
        cls._writeAtomic(cls.API_KEY_PATH, content)

    @classmethod
    def _fsyncParentDirectory(cls, path: Path) -> None:
        """削除や置換をクラッシュ後にも永続化するため親 directory を fsync する。

        open / fsync の失敗は黙殺せず呼び出し側へ伝播する。
        204 を返す経路で削除の永続化を確認できない状態を残さないため。

        Raises:
            OSError: 親 directory を開けない、または fsync に失敗した場合。
        """

        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY,
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)

    @classmethod
    def _unlinkAPIKeyFile(cls) -> None:
        """秘密ファイルを削除し、親 directory を fsync する。

        directory FD は unlink 前に開き、unlink 後に fsync する。
        open / fsync 失敗は OSError として伝播し、DELETE が偽の成功にならないようにする。

        Raises:
            OSError: 削除または親 directory の fsync に失敗した場合。
        """

        parent = cls.API_KEY_PATH.parent
        directory_descriptor = os.open(
            parent,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY,
        )
        try:
            try:
                cls.API_KEY_PATH.unlink()
            except FileNotFoundError:
                pass
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)

    @classmethod
    def _persistAPIKeysMap(cls, keys_map: dict[str, str]) -> None:
        """空 map は秘密ファイルを残さず unlink し、非空 map だけを保存する。

        Args:
            keys_map: 正規化済み URL をキーとする API キー map。

        Raises:
            OSError: ファイル書き込みまたは削除に失敗した場合。
            ValueError: エントリ数上限を超える場合。
        """

        if not keys_map:
            cls._unlinkAPIKeyFile()
            return
        cls._writeAPIKeysMap(keys_map)

    @classmethod
    def getAPIKeyForURL(cls, url: str) -> str | None:
        """指定された URL に対応する API キーを取得する。

        Args:
            url: 正規化済みの API ベース URL。

        Returns:
            str | None: 一致するキー。未設定の場合は None。
        """

        try:
            normalized_url = RecordedSeriesSettings.validateAPIBaseURL(url)
        except ValueError:
            return None
        with cls._lock:
            keys_map = cls._readAPIKeysMap()
        return keys_map.get(normalized_url)

    @classmethod
    def saveSettings(
        cls,
        settings: RecordedSeriesSettings,
        *,
        api_key: str | None = None,
    ) -> None:
        """設定を永続化し、指定時だけ API キー map を置き換える。

        空キーは現在の api_base_url のエントリだけを削除する。
        api_key=None のときは map を変更しない（URL だけ変えても他 URL のキーは残る）。
        ACP バックエンド選択時は api_key を無視する。

        Args:
            settings: 永続化するシリーズ判定設定。
            api_key: 保存する API キー。空文字列は現在 URL のキー削除、None は map 変更なし。
                ACP バックエンド選択時は無視される。

        Returns:
            None

        Raises:
            OSError: 設定または API キーを保存できない場合。
        """

        with cls._lock:
            keys_map = cls._readAPIKeysMap()
            target_url = settings.api_base_url.strip().rstrip('/')
            api_key_existed = cls.API_KEY_PATH.is_file()
            # 再シリアライズがサイズ上限で失敗しても完全復元できるよう、元ファイル bytes を保持する。
            previous_key_bytes: bytes | None = None
            if api_key_existed:
                previous_key_bytes = cls.API_KEY_PATH.read_bytes()
            keys_map_write_needed = False
            # replace 成功後の directory fsync 失敗でも新キーがディスクに残り得るため、
            # 書込開始後は常に旧 bytes へ rollback する（auth import と同じ段階フラグ）。
            keys_map_mutation_started = False

            # ACP バックエンドでは api_key を無視する
            if settings.ai_backend == 'OpenAICompatible' and api_key is not None:
                normalized_key = api_key.strip()
                if normalized_key == '':
                    # 空キーは対象 URL のキーだけを削除
                    keys_map.pop(target_url, None)
                else:
                    if len(normalized_key) > cls._API_KEY_MAX_LENGTH:
                        raise ValueError('Recorded series API key is too long.')
                    keys_map[target_url] = normalized_key
                keys_map_write_needed = True

            # キー map を先に書き、続けて非機密設定を公開する（部分更新を残さない）
            # api_key が明示的に渡されなくても、ファイルが既存なら map 形式で書き戻す。
            # これにより旧形式（単一レコード）からの自動移行が、設定変更だけでも進行する。
            if keys_map_write_needed is False and api_key_existed:
                keys_map_write_needed = True

            settings_data = json.dumps(
                settings.model_dump(mode='json'),
                ensure_ascii=False,
                indent=4,
            ) + '\n'
            try:
                if keys_map_write_needed:
                    # 空 map は秘密ファイルを残さず unlink する
                    keys_map_mutation_started = True
                    cls._persistAPIKeysMap(keys_map)
                cls._writeAtomic(cls.SETTINGS_PATH, settings_data)
            except (OSError, ValueError) as save_error:
                # 2ファイルをOSレベルで同時replaceはできないため、キー書込開始後の任意失敗では
                # 元ファイル bytes をそのまま atomic 復元し「新キー＋旧設定」を残さない。
                # ValueError（canonical サイズ超過等）も同様に扱う。
                if keys_map_mutation_started:
                    try:
                        if previous_key_bytes is not None:
                            cls._writeAtomicBytes(cls.API_KEY_PATH, previous_key_bytes)
                        else:
                            cls._unlinkAPIKeyFile()
                    except OSError as rollback_error:
                        raise OSError('Failed to save settings and roll back the API key.') from rollback_error
                raise save_error

    @classmethod
    def getAPIKey(cls) -> str | None:
        """保存済み API キー（現在の api_base_url に対応）を Resolver 向けに取得する。

        Returns:
            保存済み API キー。未設定または空ファイルの場合は None。

        Raises:
            OSError: API キーファイルを読み込めない場合。
        """

        with cls._lock:
            settings = cls.getSettings()
            if settings.ai_backend != 'OpenAICompatible':
                return None
            return cls.getAPIKeyForURL(settings.api_base_url)

    @classmethod
    def isAPIKeyConfigured(cls) -> bool:
        """現在の api_base_url に API キーが設定済みかを、キー本体を公開せず取得する。

        Returns:
            bool: 非空の API キーが現在 URL に対して保存されている場合は True。
            ACP バックエンド選択時は常に False を返す。
        """

        with cls._lock:
            settings = cls.getSettings()
            if settings.ai_backend != 'OpenAICompatible':
                return False
        return cls.getAPIKey() is not None

    @classmethod
    def getSettingsAndAPIKey(cls) -> tuple[RecordedSeriesSettings, str | None]:
        """判定1回で使用する設定とAPIキーを、同じlock世代の組として取得する。

        Returns:
            同時点の設定とAPIキー。URL更新とキー更新の途中状態は返さない。
            ACP バックエンド選択時は API キーは None。
        """

        with cls._lock:
            settings = cls.getSettings()
            if settings.ai_backend != 'OpenAICompatible':
                return settings, None
            return settings, cls.getAPIKeyForURL(settings.api_base_url)

    @classmethod
    def deleteAPIKey(cls, url: str | None = None) -> None:
        """指定された URL または現在の api_base_url に対応する API キーだけを削除する。

        構造不正で対象 URL を安全に特定できない場合は秘密ファイル全体を unlink し、
        親 directory を fsync してから戻る（204 相当の成功経路で秘密を残さない）。
        最後の1件を削除した場合も空 map を残さず秘密ファイル自体を削除する。

        Args:
            url: 削除対象の URL。None の場合は現在の設定の api_base_url を使用。
                指定時は validateAPIBaseURL と同等の正規化・検証を行う。

        Returns:
            None

        Raises:
            ValueError: url が不正な場合（構造不正ファイルの全体削除とは別）。
            OSError: API キーファイルを保存できない場合。
        """

        with cls._lock:
            try:
                keys_map = cls._readAPIKeysMap()
            except ValueError:
                # 構造不正で対象を安全に特定できない → 全体 unlink + directory fsync
                cls._unlinkAPIKeyFile()
                return

            if not keys_map:
                # 空 map / 空ファイルが残っていれば掃除する
                if cls.API_KEY_PATH.is_file():
                    cls._unlinkAPIKeyFile()
                return

            if url is not None:
                target_url = RecordedSeriesSettings.validateAPIBaseURL(url)
            else:
                settings = cls.getSettings()
                target_url = settings.api_base_url.strip().rstrip('/')

            if target_url not in keys_map:
                return
            keys_map.pop(target_url, None)
            cls._persistAPIKeysMap(keys_map)

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

        cls._writeAtomicBytes(destination, content.encode('utf-8'))

    @classmethod
    def _writeAtomicBytes(cls, destination: Path, content: bytes) -> None:
        """同一ディレクトリ内でファイルを bytes のままアトミックに置き換える。

        rollback 時は再シリアライズせず元ファイル bytes をそのまま復元するために使う。

        Args:
            destination: 置き換え先のファイルパス。
            content: 保存する bytes。

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
            written_bytes = 0
            while written_bytes < len(content):
                written_bytes += os.write(file_descriptor, content[written_bytes:])
            os.fsync(file_descriptor)
            os.close(file_descriptor)
            file_descriptor = None
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, destination)
            if destination == cls.API_KEY_PATH:
                cls._fsyncParentDirectory(destination)
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
