"""ACP バックエンド（Codex / Grok Build）の固定プリセット設定。

録画シリーズ AI など、ACP を利用するすべての用途から参照される共有設定。
Codex / Grok の 2 プロバイダを固定プリセットとして持ち、モデル・推論深さ・
Fast モード・無通信タイムアウトをプロバイダごとに独立して保存する。

保存先: DATA_DIR/acp-settings.json
秘密（auth.json）は KonomiTVBS4KACPCredentials 側が管理する。
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
ACP_DEFAULT_MODEL_BY_BACKEND: dict[str, str | None] = {
    'AcpCodex': 'gpt-5.6-luna',
    'AcpGrok': None,
}
ACP_DEFAULT_REASONING_EFFORT_BY_BACKEND: dict[str, AcpReasoningEffort] = {
    'AcpCodex': 'Medium',
    'AcpGrok': 'High',
}

# KonomiTV-BS4K の ACP バックエンド種別。固定プリセットとして 2 種のみ。
ACPBackendKind = Literal['AcpCodex', 'AcpGrok']


def IsKonomiTVBS4KCodexSolModel(konomitv_bs4k_model: str | None) -> bool:
    """Codex のモデル ID が Sol 系統かを判定する。

    Args:
        konomitv_bs4k_model: 判定する Codex モデル ID。

    Returns:
        Sol 系統（'sol' または '-sol' 末尾）なら True。
    """

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


class ACPBackendSettings(BaseModel):
    """1 プロバイダ（Codex / Grok）分の ACP 実行設定。"""

    model_config = ConfigDict(extra='forbid')

    # プロバイダ種別。正規化に使用し、JSON には保存しない。
    backend_kind: Annotated[ACPBackendKind, Field(exclude=True)] = 'AcpCodex'
    # Codex のみ使用。Grok は grok-4.5 固定のため常に None へ正規化する。
    model: Annotated[str | None, Field(max_length=255)] = None
    # Codex: Low…Ultra / Grok: Low…High。未設定時は backend 既定へ補完する。
    reasoning_effort: Annotated[AcpReasoningEffort | None, Field()] = None
    # KonomiTV-BS4K 固有。Codex の専用 profile へ Fast service tier を設定する。
    codex_fast_mode_enabled: Annotated[bool, Field()] = False
    # 壁時計の総実行上限ではなく、ACP stdio の無通信打ち切り秒数。
    # thought / tool update などの NDJSON 行が届くたびにタイマーはリセットされる。
    timeout_sec: Annotated[int, Field(ge=30, le=600)] = 120

    @field_validator('model')
    @classmethod
    def validateModel(cls, model: str | None) -> str | None:
        """モデル ID の空文字を未指定へ正規化する。

        連結 ID の分解は model_validator 側で行い、ここは前後空白のみ整える。
        """

        if model is None:
            return None
        return model.strip() or None

    @model_validator(mode='after')
    def normalizeBackendCapabilities(self) -> ACPBackendSettings:
        """backend ごとの固定能力とプリセット既定を正規化する。

        Grok はモデル固定・Fast 無効・推論深さ Low..High のみ。
        Codex はモデル既定と Ultra の Sol 制約を適用する。
        """

        if self.backend_kind == 'AcpGrok':
            # Grok Build の ACP は grok-4.5 固定。モデル ID は保存せず深さだけを持つ。
            self.model = None
            self.codex_fast_mode_enabled = False
            if self.reasoning_effort is None:
                self.reasoning_effort = ACP_DEFAULT_REASONING_EFFORT_BY_BACKEND['AcpGrok']
            if self.reasoning_effort not in _ACP_BASIC_REASONING_EFFORTS:
                raise ValueError('Grok の推論深さは Low / Medium / High のみです。')
            return self

        # 旧 UI の連結 ID を model + effort に分解する。
        # 明示された reasoning_effort がある場合は括弧側より優先する。
        if self.model is not None:
            bare_model, embedded_effort = ParseAcpCompositeModel(self.model)
            self.model = bare_model
            if self.reasoning_effort is None and embedded_effort is not None:
                self.reasoning_effort = embedded_effort

        if self.model is None:
            self.model = ACP_DEFAULT_MODEL_BY_BACKEND['AcpCodex']
        if self.reasoning_effort is None:
            self.reasoning_effort = ACP_DEFAULT_REASONING_EFFORT_BY_BACKEND['AcpCodex']
        if (
            self.reasoning_effort == 'Ultra' and
            IsKonomiTVBS4KCodexSolModel(self.model) is False
        ):
            # 旧保存値や直接 API 入力も、非 Sol では Max へ安全に補正する。
            self.reasoning_effort = 'Max'
        return self


class ACPSettings(BaseModel):
    """ACP バックエンド全体の永続化される設定。"""

    model_config = ConfigDict(extra='forbid')

    # 固定プリセットとして Codex / Grok の 2 つを持つ。
    codex: ACPBackendSettings = Field(default_factory=lambda: ACPBackendSettings(backend_kind='AcpCodex'))
    grok: ACPBackendSettings = Field(default_factory=lambda: ACPBackendSettings(backend_kind='AcpGrok'))

    @model_validator(mode='before')
    @classmethod
    def ensureBackendKinds(cls, data: object) -> object:
        """codex / grok の backend_kind を JSON 読取時に固定する。"""

        if isinstance(data, dict):
            if isinstance(data.get('codex'), dict):
                data['codex'] = {**data['codex'], 'backend_kind': 'AcpCodex'}
            if isinstance(data.get('grok'), dict):
                data['grok'] = {**data['grok'], 'backend_kind': 'AcpGrok'}
        return data

    def forBackend(self, backend_kind: str) -> ACPBackendSettings:
        """バックエンド種別に対応するプロバイダ設定を返す。

        Args:
            backend_kind: 'AcpCodex' または 'AcpGrok'。

        Returns:
            対応する ACPBackendSettings。

        Raises:
            ValueError: 未知の backend 種別の場合。
        """

        if backend_kind == 'AcpCodex':
            return self.codex
        if backend_kind == 'AcpGrok':
            return self.grok
        raise ValueError('Unknown ACP backend.')


class ACPSettingsStore:
    """ACP バックエンド設定を安全に永続化する。"""

    SETTINGS_PATH = DATA_DIR / 'acp-settings.json'

    _lock = threading.RLock()

    @classmethod
    def getSettings(cls) -> ACPSettings:
        """現在の ACP バックエンド設定を取得する。

        acp-settings.json が未作成の場合、旧 recorded-series-settings.json に
        残る ACP 実行設定（model / effort / Fast / timeout）があれば一度だけ
        移行して保存する。移行後は AI バックエンドページが正本。

        Returns:
            永続化済み設定。未作成時はデフォルト設定（または移行結果）。

        Raises:
            ValueError: 永続化済み設定が JSON またはスキーマとして不正な場合。
            OSError: 設定ファイルを読み込めない場合。
        """

        with cls._lock:
            if cls.SETTINGS_PATH.is_file() is False:
                migrated = cls._tryMigrateFromRecordedSeriesSettingsLocked()
                if migrated is not None:
                    return migrated
                return ACPSettings()
            settings_json = json.loads(cls.SETTINGS_PATH.read_text(encoding='utf-8'))
            if isinstance(settings_json, dict) is False:
                raise ValueError('ACP settings document is invalid.')
            return ACPSettings.model_validate(settings_json)

    @classmethod
    def _tryMigrateFromRecordedSeriesSettingsLocked(cls) -> ACPSettings | None:
        """旧 recorded-series-settings.json から ACP 実行設定を移行する。

        acp_model / konomitv_bs4k_acp_codex_fast_mode_enabled は Codex 固有、
        acp_reasoning_effort / acp_timeout_sec は両 backend 共通のため、
        旧 ai_backend に合わせて Codex / Grok 側へ振り分ける。

        Returns:
            移行して保存した設定。移行対象が無ければ None。
        """

        legacy_path = DATA_DIR / 'recorded-series-settings.json'
        if legacy_path.is_file() is False:
            return None
        try:
            legacy_raw = json.loads(legacy_path.read_text(encoding='utf-8'))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if isinstance(legacy_raw, dict) is False:
            return None
        has_legacy = any(
            key in legacy_raw
            for key in (
                'acp_model',
                'acp_reasoning_effort',
                'konomitv_bs4k_acp_codex_fast_mode_enabled',
                'acp_timeout_sec',
            )
        )
        if has_legacy is False:
            return None
        # 旧 ai_backend が AcpGrok なら Grok 側へ、それ以外（AcpCodex / OpenCode）は Codex 側へ移す。
        legacy_backend = legacy_raw.get('ai_backend')
        target_kind = 'grok' if legacy_backend == 'AcpGrok' else 'codex'
        target_backend_kind = 'AcpGrok' if legacy_backend == 'AcpGrok' else 'AcpCodex'
        payload: dict[str, object] = {'backend_kind': target_backend_kind}
        if 'acp_model' in legacy_raw:
            payload['model'] = legacy_raw.get('acp_model')
        if 'acp_reasoning_effort' in legacy_raw:
            payload['reasoning_effort'] = legacy_raw.get('acp_reasoning_effort')
        if 'konomitv_bs4k_acp_codex_fast_mode_enabled' in legacy_raw:
            payload['codex_fast_mode_enabled'] = legacy_raw.get(
                'konomitv_bs4k_acp_codex_fast_mode_enabled',
            )
        if 'acp_timeout_sec' in legacy_raw:
            payload['timeout_sec'] = legacy_raw.get('acp_timeout_sec')
        try:
            migrated = ACPSettings.model_validate({
                target_kind: payload,
                # 移行対象でない側は既定値のまま。
                'grok' if target_kind == 'codex' else 'codex': {
                    'backend_kind': 'AcpGrok' if target_kind == 'codex' else 'AcpCodex',
                },
            })
        except ValueError:
            return None
        # 移行結果を atomic 保存し、次回以降は acp-settings.json を正本にする。
        cls.saveSettings(migrated)
        return migrated

    @classmethod
    def saveSettings(cls, settings: ACPSettings) -> None:
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
            validated = ACPSettings.model_validate(settings.model_dump())
            payload = validated.model_dump(mode='json')
            content = json.dumps(payload, ensure_ascii=False, indent=4) + '\n'
            cls._writeAtomic(cls.SETTINGS_PATH, content)

    @classmethod
    def _writeAtomic(cls, destination: Path, content: str) -> None:
        """同一ディレクトリ内でファイルをアトミックに置き換える。

        Args:
            destination: 保存先パス。
            content: 書き込む内容。

        Returns:
            None

        Raises:
            OSError: 書き込み失敗。
        """

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
