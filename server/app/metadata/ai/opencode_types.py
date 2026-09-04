"""OpenCode 1.18.27 CLI telemetry と runtime config の型定義。

usage の正本位置:
- CLI NDJSON の StepFinishPart.tokens / cost

auth.json は provider ID をキーに ApiAuth / OAuth などの entry を保持する。
"""

from __future__ import annotations

from typing import Annotated, Literal, NotRequired

from typing_extensions import TypedDict


class OpenCodeApiAuth(TypedDict):
    """auth.json に保存する API キー認証。"""

    type: Literal['api']
    key: str
    metadata: NotRequired[dict[str, str]]


class OpenCodeTokenUsage(TypedDict):
    """CLI step-finish part の tokens オブジェクト。"""

    input: float
    output: float
    reasoning: float
    cache: dict[str, float]
    total: NotRequired[float]


class KonomiTVBS4KOpenCodeProviderOptions(TypedDict):
    """KonomiTV-BS4K 管理カスタム provider の接続オプション。"""

    baseURL: str


class KonomiTVBS4KOpenCodeProviderModel(TypedDict):
    """カスタム provider が OpenCode へ公開する1モデル。"""

    name: str


class KonomiTVBS4KOpenCodeProviderConfig(TypedDict):
    """opencode.json の provider 1件分。"""

    npm: Literal['@ai-sdk/openai-compatible', '@ai-sdk/anthropic']
    name: str
    options: KonomiTVBS4KOpenCodeProviderOptions
    models: dict[str, KonomiTVBS4KOpenCodeProviderModel]


class OpenCodeNormalizedUsage(TypedDict):
    """KonomiTV 側へ正規化した利用量（月次台帳 Phase 3 向け）。"""

    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    total_tokens: int
    estimated_cost_usd: float | None


def NormalizeOpenCodeUsage(
    tokens: OpenCodeTokenUsage | None,
    cost: float | None,
) -> OpenCodeNormalizedUsage:
    """OpenCode CLI part の tokens/cost を月次台帳向けに正規化する。

    Args:
        tokens: step-finish part の tokens。
        cost: step-finish part の cost。取得不能時は None。

    Returns:
        整数化した token 数と推定 cost。cost が負や非数の場合は None。
    """

    prompt_tokens = 0
    completion_tokens = 0
    reasoning_tokens = 0
    if tokens is not None:
        prompt_tokens = max(0, int(tokens.get('input') or 0))
        completion_tokens = max(0, int(tokens.get('output') or 0))
        reasoning_tokens = max(0, int(tokens.get('reasoning') or 0))
    total_tokens = prompt_tokens + completion_tokens + reasoning_tokens
    estimated_cost: float | None = None
    if cost is not None:
        try:
            cost_value = float(cost)
        except (TypeError, ValueError):
            cost_value = float('nan')
        if cost_value == cost_value and cost_value >= 0:  # NaN 拒否
            estimated_cost = cost_value
    return OpenCodeNormalizedUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        estimated_cost_usd=estimated_cost,
    )


# 製品 agent 名（docker/opencode/opencode.json と一致）
OPENCODE_GENERATE_AGENT: Annotated[str, 'candidate/metadata generation'] = 'recorded-series-generate'
OPENCODE_EPISODE_AGENT: Annotated[str, 'episode lookup with web tools'] = 'recorded-series-episode'
