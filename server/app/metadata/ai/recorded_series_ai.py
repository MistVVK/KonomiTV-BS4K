"""録画シリーズ AI 統合 facade。

プロバイダー非依存のインターフェースを提供し、
設定に基づいて適切なバックエンド（OpenCode / ACP）へ routing する。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Literal, TypeVar, cast

from typing_extensions import TypedDict

from app import logging
from app.constants import DATA_DIR
from app.metadata.ai.ai_failure_recovery import (
    AIBackendTarget,
    AIPromptVariant,
    AIRecoveryAttemptSummary,
    BuildPrimaryTarget,
    BuildRecoveryTarget,
    EpisodeResultCode,
    FormatRecoveryAttemptSummary,
    SeriesResultCode,
    ShouldRecoverEpisodeLookupError,
    ShouldRecoverEpisodeLookupResult,
    ShouldRecoverSeriesMetadataError,
    ShouldRecoverSeriesMetadataResult,
)
from app.metadata.ai.backends import (
    ConnectionTestCheck,
    ConnectionTestResult,
    EpisodeLookupConnectionChecks,
    RecordedSeriesAIBackend,
)
from app.metadata.ai.episode_lookup import EpisodeLookupResult
from app.metadata.ai.KonomiTVBS4KACPCredentials import (
    KonomiTVBS4KACPCredentials,
    KonomiTVBS4KACPImportProvider,
)
from app.metadata.RecordedEpisodeContext import (
    BuildEpisodeProviderFingerprint,
    RecordedEpisodeLookupContext,
)
from app.metadata.RecordedEpisodeMessages import (
    ACP_HARD_TIMEOUT_SEC,
    FormatAcpHardTimeoutMessage,
)
from app.metadata.RecordedSeriesCandidates import (
    AIChoiceResult,
    RecordedSeriesAIError,
    RecordedSeriesProgramPrompt,
    SeriesChoiceCandidate,
)
from app.metadata.RecordedSeriesGeneration import (
    AISeriesMetadataResult,
    SeriesMetadataHints,
)
from app.metadata.RecordedSeriesSettings import (
    AIBackendKind,
    RecordedSeriesSettings,
    RecordedSeriesSettingsStore,
)


# fingerprint -> proven backend kind。再起動後も単票再検索を通すためディスクへ永続化する。
# テストから差し替え可能なよう module 属性として公開する。
EPISODE_LOOKUP_CAPABILITY_PROOFS_PATH: Path = (
    DATA_DIR / 'recorded-series-episode-lookup-proofs.json'
)
_MAX_EPISODE_LOOKUP_CAPABILITY_PROOFS = 32
_EPISODE_LOOKUP_CAPABILITY_PROOFS_LOCK = threading.RLock()
# fingerprint 挿入順を保ち、上限超過時は最古を捨てる。
_EPISODE_LOOKUP_CAPABILITY_PROOFS: OrderedDict[str, str] = OrderedDict()
_EPISODE_LOOKUP_CAPABILITY_PROOFS_LOADED = False
# ACP agent は全 provider 合計で1件だけ実行する。acp_client 側の Semaphore も
# 最終防衛として維持するが、公開 facade でも backend 構築前から直列化する。
ACP_OPERATION_LOCK = asyncio.Lock()
# Codex / Grok の可変 credential profile は provider ごとに独立している。
# 実行中 provider の import/delete だけを止め、別 provider の認証操作を巻き添えにしない。
ACP_CREDENTIAL_OPERATION_LOCKS: dict[KonomiTVBS4KACPImportProvider, asyncio.Lock] = {
    'codex': asyncio.Lock(),
    'grok': asyncio.Lock(),
}
# 公開 facade の実行待ち・credential lock 待機から backend 回収までに適用する絶対上限。
# acp_client 内部にも同じ上限を残し、facade を経由しない呼出しも有限に保つ。
_ACP_OPERATION_HARD_TIMEOUT_SEC = ACP_HARD_TIMEOUT_SEC

_AcpOperationResult = TypeVar('_AcpOperationResult')


class _AIBackendTargetFingerprint(TypedDict, total=False):
    """1 backend の実行条件を fingerprint 化するための安全な構造。"""

    backend_kind: str
    service_id: str | None
    prompt_variant: str
    service: object
    api_key_hash: str | None
    acp_settings: object
    credential_generation: str | None


class _AIExecutionFingerprintPayload(TypedDict):
    """主系・回復系をまとめた AI 実行条件。"""

    primary: _AIBackendTargetFingerprint
    recovery: _AIBackendTargetFingerprint | None
    failure_recovery_strategy: str


async def _RunACPOperationWithDeadline(
    operation: Callable[[], Awaitable[_AcpOperationResult]],
    credential_provider: KonomiTVBS4KACPImportProvider | None,
    *,
    hard_deadline: float | None = None,
) -> _AcpOperationResult:
    """直列実行待ちと credential lock 待機を含む ACP 公開操作へ絶対期限を適用する。

    Args:
        operation: lock 取得後に backend を生成して実行する非同期処理。
        credential_provider: 実行中の世代変更を止める Codex / Grok provider。
            Gemini は管理 API から ADC を変更しないため None。
        hard_deadline: 同一判定の ACP 回復試行で共有する event loop 絶対期限。
            未指定時はこの操作の開始時点から既定上限を適用する。

    Returns:
        backend が返した操作結果。

    Raises:
        RecordedSeriesAIError: lock 待機から cleanup 完了までが安全上限を超えた場合。
    """

    started_at = time.monotonic()
    effective_deadline = hard_deadline
    if effective_deadline is None:
        effective_deadline = (
            asyncio.get_running_loop().time() + _ACP_OPERATION_HARD_TIMEOUT_SEC
        )
    try:
        # 全 ACP の直列実行待ちと、対象 provider の認証排他待ちを総実行時間に含める。
        # backend は両 lock 取得後に生成し、期限切れ要求が新しい ACP process を起動しないようにする。
        async with asyncio.timeout_at(effective_deadline):
            async with ACP_OPERATION_LOCK:
                if credential_provider is None:
                    return await operation()
                async with ACP_CREDENTIAL_OPERATION_LOCKS[credential_provider]:
                    return await operation()
    except TimeoutError as ex:
        raise RecordedSeriesAIError(
            'HardTimeout',
            latency_ms=int((time.monotonic() - started_at) * 1000),
        ) from ex


def _loadEpisodeLookupCapabilityProofsLocked() -> None:
    """ディスク上の能力証明をメモリへ読み込む。lock 保持中に呼ぶこと。"""

    global _EPISODE_LOOKUP_CAPABILITY_PROOFS_LOADED
    if _EPISODE_LOOKUP_CAPABILITY_PROOFS_LOADED:
        return
    _EPISODE_LOOKUP_CAPABILITY_PROOFS.clear()
    path = EPISODE_LOOKUP_CAPABILITY_PROOFS_PATH
    if path.is_file() is False:
        _EPISODE_LOOKUP_CAPABILITY_PROOFS_LOADED = True
        return
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as ex:
        logging.warning(
            '[RecordedSeriesAI] Failed to load episode lookup capability proofs. '
            'Starting with an empty proof set.',
            exc_info=ex,
        )
        _EPISODE_LOOKUP_CAPABILITY_PROOFS_LOADED = True
        return
    entries: dict[object, object] | None = None
    if isinstance(raw, dict):
        proofs = raw.get('proofs')
        if isinstance(proofs, dict):
            entries = cast(dict[object, object], proofs)
        else:
            # 旧形式や手編集: fingerprint -> backend の flat map も受理する。
            entries = cast(dict[object, object], raw)
    if entries is None:
        logging.warning(
            '[RecordedSeriesAI] Episode lookup capability proof file is invalid. '
            'Starting with an empty proof set.',
        )
        _EPISODE_LOOKUP_CAPABILITY_PROOFS_LOADED = True
        return
    for raw_fingerprint, raw_backend in entries.items():
        if isinstance(raw_fingerprint, str) is False or raw_fingerprint == '':
            continue
        if isinstance(raw_backend, str) is False or raw_backend == '':
            continue
        fingerprint = cast(str, raw_fingerprint)
        backend = cast(str, raw_backend)
        _EPISODE_LOOKUP_CAPABILITY_PROOFS[fingerprint] = backend
        if (
            len(_EPISODE_LOOKUP_CAPABILITY_PROOFS)
            > _MAX_EPISODE_LOOKUP_CAPABILITY_PROOFS
        ):
            _EPISODE_LOOKUP_CAPABILITY_PROOFS.popitem(last=False)
    _EPISODE_LOOKUP_CAPABILITY_PROOFS_LOADED = True


def _persistEpisodeLookupCapabilityProofsLocked() -> None:
    """メモリ上の能力証明をアトミックにディスクへ書く。lock 保持中に呼ぶこと。"""

    path = EPISODE_LOOKUP_CAPABILITY_PROOFS_PATH
    payload = {
        'version': 1,
        'proofs': dict(_EPISODE_LOOKUP_CAPABILITY_PROOFS),
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path = path.with_name(
        f'.{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp',
    )
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        encoded = content.encode('utf-8')
        written = 0
        while written < len(encoded):
            written += os.write(file_descriptor, encoded[written:])
        os.fsync(file_descriptor)
        os.close(file_descriptor)
        file_descriptor = None
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except OSError as ex:
        logging.error(
            '[RecordedSeriesAI] Failed to persist episode lookup capability proofs.',
            exc_info=ex,
        )
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _ensureEpisodeLookupCapabilityProofsLoaded() -> None:
    """必要ならディスクから能力証明を読み込む。"""

    with _EPISODE_LOOKUP_CAPABILITY_PROOFS_LOCK:
        _loadEpisodeLookupCapabilityProofsLocked()


def reset_episode_lookup_capability_proofs_for_tests() -> None:
    """単体テスト用にメモリ状態だけを初期化する。永続ファイルは触らない。"""

    global _EPISODE_LOOKUP_CAPABILITY_PROOFS_LOADED
    with _EPISODE_LOOKUP_CAPABILITY_PROOFS_LOCK:
        _EPISODE_LOOKUP_CAPABILITY_PROOFS.clear()
        _EPISODE_LOOKUP_CAPABILITY_PROOFS_LOADED = False


def _BuildTargetFingerprint(target: AIBackendTarget) -> _AIBackendTargetFingerprint:
    """1 backend のモデル・認証・service 定義を安全な fingerprint 素材へ変換する。

    Args:
        target: 主系または回復系の実行ターゲット。

    Returns:
        秘密本体を含まない実行条件。
    """

    if target.backend_kind == 'OpenCode':
        from app.metadata.ai.AIBackendSettings import AIBackendSettingsStore

        service = (
            AIBackendSettingsStore.getService(target.service_id)
            if target.service_id is not None
            else None
        )
        api_key = (
            AIBackendSettingsStore.getAPIKey(target.service_id)
            if target.service_id is not None
            else None
        )
        return _AIBackendTargetFingerprint(
            backend_kind=target.backend_kind,
            service_id=target.service_id,
            prompt_variant=target.prompt_variant,
            service=(
                service.model_dump(mode='json')
                if service is not None
                else None
            ),
            # API キー本体は保持せず、認証世代を区別する hash だけを含める。
            api_key_hash=(
                hashlib.sha256(api_key.encode('utf-8')).hexdigest()
                if api_key is not None
                else None
            ),
        )

    from app.metadata.ai.ACPSettings import ACPSettingsStore

    credential_provider = _GetACPCredentialProvider(target.backend_kind)
    acp_settings = ACPSettingsStore.getSettings().forBackend(target.backend_kind)
    return _AIBackendTargetFingerprint(
        backend_kind=target.backend_kind,
        service_id=None,
        prompt_variant=target.prompt_variant,
        acp_settings=acp_settings.model_dump(mode='json'),
        credential_generation=(
            KonomiTVBS4KACPCredentials.getCredentialGeneration(credential_provider)
            if credential_provider is not None
            else None
        ),
    )


def _BuildAIExecutionFingerprintPayload(
    settings: RecordedSeriesSettings,
) -> _AIExecutionFingerprintPayload:
    """主系・予備系を含む不変な AI 実行条件を構築する。

    Args:
        settings: 判定開始時点の録画シリーズ設定。

    Returns:
        JSON 直列化可能な fingerprint 素材。
    """

    recovery_target = BuildRecoveryTarget(settings)
    return _AIExecutionFingerprintPayload(
        primary=_BuildTargetFingerprint(BuildPrimaryTarget(settings)),
        recovery=(
            _BuildTargetFingerprint(recovery_target)
            if recovery_target is not None
            else None
        ),
        failure_recovery_strategy=settings.ai_failure_recovery_strategy,
    )


def GetAIExecutionFingerprint(settings: RecordedSeriesSettings) -> str:
    """主系・予備系の実設定と認証世代を識別する hash を返す。

    Args:
        settings: 判定開始時点の録画シリーズ設定。

    Returns:
        安全な SHA-256 fingerprint。
    """

    payload = _BuildAIExecutionFingerprintPayload(settings)
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


def _VerifyAIExecutionFingerprint(
    settings: RecordedSeriesSettings,
    expected_fingerprint: str,
) -> None:
    """認証・設定排他の取得後に、受付時の AI 実行条件と一致するか確認する。

    Args:
        settings: 判定受付時の録画シリーズ設定。
        expected_fingerprint: 受付時に固定した実行条件 fingerprint。

    Returns:
        None

    Raises:
        RecordedSeriesAIError: モデル・service・認証世代が変更された場合。
    """

    if GetAIExecutionFingerprint(settings) != expected_fingerprint:
        raise RecordedSeriesAIError('AISettingsChangedBeforeRequest')


def get_episode_lookup_provider_fingerprint(
    settings: RecordedSeriesSettings,
    api_key: str | None,
) -> str:
    """接続試験と実行前検証で共有する、安全な能力証明キーを返す。

    主系 backend に加え、失敗時ポリシーと予備 AI 設定も fingerprint に含め、
    設定変更後に古い判定結果や proof を再利用しない。
    """

    # provider proof と自動判定 cache は同じ主系・予備系実行条件を共有する。
    endpoint_identifier = GetAIExecutionFingerprint(settings)
    backend_kind_for_fingerprint = settings.ai_backend
    effective_api_key: str | None = None
    return BuildEpisodeProviderFingerprint(
        backend_kind=backend_kind_for_fingerprint,
        effective_model=get_audit_model(settings),
        endpoint_identifier=endpoint_identifier,
        api_key=effective_api_key,
    )


def record_episode_lookup_capability_proof(
    settings: RecordedSeriesSettings,
    api_key: str | None,
    result: ConnectionTestResult,
    *,
    tested_provider_fingerprint: str | None = None,
) -> bool:
    """EpisodeLookup 接続試験の実測結果を provider fingerprint に結び付ける。

    接続・Web 検索・検索元 URL・strict schema の4項目がすべて Passed の
    場合だけ、接続試験の診断結果を provider fingerprint に記録する。設定やキーが
    変わると fingerprint も変わるため、古い診断結果は再利用されない。実検索は
    同じ能力を実行時に検証するため、この記録の有無を開始条件にはしない。
    """

    fingerprint = get_episode_lookup_provider_fingerprint(settings, api_key)
    with _EPISODE_LOOKUP_CAPABILITY_PROOFS_LOCK:
        _loadEpisodeLookupCapabilityProofsLocked()
        if (
            tested_provider_fingerprint is not None
            and fingerprint != tested_provider_fingerprint
        ):
            # 接続試験中に設定・認証世代が変わった結果を、新世代の proof として
            # 登録しない。試験開始時の古い proof も失効させる。
            if tested_provider_fingerprint in _EPISODE_LOOKUP_CAPABILITY_PROOFS:
                _EPISODE_LOOKUP_CAPABILITY_PROOFS.pop(tested_provider_fingerprint, None)
                _persistEpisodeLookupCapabilityProofsLocked()
            return False
        checks = result.checks
        is_proven = (
            result.success
            and checks is not None
            and all(
                check.status == 'Passed'
                for check in (
                    checks.backend_connection,
                    checks.web_search,
                    checks.source_url,
                    checks.strict_schema,
                )
            )
        )
        if is_proven is False:
            if fingerprint in _EPISODE_LOOKUP_CAPABILITY_PROOFS:
                _EPISODE_LOOKUP_CAPABILITY_PROOFS.pop(fingerprint, None)
                _persistEpisodeLookupCapabilityProofsLocked()
            return False
        # 再記録時は LRU の末尾へ移す。
        _EPISODE_LOOKUP_CAPABILITY_PROOFS.pop(fingerprint, None)
        _EPISODE_LOOKUP_CAPABILITY_PROOFS[fingerprint] = settings.ai_backend
        while (
            len(_EPISODE_LOOKUP_CAPABILITY_PROOFS)
            > _MAX_EPISODE_LOOKUP_CAPABILITY_PROOFS
        ):
            _EPISODE_LOOKUP_CAPABILITY_PROOFS.popitem(last=False)
        _persistEpisodeLookupCapabilityProofsLocked()
        return True


def has_episode_lookup_capability_proof(
    settings: RecordedSeriesSettings,
    api_key: str | None,
) -> bool:
    """現在の provider fingerprint に一致する能力証明があるかを返す。"""

    fingerprint = get_episode_lookup_provider_fingerprint(settings, api_key)
    with _EPISODE_LOOKUP_CAPABILITY_PROOFS_LOCK:
        _loadEpisodeLookupCapabilityProofsLocked()
        return fingerprint in _EPISODE_LOOKUP_CAPABILITY_PROOFS


def invalidate_episode_lookup_capability_proof(
    *,
    settings: RecordedSeriesSettings | None = None,
    api_key: str | None = None,
    backend_kind: str | None = None,
) -> None:
    """実行時の能力否定または認証世代変更に対応する proof を失効する。

    Args:
        settings: 失効対象の完全な provider 設定。指定時は完全一致だけを失効する。
        api_key: OpenAI 互換設定に対応する API キー。
        backend_kind: 認証 import/delete 時に backend 全世代を失効する識別子。
    """

    with _EPISODE_LOOKUP_CAPABILITY_PROOFS_LOCK:
        _loadEpisodeLookupCapabilityProofsLocked()
        changed = False
        if settings is not None:
            fingerprint = get_episode_lookup_provider_fingerprint(settings, api_key)
            if fingerprint in _EPISODE_LOOKUP_CAPABILITY_PROOFS:
                _EPISODE_LOOKUP_CAPABILITY_PROOFS.pop(fingerprint, None)
                changed = True
        elif backend_kind is not None:
            stale_fingerprints = [
                fingerprint
                for fingerprint, proven_backend in _EPISODE_LOOKUP_CAPABILITY_PROOFS.items()
                if proven_backend == backend_kind
            ]
            for fingerprint in stale_fingerprints:
                _EPISODE_LOOKUP_CAPABILITY_PROOFS.pop(fingerprint, None)
                changed = True
        if changed:
            _persistEpisodeLookupCapabilityProofsLocked()


def invalidate_episode_lookup_capability_fingerprint(
    provider_fingerprint: str,
) -> None:
    """実際に試験・lookup へ使った provider fingerprint だけを失効する。"""

    with _EPISODE_LOOKUP_CAPABILITY_PROOFS_LOCK:
        _loadEpisodeLookupCapabilityProofsLocked()
        if provider_fingerprint in _EPISODE_LOOKUP_CAPABILITY_PROOFS:
            _EPISODE_LOOKUP_CAPABILITY_PROOFS.pop(provider_fingerprint, None)
            _persistEpisodeLookupCapabilityProofsLocked()


class AcpBackendNotImplementedError(RecordedSeriesAIError):
    """ACP バックエンドを安全に起動できない場合に送出する。"""

    def __init__(self, backend_kind: str) -> None:
        """ACP セットアップ失敗を監査可能な固定エラーへ正規化する。

        Args:
            backend_kind: 起動できなかった ACP バックエンド種別。
        """

        super().__init__('HostCLIStartFailed')
        self.backend_kind = backend_kind


def _create_backend(
    settings: RecordedSeriesSettings,
    *,
    api_key: str | None = None,
) -> RecordedSeriesAIBackend:
    """設定の主系ターゲットからバックエンドインスタンスを生成する。

    Args:
        settings: 判定開始時点の録画シリーズ判定設定（immutable snapshot）。
        api_key: 同じ時点で解決した API キー。OpenCode では未使用（secrets 参照）。

    Returns:
        RecordedSeriesAIBackend: 適切なバックエンドインスタンス。

    Raises:
        AcpBackendNotImplementedError: ACP バックエンドのコマンドが未設定の場合。
        RecordedSeriesAIError: OpenCode service が未設定の場合。
    """

    return CreateBackendForTarget(
        BuildPrimaryTarget(settings),
        api_key=api_key,
    )


def CreateBackendForTarget(
    target: AIBackendTarget,
    *,
    api_key: str | None = None,
) -> RecordedSeriesAIBackend:
    """主系・予備系のどちらにも使える backend 生成処理。

    予備 backend は失敗時にだけ呼び、通常成功時の負荷を増やさない。

    Args:
        target: 実行する backend 種別と service_id。
        api_key: 互換引数。OpenCode では未使用。

    Returns:
        RecordedSeriesAIBackend 実装。

    Raises:
        AcpBackendNotImplementedError: ACP バックエンドのコマンドが未設定の場合。
        RecordedSeriesAIError: OpenCode service が未設定の場合。
    """

    _ = api_key
    backend_kind: AIBackendKind = target.backend_kind
    if backend_kind == 'OpenCode':
        from app.metadata.ai.opencode_backend import BuildOpenCodeBackendFromServiceID

        service_id = target.service_id
        if service_id is None or service_id.strip() == '':
            raise RecordedSeriesAIError('OpenCodeServiceNotFound')
        return BuildOpenCodeBackendFromServiceID(service_id)

    # ACP バックエンド
    from app.metadata.ai.acp_presets import resolve_command
    from app.metadata.ai.acp_profiles import (
        AcpProfileError,
        ensure_acp_profile,
        get_profile_environment,
    )

    # モデル・推論深さ・Fast・タイムアウトは ACPSettings が正本（AI バックエンドページ管理）。
    from app.metadata.ai.ACPSettings import ACPSettingsStore

    acp_settings = ACPSettingsStore.getSettings().forBackend(backend_kind)

    # コマンド解決
    try:
        runtime_command, preset_args = resolve_command(backend_kind)
    except ValueError as ex:
        raise AcpBackendNotImplementedError(backend_kind) from ex

    # provider ごとの専用プロファイルを構築し、ホスト home と共有しない固定環境を取得する。
    profile_backend = _backend_to_profile_name(backend_kind)
    try:
        profile_dir = ensure_acp_profile(
            backend=profile_backend,
            konomitv_bs4k_fast_mode_enabled=acp_settings.codex_fast_mode_enabled,
        )
        profile_env = get_profile_environment(
            profile_backend,
            profile_dir,
        )
    except (AcpProfileError, OSError) as ex:
        # AI 監査予約後の profile 構築失敗を生例外にすると、Resolver の想定外失敗経路で
        # 監査だけが Pending に残る。固定コードへ正規化して既存の Failed 終端へ載せる。
        raise AcpBackendNotImplementedError(backend_kind) from ex

    # 固定プリセットは provider 専用 workspace だけを使用する。
    runtime_cwd = str(profile_dir / 'workspace')

    # 固定プリセットの引数を provider ごとの実行条件へ展開する。
    all_args = list(preset_args)

    # Grok Build はモデルが grok-4.5 固定のため、CLI の --reasoning-effort で深さを切り替える。
    # `grok --reasoning-effort {low,medium,high} agent stdio` の形になるよう先頭へ挿入する。
    if backend_kind == 'AcpGrok' and acp_settings.reasoning_effort is not None:
        all_args = [
            '--reasoning-effort',
            acp_settings.reasoning_effort.lower(),
            *all_args,
        ]

    return _AcpAdapter(
        backend_kind=backend_kind,
        command=runtime_command,
        args=all_args,
        env=profile_env,
        timeout_sec=acp_settings.timeout_sec,
        model=acp_settings.model,
        reasoning_effort=acp_settings.reasoning_effort,
        cwd=runtime_cwd,
        profile_dir=str(profile_dir),
    )


class _AcpAdapter:
    """ACP バックエンドのプロトコルアダプタ（内部クラス）。"""

    def __init__(
        self,
        backend_kind: str,
        command: str,
        args: list[str],
        env: dict[str, str],
        timeout_sec: int,
        model: str | None = None,
        reasoning_effort: str | None = None,
        cwd: str | None = None,
        profile_dir: str = '',
        readable_files: tuple[str, ...] = (),
    ) -> None:
        self._backend_kind = backend_kind
        self._command = command
        self._args = args
        self._env = env
        self._timeout_sec = timeout_sec
        self._model = model
        # Codex は session config、Grok は CLI 引数へ適用済みの推論深さ。
        # 成功結果と接続試験の監査ラベルを、実際に適用した条件から再現する。
        self._reasoning_effort = reasoning_effort
        self._cwd = cwd
        # Landlock launcher が書込みを許可する、選択中 provider 専用 profile。
        self._profile_dir = profile_dir
        # provider ごとに明示した read-only 資格情報ファイル（現状は未使用）。
        self._readable_files = readable_files

    @property
    def backend_kind(self) -> str:
        return self._backend_kind

    def _audit_model(self) -> str:
        """監査用のモデル文字列（backend prefix 付き）を返す。"""
        backend_prefix_map: dict[str, str] = {
            'AcpCodex': 'acp:codex',
            'AcpGrok': 'acp:grok',
        }
        prefix = backend_prefix_map.get(self._backend_kind, 'acp')
        if self._model:
            label = f'{prefix}:{self._model}'
        else:
            label = prefix
        # 推論深さを選んだ場合は Codex の [medium] 表記に合わせて括弧で付与する。
        if self._reasoning_effort:
            return f'{label}[{self._reasoning_effort.lower()}]'
        return label

    def _operation_args(
        self,
        operation: Literal['CandidateSelection', 'SeriesMetadata', 'EpisodeLookup'],
    ) -> list[str]:
        """共通 output schema を provider 固有 CLI の薄い受け口へ写像する。"""

        if self._backend_kind != 'AcpGrok':
            return self._args
        from app.metadata.ai.acp_client import BuildAcpOutputJSONSchemaArgument
        return [
            '--json-schema',
            BuildAcpOutputJSONSchemaArgument(operation),
            *self._args,
        ]

    async def selectCandidate(
        self,
        program: RecordedSeriesProgramPrompt,
        candidates: list[SeriesChoiceCandidate],
        *,
        minimum_confidence: float = 0.80,
    ) -> AIChoiceResult:
        from app.metadata.ai.acp_client import run_acp_candidate_selection
        # Grok の推論深さは起動引数へ適用済み。ACP session config へ渡すのは、
        # reasoning_effort の configOption を必ず広告する Codex だけに限定する。
        result = await run_acp_candidate_selection(
            command=self._command,
            args=self._operation_args('CandidateSelection'),
            env=self._env,
            program=program,
            candidates=candidates,
            model=self._model,
            reasoning_effort=(
                self._reasoning_effort
                if self._backend_kind == 'AcpCodex'
                else None
            ),
            timeout_sec=self._timeout_sec,
            minimum_confidence=minimum_confidence,
            cwd=self._cwd or self._env.get('HOME'),
            profile_dir=self._profile_dir,
            readable_files=self._readable_files,
            backend_kind=self._backend_kind,
        )
        # 監査用の model を backend prefix 付きで上書き
        return AIChoiceResult(
            choice_id=result.choice_id,
            confidence=result.confidence,
            model=self._audit_model(),
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            http_status=result.http_status,
            latency_ms=result.latency_ms,
        )

    async def resolveSeriesMetadata(
        self,
        program: RecordedSeriesProgramPrompt,
        hints: SeriesMetadataHints,
        *,
        prompt_variant: AIPromptVariant = 'Default',
        execution_guard: Callable[[], None] | None = None,
        local_validation_attempts: int = 2,
    ) -> AISeriesMetadataResult:
        """固定 preset の tool-free ACP turn でシリーズ情報を生成する。"""

        from app.metadata.ai.acp_client import run_acp_series_metadata

        # ACP credential lock 取得後、process 起動前に受付時の実行条件と再照合する。
        if execution_guard is not None:
            execution_guard()
        _ = local_validation_attempts

        result = await run_acp_series_metadata(
            command=self._command,
            args=self._operation_args('SeriesMetadata'),
            env=self._env,
            program=program,
            hints=hints,
            model=self._model,
            reasoning_effort=(
                self._reasoning_effort
                if self._backend_kind == 'AcpCodex'
                else None
            ),
            timeout_sec=self._timeout_sec,
            cwd=self._cwd or self._env.get('HOME'),
            profile_dir=self._profile_dir,
            readable_files=self._readable_files,
            backend_kind=self._backend_kind,
            prompt_variant=prompt_variant,
        )
        return replace(result, model=self._audit_model())

    async def lookupEpisode(
        self,
        program: RecordedEpisodeLookupContext,
        *,
        prompt_variant: AIPromptVariant = 'Default',
        execution_guard: Callable[[], None] | None = None,
    ) -> EpisodeLookupResult:
        """固定 preset の Web tool trace を検証して話数検索を実行する。"""

        from app.metadata.ai.acp_client import run_acp_episode_lookup

        # ACP credential lock 取得後、process 起動前に受付時の実行条件と再照合する。
        if execution_guard is not None:
            execution_guard()

        result = await run_acp_episode_lookup(
            command=self._command,
            args=self._operation_args('EpisodeLookup'),
            env=self._env,
            program=program,
            backend_kind=self._backend_kind,
            model=self._model,
            reasoning_effort=(
                self._reasoning_effort
                if self._backend_kind == 'AcpCodex'
                else None
            ),
            timeout_sec=self._timeout_sec,
            cwd=self._cwd or self._env.get('HOME'),
            profile_dir=self._profile_dir,
            readable_files=self._readable_files,
            prompt_variant=prompt_variant,
        )
        return replace(result, model=self._audit_model())

    async def testConnection(
        self,
        capability: str,
    ) -> ConnectionTestResult:
        if capability == 'EpisodeLookup':
            from app.metadata.ai.acp_client import (
                run_acp_episode_lookup_connection_test,
            )

            result = await run_acp_episode_lookup_connection_test(
                command=self._command,
                args=self._operation_args('EpisodeLookup'),
                env=self._env,
                backend_kind=self._backend_kind,
                model=self._model,
                reasoning_effort=(
                    self._reasoning_effort
                    if self._backend_kind == 'AcpCodex'
                    else None
                ),
                timeout_sec=self._timeout_sec,
                cwd=self._cwd or self._env.get('HOME'),
                profile_dir=self._profile_dir,
                readable_files=self._readable_files,
            )
            checks = result.checks
            message = result.message
            if checks is None:
                if result.success:
                    checks = EpisodeLookupConnectionChecks(
                        backend_connection=ConnectionTestCheck(
                            status='Passed',
                            message='ACP backend との接続を確認しました。',
                        ),
                        web_search=ConnectionTestCheck(
                            status='Passed',
                            message='検証済み ACP Web tool の完了を確認しました。',
                        ),
                        source_url=ConnectionTestCheck(
                            status='Passed',
                            message='完了した Web tool trace から公開 HTTP(S) URL を取得しました。',
                        ),
                        strict_schema=ConnectionTestCheck(
                            status='Passed',
                            message='strict schema に適合するモデル出力を確認しました。',
                        ),
                        timeout_cancel=ConnectionTestCheck(
                            status='NotRun',
                            message='通常応答の試験では timeout / cancel 回収を意図的に発生させていません。',
                        ),
                        permission_policy=ConnectionTestCheck(
                            status='NotRun',
                            message='通常応答だけでは危険権限の拒否 policy を個別実測していません。',
                        ),
                    )
                    message = 'ACP 接続、Web 検索、検索元 URL、strict schema を確認しました。'
                else:
                    not_run = ConnectionTestCheck(
                        status='NotRun',
                        message='ACP の一括結果だけではこの項目を個別判定できませんでした。',
                    )
                    checks = EpisodeLookupConnectionChecks(
                        backend_connection=not_run,
                        web_search=not_run,
                        source_url=not_run,
                        strict_schema=not_run,
                        timeout_cancel=not_run,
                        permission_policy=not_run,
                    )
            return replace(
                result,
                model=self._audit_model(),
                message=message,
                checks=checks,
            )
        from app.metadata.ai.acp_client import run_acp_connection_test
        # API の capability 名は互換維持で CandidateSelection のままだが、
        # 接続試験の中身は本番と同じシリーズ情報生成 schema へ固定する。
        result = await run_acp_connection_test(
            command=self._command,
            args=self._operation_args('SeriesMetadata'),
            env=self._env,
            model=self._model,
            reasoning_effort=(
                self._reasoning_effort
                if self._backend_kind == 'AcpCodex'
                else None
            ),
            timeout_sec=self._timeout_sec,
            cwd=self._cwd or self._env.get('HOME'),
            profile_dir=self._profile_dir,
            readable_files=self._readable_files,
            backend_kind=self._backend_kind,
        )
        return replace(result, model=self._audit_model())


def _backend_to_profile_name(
    backend_kind: str,
) -> Literal['codex', 'grok']:
    """バックエンド種別をプロファイルディレクトリ名に変換する。"""
    mapping: dict[str, Literal['codex', 'grok']] = {
        'AcpCodex': 'codex',
        'AcpGrok': 'grok',
    }
    try:
        return mapping[backend_kind]
    except KeyError as ex:
        raise AcpBackendNotImplementedError(backend_kind) from ex


def GetACPCredentialOperationLock(
    provider: KonomiTVBS4KACPImportProvider,
) -> asyncio.Lock:
    """指定 provider の実行と認証変更を排他する lock を返す。

    Args:
        provider: Codex または Grok の資格情報 provider。

    Returns:
        provider 専用の process-local asyncio lock。
    """

    return ACP_CREDENTIAL_OPERATION_LOCKS[provider]


def IsACPOperationRunning() -> bool:
    """公開 facade で ACP agent が1件実行中かを返す。

    Returns:
        ACP の直列実行 lock が保持されている場合は True。
    """

    return ACP_OPERATION_LOCK.locked()


def _GetACPCredentialProvider(
    backend_kind: str,
) -> KonomiTVBS4KACPImportProvider | None:
    """backend が使用する可変 credential provider を返す。

    Args:
        backend_kind: 録画シリーズ AI backend の識別子。

    Returns:
        Codex / Grok の provider。それ以外は None。
    """

    mapping: dict[str, KonomiTVBS4KACPImportProvider] = {
        'AcpCodex': 'codex',
        'AcpGrok': 'grok',
    }
    return mapping.get(backend_kind)


# === 公開 facade 関数 ===


async def select_candidate(
    program: RecordedSeriesProgramPrompt,
    candidates: list[SeriesChoiceCandidate],
    *,
    minimum_confidence: float = 0.80,
    settings: RecordedSeriesSettings | None = None,
    api_key: str | None = None,
) -> AIChoiceResult:
    """バックエンド非依存の候補選択。

    設定から適切なバックエンドを選択し、候補選択を実行する。

    Args:
        program: 録画番組メタデータ。
        candidates: 選択候補リスト。
        minimum_confidence: 最低信頼度。
        settings: 判定開始時の設定 snapshot。未指定時はここで取得する。
        api_key: 同じ時点の API キー snapshot。settings 指定時に併用する。

    Returns:
        AIChoiceResult: 検証済みの選択結果。

    Raises:
        RecordedSeriesAIError: ACP セットアップまたは AI 呼び出しの失敗。
    """
    # 判定1回分の settings/key を固定し、backend 内で再取得して世代がずれないようにする。
    if settings is None:
        settings, api_key = RecordedSeriesSettingsStore.getSettingsAndAPIKey()

    async def RunSelection() -> AIChoiceResult:
        backend = _create_backend(settings, api_key=api_key)
        return await backend.selectCandidate(
            program,
            candidates,
            minimum_confidence=minimum_confidence,
        )

    if settings.ai_backend == 'OpenCode':
        # Phase 2 で OpenCodeBackend を直接呼ぶ（ACP 直列 lock は使わない）。
        result = await RunSelection()
    else:
        result = await _RunACPOperationWithDeadline(
            RunSelection,
            _GetACPCredentialProvider(settings.ai_backend),
        )
    return AIChoiceResult(
        choice_id=result.choice_id,
        confidence=result.confidence,
        model=get_audit_model(settings),
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        http_status=result.http_status,
        latency_ms=result.latency_ms,
    )


async def _RunBackendOperation(
    target: AIBackendTarget,
    operation: Callable[[RecordedSeriesAIBackend], Awaitable[_AcpOperationResult]],
    *,
    api_key: str | None = None,
    acp_hard_deadline: float | None = None,
) -> _AcpOperationResult:
    """指定ターゲットの backend を生成して操作を実行する。

    OpenCode は直接実行し、ACP は直列 lock と credential lock を適用する。
    backend は失敗時にだけ予備用ターゲットから生成し、成功時の負荷を増やさない。

    Args:
        target: 主系または予備系の実行ターゲット。
        operation: backend を受け取り結果を返す非同期処理。
        api_key: 互換引数。OpenCode では未使用。
        acp_hard_deadline: 同一判定の全 ACP 試行で共有する絶対期限。

    Returns:
        operation の戻り値。
    """

    async def Run() -> _AcpOperationResult:
        backend = CreateBackendForTarget(target, api_key=api_key)
        return await operation(backend)

    if target.backend_kind == 'OpenCode':
        return await Run()
    return await _RunACPOperationWithDeadline(
        Run,
        _GetACPCredentialProvider(target.backend_kind),
        hard_deadline=acp_hard_deadline,
    )


def _BuildSeriesAttemptSummary(
    *,
    attempt_number: int,
    target: AIBackendTarget,
    result: AISeriesMetadataResult | None,
    error: RecordedSeriesAIError | None,
    adopted: bool,
) -> AIRecoveryAttemptSummary:
    """シリーズ生成の1試行サマリを構築する。"""

    if result is not None:
        return AIRecoveryAttemptSummary(
            attempt_number=attempt_number,
            role=target.role,
            backend_kind=target.backend_kind,
            service_id=target.service_id,
            model=result.model,
            result_code=SeriesResultCode(result),
            succeeded=True,
            adopted=adopted,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=result.latency_ms,
            http_status=result.http_status,
            error_code=None,
        )
    assert error is not None
    return AIRecoveryAttemptSummary(
        attempt_number=attempt_number,
        role=target.role,
        backend_kind=target.backend_kind,
        service_id=target.service_id,
        model=get_audit_model_for_target(target),
        result_code=error.code,
        succeeded=False,
        adopted=adopted,
        prompt_tokens=None,
        completion_tokens=None,
        latency_ms=error.latency_ms,
        http_status=error.http_status,
        error_code=error.code,
    )


def _BuildEpisodeAttemptSummary(
    *,
    attempt_number: int,
    target: AIBackendTarget,
    result: EpisodeLookupResult | None,
    error: RecordedSeriesAIError | None,
    adopted: bool,
) -> AIRecoveryAttemptSummary:
    """話数検索の1試行サマリを構築する。"""

    if result is not None:
        return AIRecoveryAttemptSummary(
            attempt_number=attempt_number,
            role=target.role,
            backend_kind=target.backend_kind,
            service_id=target.service_id,
            model=result.model,
            result_code=EpisodeResultCode(result),
            succeeded=result.outcome in {
                'Resolved',
                'NotNumbered',
                'NoPublishedNumber',
                'InsufficientEvidence',
            },
            adopted=adopted,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=result.latency_ms,
            http_status=result.http_status,
            error_code=result.error_code,
        )
    assert error is not None
    return AIRecoveryAttemptSummary(
        attempt_number=attempt_number,
        role=target.role,
        backend_kind=target.backend_kind,
        service_id=target.service_id,
        model=get_audit_model_for_target(target),
        result_code=error.code,
        succeeded=False,
        adopted=adopted,
        prompt_tokens=None,
        completion_tokens=None,
        latency_ms=error.latency_ms,
        http_status=error.http_status,
        error_code=error.code,
    )


def _LogRecoveryAttempts(purpose: str, summaries: list[AIRecoveryAttemptSummary]) -> None:
    """試行ごとの監査情報を英語ログへ残す。"""

    trail = '; '.join(FormatRecoveryAttemptSummary(item) for item in summaries)
    logging.info(
        f'[RecordedSeriesAI] {purpose} recovery attempts: {trail}',
    )


def _SumOptionalAttemptMetric(
    summaries: list[AIRecoveryAttemptSummary],
    metric: Literal['prompt_tokens', 'completion_tokens', 'latency_ms'],
) -> int | None:
    """全試行の監査値を、値が1件以上ある場合だけ合算する。

    Args:
        summaries: 採否を含む全試行サマリ。
        metric: 合算する token または遅延フィールド。

    Returns:
        合算値。全試行が未計測なら None。
    """

    if metric == 'prompt_tokens':
        values = [
            summary.prompt_tokens
            for summary in summaries
            if summary.prompt_tokens is not None
        ]
    elif metric == 'completion_tokens':
        values = [
            summary.completion_tokens
            for summary in summaries
            if summary.completion_tokens is not None
        ]
    else:
        values = [
            summary.latency_ms
            for summary in summaries
            if summary.latency_ms is not None
        ]
    return sum(values) if len(values) > 0 else None


def _FormatRecoveryAttemptSummaries(
    summaries: list[AIRecoveryAttemptSummary],
) -> tuple[str, ...]:
    """全試行を永続監査向けの安全な文字列列へ変換する。"""

    return tuple(FormatRecoveryAttemptSummary(item) for item in summaries)


def _ApplySeriesAttemptAudit(
    result: AISeriesMetadataResult,
    summaries: list[AIRecoveryAttemptSummary],
) -> AISeriesMetadataResult:
    """採用結果へ全試行分の利用量・遅延・試行列を集約する。"""

    return replace(
        result,
        prompt_tokens=_SumOptionalAttemptMetric(summaries, 'prompt_tokens'),
        completion_tokens=_SumOptionalAttemptMetric(summaries, 'completion_tokens'),
        latency_ms=_SumOptionalAttemptMetric(summaries, 'latency_ms') or 0,
        recovery_attempt_summaries=_FormatRecoveryAttemptSummaries(summaries),
    )


def _ApplyEpisodeAttemptAudit(
    result: EpisodeLookupResult,
    summaries: list[AIRecoveryAttemptSummary],
) -> EpisodeLookupResult:
    """採用結果へ全試行分の利用量・遅延・試行列を集約する。"""

    return replace(
        result,
        prompt_tokens=_SumOptionalAttemptMetric(summaries, 'prompt_tokens'),
        completion_tokens=_SumOptionalAttemptMetric(summaries, 'completion_tokens'),
        latency_ms=_SumOptionalAttemptMetric(summaries, 'latency_ms') or 0,
        recovery_attempt_summaries=_FormatRecoveryAttemptSummaries(summaries),
    )


async def resolve_series_metadata(
    program: RecordedSeriesProgramPrompt,
    hints: SeriesMetadataHints,
    *,
    settings: RecordedSeriesSettings | None = None,
    api_key: str | None = None,
) -> AISeriesMetadataResult:
    """バックエンド非依存でシリーズ名・話数・話名を一括生成する。

    主系を通常プロンプトで実行し、技術的失敗または Unresolved のときだけ
    失敗時ポリシー（最大 2 試行）を適用する。NotSeries / Series は切り替えない。

    Args:
        program: 録画番組メタデータ。
        hints: サーバーが固定したローカル・既存 Series・Wikipedia の参考情報。
        settings: 判定開始時の設定 snapshot。未指定時はここで取得する。
        api_key: 同じ時点の API キー snapshot。settings 指定時に併用する。

    Returns:
        最小 schema と hints 内 ID 制約を検証済みの生成結果。
        recovery_attempt_summaries に試行列を含む。

    Raises:
        RecordedSeriesAIError: 最終試行までの AI 呼び出し失敗。
    """

    if settings is None:
        settings, api_key = RecordedSeriesSettingsStore.getSettingsAndAPIKey()

    primary = BuildPrimaryTarget(settings)
    expected_execution_fingerprint = GetAIExecutionFingerprint(settings)
    summaries: list[AIRecoveryAttemptSummary] = []
    acp_hard_deadline: float | None = None

    async def RunGeneration(
        target: AIBackendTarget,
    ) -> AISeriesMetadataResult:
        nonlocal acp_hard_deadline
        # 同一判定で ACP を複数回使っても、最初の ACP 試行からの絶対期限を共有する。
        if target.backend_kind != 'OpenCode' and acp_hard_deadline is None:
            acp_hard_deadline = (
                asyncio.get_running_loop().time() + _ACP_OPERATION_HARD_TIMEOUT_SEC
            )

        async def Operation(backend: RecordedSeriesAIBackend) -> AISeriesMetadataResult:
            return await backend.resolveSeriesMetadata(
                program,
                hints,
                prompt_variant=target.prompt_variant,
                execution_guard=lambda: _VerifyAIExecutionFingerprint(
                    settings,
                    expected_execution_fingerprint,
                ),
                # Fail と FallbackBackend の主系では、従来の schema 自己修復を維持する。
                # RetrySameBackend の各試行と FallbackBackend の予備は1回に限定し、
                # 外側の失敗時ポリシーによる主系・回復系を最大2試行に固定する。
                local_validation_attempts=(
                    2
                    if (
                        settings.ai_failure_recovery_strategy == 'Fail'
                        or (
                            settings.ai_failure_recovery_strategy == 'FallbackBackend'
                            and target.role == 'Primary'
                        )
                    )
                    else 1
                ),
            )

        result = await _RunBackendOperation(
            target,
            Operation,
            api_key=api_key,
            acp_hard_deadline=acp_hard_deadline,
        )
        # 監査 model は実際に使った target から付与する（予備切替時に主系ラベルへ戻さない）。
        return replace(result, model=get_audit_model_for_target(target))

    first_result: AISeriesMetadataResult | None = None
    first_error: RecordedSeriesAIError | None = None
    try:
        first_result = await RunGeneration(primary)
    except RecordedSeriesAIError as ex:
        first_error = ex

    needs_recovery = False
    if first_result is not None:
        needs_recovery = ShouldRecoverSeriesMetadataResult(first_result)
    elif first_error is not None:
        needs_recovery = ShouldRecoverSeriesMetadataError(first_error)

    recovery_target = BuildRecoveryTarget(settings) if needs_recovery else None
    if recovery_target is None:
        # Fail 方針、または回復不要。1 試行で終了する。
        if first_error is not None:
            summary = _BuildSeriesAttemptSummary(
                attempt_number=1,
                target=primary,
                result=None,
                error=first_error,
                adopted=True,
            )
            _LogRecoveryAttempts('SeriesMetadata', [summary])
            raise RecordedSeriesAIError(
                first_error.code,
                http_status=first_error.http_status,
                latency_ms=first_error.latency_ms,
                recovery_attempt_summaries=(FormatRecoveryAttemptSummary(summary),),
            ) from first_error
        assert first_result is not None
        summary = _BuildSeriesAttemptSummary(
            attempt_number=1,
            target=primary,
            result=first_result,
            error=None,
            adopted=True,
        )
        _LogRecoveryAttempts('SeriesMetadata', [summary])
        return _ApplySeriesAttemptAudit(first_result, [summary])

    # 1 回目は採用せず記録だけ残し、2 回目へ進む（最大 2 試行固定）。
    summaries.append(
        _BuildSeriesAttemptSummary(
            attempt_number=1,
            target=primary,
            result=first_result,
            error=first_error,
            adopted=False,
        ),
    )
    logging.info(
        f'[RecordedSeriesAI] SeriesMetadata applying recovery strategy='
        f'{settings.ai_failure_recovery_strategy} role={recovery_target.role} '
        f'backend={recovery_target.backend_kind}',
    )
    try:
        second_result = await RunGeneration(recovery_target)
    except RecordedSeriesAIError as second_error:
        # 主系が正常な Unresolved を返していた場合、回復試行の技術障害で
        # その判定を Failed へ劣化させず、主系結果を監査付きで採用する。
        preserve_first_result = (
            first_result is not None
            and ShouldRecoverSeriesMetadataError(second_error)
        )
        if preserve_first_result:
            summaries[0] = replace(summaries[0], adopted=True)
        summaries.append(
            _BuildSeriesAttemptSummary(
                attempt_number=2,
                target=recovery_target,
                result=None,
                error=second_error,
                adopted=preserve_first_result is False,
            ),
        )
        _LogRecoveryAttempts('SeriesMetadata', summaries)
        if first_result is not None and preserve_first_result:
            return _ApplySeriesAttemptAudit(first_result, summaries)
        raise RecordedSeriesAIError(
            second_error.code,
            http_status=second_error.http_status,
            latency_ms=_SumOptionalAttemptMetric(summaries, 'latency_ms'),
            recovery_attempt_summaries=_FormatRecoveryAttemptSummaries(summaries),
        ) from second_error

    summaries.append(
        _BuildSeriesAttemptSummary(
            attempt_number=2,
            target=recovery_target,
            result=second_result,
            error=None,
            adopted=True,
        ),
    )
    _LogRecoveryAttempts('SeriesMetadata', summaries)
    return _ApplySeriesAttemptAudit(second_result, summaries)


async def lookup_episode(
    program: RecordedEpisodeLookupContext,
    *,
    settings: RecordedSeriesSettings | None = None,
    api_key: str | None = None,
    expected_provider_fingerprint: str | None = None,
) -> EpisodeLookupResult:
    """バックエンド非依存の話数検索。

    主系を通常プロンプトで実行し、InsufficientEvidence または技術的失敗のときだけ
    失敗時ポリシー（最大 2 試行）を適用する。Resolved / NotNumbered /
    NoPublishedNumber は切り替えない。予備 AI には主系の失敗理由を渡さない。

    Args:
        program: 録画番組メタデータ。
        settings: 判定開始時の設定 snapshot。未指定時はここで取得する。
        api_key: 同じ時点の API キー snapshot。settings 指定時に併用する。
        expected_provider_fingerprint: Automation が永続化する provider。

    Returns:
        EpisodeLookupResult: 検証済みの話数検索結果。
        recovery_attempt_summaries に試行列を含む。

    Raises:
        RecordedSeriesAIError: 設定変更検知など、Result へ正規化できない失敗。
    """
    if settings is None:
        settings, api_key = RecordedSeriesSettingsStore.getSettingsAndAPIKey()

    # fingerprint は主系・予備系の実設定と認証世代を含む。
    if expected_provider_fingerprint is not None and (
        get_episode_lookup_provider_fingerprint(settings, api_key)
        != expected_provider_fingerprint
    ):
        raise RecordedSeriesAIError('AISettingsChangedBeforeRequest')

    primary = BuildPrimaryTarget(settings)
    expected_execution_fingerprint = GetAIExecutionFingerprint(settings)
    summaries: list[AIRecoveryAttemptSummary] = []
    acp_hard_deadline: float | None = None

    async def RunLookup(target: AIBackendTarget) -> EpisodeLookupResult:
        nonlocal acp_hard_deadline
        # RetrySameBackend / ACP 予備のどちらも、最初の ACP 試行からの期限を共有する。
        if target.backend_kind != 'OpenCode' and acp_hard_deadline is None:
            acp_hard_deadline = (
                asyncio.get_running_loop().time() + _ACP_OPERATION_HARD_TIMEOUT_SEC
            )

        async def Operation(backend: RecordedSeriesAIBackend) -> EpisodeLookupResult:
            return await backend.lookupEpisode(
                program,
                prompt_variant=target.prompt_variant,
                execution_guard=lambda: _VerifyAIExecutionFingerprint(
                    settings,
                    expected_execution_fingerprint,
                ),
            )

        result = await _RunBackendOperation(
            target,
            Operation,
            api_key=api_key,
            acp_hard_deadline=acp_hard_deadline,
        )
        return replace(result, model=get_audit_model_for_target(target))

    first_result: EpisodeLookupResult | None = None
    first_error: RecordedSeriesAIError | None = None
    try:
        first_result = await RunLookup(primary)
    except RecordedSeriesAIError as ex:
        first_error = ex

    needs_recovery = False
    if first_result is not None:
        needs_recovery = ShouldRecoverEpisodeLookupResult(first_result)
    elif first_error is not None:
        needs_recovery = ShouldRecoverEpisodeLookupError(first_error)

    recovery_target = BuildRecoveryTarget(settings) if needs_recovery else None
    if recovery_target is None:
        if first_error is not None:
            summary = _BuildEpisodeAttemptSummary(
                attempt_number=1,
                target=primary,
                result=None,
                error=first_error,
                adopted=True,
            )
            _LogRecoveryAttempts('EpisodeLookup', [summary])
            raise RecordedSeriesAIError(
                first_error.code,
                http_status=first_error.http_status,
                latency_ms=first_error.latency_ms,
                recovery_attempt_summaries=(FormatRecoveryAttemptSummary(summary),),
            ) from first_error
        assert first_result is not None
        summary = _BuildEpisodeAttemptSummary(
            attempt_number=1,
            target=primary,
            result=first_result,
            error=None,
            adopted=True,
        )
        _LogRecoveryAttempts('EpisodeLookup', [summary])
        return _ApplyEpisodeAttemptAudit(first_result, [summary])

    summaries.append(
        _BuildEpisodeAttemptSummary(
            attempt_number=1,
            target=primary,
            result=first_result,
            error=first_error,
            adopted=False,
        ),
    )
    logging.info(
        f'[RecordedSeriesAI] EpisodeLookup applying recovery strategy='
        f'{settings.ai_failure_recovery_strategy} role={recovery_target.role} '
        f'backend={recovery_target.backend_kind}',
    )
    try:
        second_result = await RunLookup(recovery_target)
    except RecordedSeriesAIError as second_error:
        preserve_first_result = (
            first_result is not None
            and first_result.outcome == 'InsufficientEvidence'
            and ShouldRecoverEpisodeLookupError(second_error)
        )
        if preserve_first_result:
            summaries[0] = replace(summaries[0], adopted=True)
        summaries.append(
            _BuildEpisodeAttemptSummary(
                attempt_number=2,
                target=recovery_target,
                result=None,
                error=second_error,
                adopted=preserve_first_result is False,
            ),
        )
        _LogRecoveryAttempts('EpisodeLookup', summaries)
        if first_result is not None and preserve_first_result:
            return _ApplyEpisodeAttemptAudit(first_result, summaries)
        raise RecordedSeriesAIError(
            second_error.code,
            http_status=second_error.http_status,
            latency_ms=_SumOptionalAttemptMetric(summaries, 'latency_ms'),
            recovery_attempt_summaries=_FormatRecoveryAttemptSummaries(summaries),
        ) from second_error

    # lookup backend は技術障害も Result へ正規化する。主系の正常な
    # InsufficientEvidence を、回復先の SearchFailed 等で上書きしない。
    second_is_technical_failure = second_result.outcome not in {
        'Resolved',
        'NotNumbered',
        'NoPublishedNumber',
        'InsufficientEvidence',
    }
    preserve_first_result = (
        first_result is not None
        and first_result.outcome == 'InsufficientEvidence'
        and second_is_technical_failure
        and ShouldRecoverEpisodeLookupResult(second_result)
    )
    if preserve_first_result:
        summaries[0] = replace(summaries[0], adopted=True)

    summaries.append(
        _BuildEpisodeAttemptSummary(
            attempt_number=2,
            target=recovery_target,
            result=second_result,
            error=None,
            adopted=preserve_first_result is False,
        ),
    )
    _LogRecoveryAttempts('EpisodeLookup', summaries)
    adopted_result = first_result if preserve_first_result else second_result
    assert adopted_result is not None
    return _ApplyEpisodeAttemptAudit(
        adopted_result,
        summaries,
    )


async def test_connection(
    capability: str,
    *,
    settings: RecordedSeriesSettings | None = None,
    api_key: str | None = None,
) -> ConnectionTestResult:
    """バックエンド非依存の接続試験。

    Args:
        capability: 'CandidateSelection' または 'EpisodeLookup'。
        settings: 接続試験に使う設定 snapshot。未指定時は保存済み設定。
        api_key: 未使用（互換引数）。settings 指定時に併用する。

    Returns:
        ConnectionTestResult: 接続試験結果。

    Note:
        OpenCode は service 単位 backend で接続試験する。draft 一時キー試験は
        AIBackendRouter の connection-test API を使う。
    """
    if settings is None:
        settings, api_key = RecordedSeriesSettingsStore.getSettingsAndAPIKey()

    async def RunTest() -> ConnectionTestResult:
        provider_fingerprint = get_episode_lookup_provider_fingerprint(
            settings,
            api_key,
        )
        backend = _create_backend(settings, api_key=api_key)
        result = await backend.testConnection(capability)
        return replace(
            result,
            provider_fingerprint=provider_fingerprint,
        )

    if settings.ai_backend == 'OpenCode':
        return await RunTest()
    try:
        return await _RunACPOperationWithDeadline(
            RunTest,
            _GetACPCredentialProvider(settings.ai_backend),
        )
    except RecordedSeriesAIError as ex:
        if ex.code != 'HardTimeout':
            raise

        # lock 待機中なら backend process は未起動で、実行中なら acp_client が cleanup を完了してから戻る。
        # EpisodeLookup の6項目は到達段階を推測せず、期限制御だけを Passed とする。
        checks: EpisodeLookupConnectionChecks | None = None
        if capability == 'EpisodeLookup':
            not_run = ConnectionTestCheck(
                status='NotRun',
                message='総実行時間の安全上限により、この能力の確認を完了できませんでした。',
            )
            checks = EpisodeLookupConnectionChecks(
                backend_connection=not_run,
                web_search=not_run,
                source_url=not_run,
                strict_schema=not_run,
                timeout_cancel=ConnectionTestCheck(
                    status='Passed',
                    message='絶対実行時間の安全上限を ACP credential / 実行待ちにも適用しました。',
                ),
                permission_policy=not_run,
            )
        return ConnectionTestResult(
            success=False,
            latency_ms=ex.latency_ms or 0,
            model=get_audit_model(settings),
            message=FormatAcpHardTimeoutMessage(subject='ACP'),
            checks=checks,
            error_code='HardTimeout',
        )


def get_backend_kind() -> str:
    """現在のバックエンド種別を返す。"""
    settings = RecordedSeriesSettingsStore.getSettings()
    return settings.ai_backend


def get_audit_model_for_target(target: AIBackendTarget) -> str:
    """実行ターゲットから監査用モデル文字列を返す。

    Args:
        target: 主系または予備系の実行ターゲット。

    Returns:
        backend 種別とモデルを識別できる監査ラベル。
    """

    if target.backend_kind == 'OpenCode':
        service_id = target.service_id
        if service_id is None:
            return 'opencode'
        try:
            from app.metadata.ai.AIBackendSettings import AIBackendSettingsStore
            service = AIBackendSettingsStore.getService(service_id)
        except (OSError, ValueError):
            service = None
        if service is None:
            return f'opencode:service:{service_id}'
        return service.getAuditModelLabel()

    backend_prefix_map: dict[str, str] = {
        'AcpCodex': 'acp:codex',
        'AcpGrok': 'acp:grok',
    }
    prefix = backend_prefix_map.get(target.backend_kind, 'acp')
    from app.metadata.ai.ACPSettings import ACPSettingsStore

    acp_settings = ACPSettingsStore.getSettings().forBackend(target.backend_kind)
    acp_model = acp_settings.model
    if acp_model:
        label = f'{prefix}:{acp_model}'
    else:
        label = prefix
    reasoning_effort = acp_settings.reasoning_effort
    if reasoning_effort is not None:
        return f'{label}[{reasoning_effort.lower()}]'
    return label


def get_audit_model(settings: RecordedSeriesSettings | None = None) -> str:
    """監査用のモデル文字列（backend prefix 付き）を返す。

    ACP: "acp:codex:claude-sonnet-4-5" または "acp:codex"
    OpenCode: "opencode:{provider}/{model}" または service 未設定時 "opencode"

    Args:
        settings: 監査ラベルへ変換する設定。未指定時は保存済み設定を使用する。

    Returns:
        主系 backend 種別とモデルを識別できる監査ラベル。
    """

    effective_settings = settings or RecordedSeriesSettingsStore.getSettings()
    return get_audit_model_for_target(BuildPrimaryTarget(effective_settings))
