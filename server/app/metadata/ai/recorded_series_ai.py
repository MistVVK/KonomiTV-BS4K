"""録画シリーズ AI 統合 facade。

プロバイダー非依存のインターフェースを提供し、
設定に基づいて適切なバックエンド（OpenCode / ACP）へ routing する。
"""

from __future__ import annotations

import asyncio
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

from app import logging
from app.constants import DATA_DIR
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


async def _RunACPOperationWithDeadline(
    operation: Callable[[], Awaitable[_AcpOperationResult]],
    credential_provider: KonomiTVBS4KACPImportProvider | None,
) -> _AcpOperationResult:
    """直列実行待ちと credential lock 待機を含む ACP 公開操作へ絶対期限を適用する。

    Args:
        operation: lock 取得後に backend を生成して実行する非同期処理。
        credential_provider: 実行中の世代変更を止める Codex / Grok provider。
            Gemini は管理 API から ADC を変更しないため None。

    Returns:
        backend が返した操作結果。

    Raises:
        RecordedSeriesAIError: lock 待機から cleanup 完了までが安全上限を超えた場合。
    """

    started_at = time.monotonic()
    try:
        # 全 ACP の直列実行待ちと、対象 provider の認証排他待ちを総実行時間に含める。
        # backend は両 lock 取得後に生成し、期限切れ要求が新しい ACP process を起動しないようにする。
        async with asyncio.timeout(_ACP_OPERATION_HARD_TIMEOUT_SEC):
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


