"""主系 AI 失敗時の回復方針（最大 2 試行）を定義する。

シリーズ情報生成と話数 Web 検索で同じ判定・試行計画を共有する。
OpenCode の format 補修（Auto JSON 補正 / format.retryCount）は backend 内に残し、
ここでの 2 試行目は独立した通常実行または修正版プロンプト再実行とする。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.metadata.ai.episode_lookup import EpisodeLookupResult
from app.metadata.RecordedSeriesCandidates import RecordedSeriesAIError
from app.metadata.RecordedSeriesGeneration import AISeriesMetadataResult
from app.metadata.RecordedSeriesSettings import (
    AIBackendKind,
    AIFailureRecoveryStrategy,
    RecordedSeriesSettings,
)


# プロンプト種別。RecoveryRetry は同一 backend の 2 回目だけに使う。
AIPromptVariant = Literal['Default', 'RecoveryRetry']
# 監査用の試行役割。
AIRecoveryRole = Literal['Primary', 'PrimaryRetry', 'Fallback']


@dataclass(frozen=True, slots=True)
class AIBackendTarget:
    """1 回の AI 実行に使う backend 解決結果。"""

    # 実行する backend 種別。
    backend_kind: AIBackendKind
    # OpenCode のときだけ非 None になる service UUID。
    service_id: str | None
    # 監査・ログ用の試行役割。
    role: AIRecoveryRole
    # この試行で使うプロンプト種別。
    prompt_variant: AIPromptVariant


@dataclass(frozen=True, slots=True)
class AIRecoveryAttemptSummary:
    """1 試行分の監査サマリ（秘密を含まない）。"""

    # 1-based の試行番号（最大 2）。
    attempt_number: int
    # Primary / PrimaryRetry / Fallback。
    role: AIRecoveryRole
    # 実行した backend 種別。
    backend_kind: str
    # OpenCode service_id。ACP では None。
    service_id: str | None
    # 監査 model ラベル。
    model: str
    # 成功時の decision / outcome、失敗時の error code。
    result_code: str
    # 試行が技術的に成功したか（decision=Unresolved も True）。
    succeeded: bool
    # 最終採用された試行か。
    adopted: bool
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int | None
    http_status: int | None
    error_code: str | None


def BuildPrimaryTarget(settings: RecordedSeriesSettings) -> AIBackendTarget:
    """設定から主系（1 回目）の実行ターゲットを構築する。

    Args:
        settings: 判定開始時点の設定 snapshot。

    Returns:
        通常プロンプトで実行する主系ターゲット。
    """

    return AIBackendTarget(
        backend_kind=settings.ai_backend,
        service_id=(
            settings.ai_backend_service_id
            if settings.ai_backend == 'OpenCode'
            else None
        ),
        role='Primary',
        prompt_variant='Default',
    )


def BuildRecoveryTarget(settings: RecordedSeriesSettings) -> AIBackendTarget | None:
    """失敗時ポリシーに基づく 2 回目ターゲットを返す。

    Args:
        settings: 判定開始時点の設定 snapshot。

    Returns:
        2 回目を実行する場合のターゲット。Fail または設定不正時は None。
    """

    strategy: AIFailureRecoveryStrategy = settings.ai_failure_recovery_strategy
    if strategy == 'Fail':
        return None
    if strategy == 'RetrySameBackend':
        return AIBackendTarget(
            backend_kind=settings.ai_backend,
            service_id=(
                settings.ai_backend_service_id
                if settings.ai_backend == 'OpenCode'
                else None
            ),
            role='PrimaryRetry',
            # 2 回目だけ schema 確認・別検索戦略を求める。
            prompt_variant='RecoveryRetry',
        )
    # FallbackBackend: 予備 AI を通常プロンプトで独立実行する。
    # 主系の回答・例外・失敗理由は渡さない。
    fallback_backend = settings.ai_fallback_backend
    if fallback_backend is None:
        return None
    return AIBackendTarget(
        backend_kind=fallback_backend,
        service_id=(
            settings.ai_fallback_backend_service_id
            if fallback_backend == 'OpenCode'
            else None
        ),
        role='Fallback',
        prompt_variant='Default',
    )


def ShouldRecoverSeriesMetadataResult(result: AISeriesMetadataResult) -> bool:
    """シリーズ生成結果が失敗時ポリシー適用対象かを返す。

    Args:
        result: 主系の検証済み結果。

    Returns:
        Unresolved のとき True。NotSeries / Series は正常判定として False。
    """

    return result.decision == 'Unresolved'


def ShouldRecoverSeriesMetadataError(error: RecordedSeriesAIError) -> bool:
    """シリーズ生成の例外が失敗時ポリシー適用対象かを返す。

    Args:
        error: 主系実行中の例外。

    Returns:
        技術的失敗として回復を試す場合は True。
        入力変更など回復不能なコードは False。
    """

    # 入力世代のずれは再実行しても意味がない。
    if error.code in {
        'InputChangedBeforeRequest',
        'InputChangedBeforeApply',
        'AISettingsChangedBeforeRequest',
        'Cancelled',
    }:
        return False
    return True


def ShouldRecoverEpisodeLookupResult(result: EpisodeLookupResult) -> bool:
    """話数検索結果が失敗時ポリシー適用対象かを返す。

    Args:
        result: 主系の検証済み結果。

    Returns:
        InsufficientEvidence または技術失敗 outcome のとき True。
        Resolved / NotNumbered / NoPublishedNumber は False。
    """

    if result.outcome in {'Resolved', 'NotNumbered', 'NoPublishedNumber'}:
        return False
    if result.outcome == 'Cancelled':
        return False
    # InsufficientEvidence は再判定対象。その他の失敗 outcome は技術的失敗。
    return True


def ShouldRecoverEpisodeLookupError(error: RecordedSeriesAIError) -> bool:
    """話数検索の例外が失敗時ポリシー適用対象かを返す。

    Args:
        error: 主系実行中の例外。

    Returns:
        回復を試す場合は True。
    """

    if error.code in {
        'InputChangedBeforeRequest',
        'InputChangedAfterRequest',
        'AISettingsChangedBeforeRequest',
        'Cancelled',
    }:
        return False
    return True


def FormatRecoveryAttemptSummary(summary: AIRecoveryAttemptSummary) -> str:
    """監査・ログ向けの1行サマリを返す。

    Args:
        summary: 試行サマリ。

    Returns:
        秘密を含まない固定形式文字列。
    """

    adopted = 'adopted' if summary.adopted else 'not-adopted'
    service = summary.service_id or '-'
    prompt_tokens = summary.prompt_tokens if summary.prompt_tokens is not None else '-'
    completion_tokens = (
        summary.completion_tokens
        if summary.completion_tokens is not None
        else '-'
    )
    latency_ms = summary.latency_ms if summary.latency_ms is not None else '-'
    http_status = summary.http_status if summary.http_status is not None else '-'
    error_code = summary.error_code or '-'
    return (
        f'{summary.attempt_number}:{summary.role}:{summary.backend_kind}:'
        f'service={service}:model={summary.model}:result={summary.result_code}:'
        f'{adopted}:prompt_tokens={prompt_tokens}:'
        f'completion_tokens={completion_tokens}:latency_ms={latency_ms}:'
        f'http_status={http_status}:error={error_code}'
    )


def SeriesResultCode(result: AISeriesMetadataResult) -> str:
    """シリーズ結果の監査コードを返す。"""

    return result.decision


def EpisodeResultCode(result: EpisodeLookupResult) -> str:
    """話数検索結果の監査コードを返す。"""

    if result.error_code:
        return result.error_code
    return result.outcome