def get_episode_lookup_provider_fingerprint(
    settings: RecordedSeriesSettings,
    api_key: str | None,
) -> str:
    """接続試験と実行前検証で共有する、安全な能力証明キーを返す。"""

    if settings.ai_backend == 'OpenCode':
        # OpenCode は service 定義（provider/model/auth）が変わると旧 proof を失効させる。
        from app.metadata.ai.AIBackendSettings import AIBackendSettingsStore

        service = None
        if settings.ai_backend_service_id is not None:
            service = AIBackendSettingsStore.getService(settings.ai_backend_service_id)
        endpoint_identifier = json.dumps(
            {
                'backend': 'OpenCode',
                'service_id': settings.ai_backend_service_id,
                'provider_type': (
                    service.opencode_provider_type if service is not None else None
                ),
                'provider_id': (
                    service.opencode_provider_id if service is not None else None
                ),
                'model_id': (
                    service.opencode_model_id if service is not None else None
                ),
                'model_variant': (
                    service.opencode_model_variant if service is not None else None
                ),
                'structured_output_mode': (
                    service.structured_output_mode if service is not None else None
                ),
                'auth_mode': service.auth_mode if service is not None else None,
                'api_base_url': service.api_base_url if service is not None else None,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        backend_kind_for_fingerprint = 'OpenCode'
        effective_api_key: str | None = None
    else:
        # ACP は credential 世代と profile 名を fingerprint に含める。
        # 検証した世代と実際に CLI が読む世代を一致させるための核になる。
        credential_provider = _GetACPCredentialProvider(settings.ai_backend)
        endpoint_identifier = json.dumps(
            {
                'profile': f'{settings.ai_backend}:recorded-series-profile',
                'credential_generation': (
                    KonomiTVBS4KACPCredentials.getCredentialGeneration(
                        credential_provider,
                    )
                    if credential_provider is not None
                    else None
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        backend_kind_for_fingerprint = settings.ai_backend
        effective_api_key = None
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
    """設定に基づいてバックエンドインスタンスを生成する。

    OpenCode は service_id から OpenCodeBackend を構築する。
    AcpCodex / AcpGrok は従来どおり ACP アダプタを生成する。

    Args:
        settings: 判定開始時点の録画シリーズ判定設定（immutable snapshot）。
        api_key: 同じ時点で解決した API キー。OpenCode では未使用（secrets 参照）。

    Returns:
        RecordedSeriesAIBackend: 適切なバックエンドインスタンス。

    Raises:
        AcpBackendNotImplementedError: ACP バックエンドのコマンドが未設定の場合。
        RecordedSeriesAIError: OpenCode service が未設定の場合。
    """
    if settings.ai_backend == 'OpenCode':
        from app.metadata.ai.opencode_backend import BuildOpenCodeBackendFromServiceID

        _ = api_key
        service_id = settings.ai_backend_service_id
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

    acp_settings = ACPSettingsStore.getSettings().forBackend(settings.ai_backend)

    # コマンド解決
    try:
        runtime_command, preset_args = resolve_command(
            settings.ai_backend,
        )
    except ValueError as ex:
        raise AcpBackendNotImplementedError(settings.ai_backend) from ex

    # provider ごとの専用プロファイルを構築し、ホスト home と共有しない固定環境を取得する。
    profile_backend = _backend_to_profile_name(settings.ai_backend)
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
        raise AcpBackendNotImplementedError(settings.ai_backend) from ex

    # 固定プリセットは provider 専用 workspace だけを使用する。
    runtime_cwd = str(profile_dir / 'workspace')

    # ACP バックエンドアダプタを生成

    # 固定プリセットの引数を provider ごとの実行条件へ展開する。
    all_args = list(preset_args)

    # Grok Build はモデルが grok-4.5 固定のため、CLI の --reasoning-effort で深さを切り替える。
    # `grok --reasoning-effort {low,medium,high} agent stdio` の形になるよう先頭へ挿入する。
    if settings.ai_backend == 'AcpGrok' and acp_settings.reasoning_effort is not None:
        all_args = [
            '--reasoning-effort',
            acp_settings.reasoning_effort.lower(),
            *all_args,
        ]

    return _AcpAdapter(
        backend_kind=settings.ai_backend,
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
    ) -> AISeriesMetadataResult:
        """固定 preset の tool-free ACP turn でシリーズ情報を生成する。"""

        from app.metadata.ai.acp_client import run_acp_series_metadata

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
        )
        return replace(result, model=self._audit_model())

    async def lookupEpisode(
        self,
        program: RecordedEpisodeLookupContext,
    ) -> EpisodeLookupResult:
        """固定 preset の Web tool trace を検証して話数検索を実行する。"""

        from app.metadata.ai.acp_client import run_acp_episode_lookup

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


async def resolve_series_metadata(
    program: RecordedSeriesProgramPrompt,
    hints: SeriesMetadataHints,
    *,
    settings: RecordedSeriesSettings | None = None,
    api_key: str | None = None,
) -> AISeriesMetadataResult:
    """バックエンド非依存でシリーズ名・話数・話名を一括生成する。

    Args:
        program: 録画番組メタデータ。
        hints: サーバーが固定したローカル・既存 Series・Wikipedia の参考情報。
        settings: 判定開始時の設定 snapshot。未指定時はここで取得する。
        api_key: 同じ時点の API キー snapshot。settings 指定時に併用する。

    Returns:
        最小 schema と hints 内 ID 制約を検証済みの生成結果。

    Raises:
        RecordedSeriesAIError: ACP セットアップまたは AI 呼び出しの失敗。
    """

    if settings is None:
        settings, api_key = RecordedSeriesSettingsStore.getSettingsAndAPIKey()

    async def RunGeneration() -> AISeriesMetadataResult:
        backend = _create_backend(settings, api_key=api_key)
        return await backend.resolveSeriesMetadata(program, hints)

    if settings.ai_backend == 'OpenCode':
        # Phase 2 で OpenCodeBackend を直接呼ぶ。
        result = await RunGeneration()
    else:
        result = await _RunACPOperationWithDeadline(
            RunGeneration,
            _GetACPCredentialProvider(settings.ai_backend),
        )
    return replace(result, model=get_audit_model(settings))


async def lookup_episode(
    program: RecordedEpisodeLookupContext,
    *,
    settings: RecordedSeriesSettings | None = None,
    api_key: str | None = None,
    expected_provider_fingerprint: str | None = None,
) -> EpisodeLookupResult:
    """バックエンド非依存の話数検索。

    Args:
        program: 録画番組メタデータ。
        settings: 判定開始時の設定 snapshot。未指定時はここで取得する。
        api_key: 同じ時点の API キー snapshot。settings 指定時に併用する。
        expected_provider_fingerprint: Automation が永続化する provider。

    Returns:
        EpisodeLookupResult: 検証済みの話数検索結果。

    Raises:
        RecordedSeriesAIError: ACP セットアップまたは AI 呼び出しの失敗。
    """
    if settings is None:
        settings, api_key = RecordedSeriesSettingsStore.getSettingsAndAPIKey()

    async def RunLookup() -> EpisodeLookupResult:
        if expected_provider_fingerprint is not None and (
            get_episode_lookup_provider_fingerprint(settings, api_key)
            != expected_provider_fingerprint
        ):
            raise RecordedSeriesAIError('AISettingsChangedBeforeRequest')
        backend = _create_backend(settings, api_key=api_key)
        return await backend.lookupEpisode(program)

    if settings.ai_backend == 'OpenCode':
        # Phase 2 で OpenCodeBackend を直接呼ぶ。
        result = await RunLookup()
    else:
        # provider fingerprint の再照合から subprocess 終了まで credential import/delete を止め、
        # 受付時の世代と実際に CLI が読む世代を一致させる。
        result = await _RunACPOperationWithDeadline(
            RunLookup,
            _GetACPCredentialProvider(settings.ai_backend),
        )
    return replace(result, model=get_audit_model(settings))


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


def get_audit_model(settings: RecordedSeriesSettings | None = None) -> str:
    """監査用のモデル文字列（backend prefix 付き）を返す。

    ACP: "acp:codex:claude-sonnet-4-5" または "acp:codex"
    OpenCode: "opencode:{provider}/{model}" または service 未設定時 "opencode"

    Args:
        settings: 監査ラベルへ変換する設定。未指定時は保存済み設定を使用する。

    Returns:
        backend 種別とモデルを識別できる監査ラベル。
    """

    effective_settings = settings or RecordedSeriesSettingsStore.getSettings()
    if effective_settings.ai_backend == 'OpenCode':
        service_id = effective_settings.ai_backend_service_id
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
    prefix = backend_prefix_map.get(effective_settings.ai_backend, 'acp')
    # モデル・推論深さは ACPSettings が正本（AI バックエンドページ管理）。
    from app.metadata.ai.ACPSettings import ACPSettingsStore

    acp_settings = ACPSettingsStore.getSettings().forBackend(effective_settings.ai_backend)
    acp_model = acp_settings.model
    if acp_model:
        label = f'{prefix}:{acp_model}'
    else:
        label = prefix
    # モデル名と分離保存した推論深さを、Codex 互換の [effort] 表記で監査へ載せる。
    reasoning_effort = acp_settings.reasoning_effort
    if reasoning_effort is not None:
        return f'{label}[{reasoning_effort.lower()}]'
    return label
