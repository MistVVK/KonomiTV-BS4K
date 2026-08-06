"""OpenCode 1.18.13 OpenAPI 契約の型定義土台（Phase 2 クライアントが利用）。

usage の正本位置:
- AssistantMessage.tokens.input / output / reasoning / cache
- AssistantMessage.cost
- StepFinishPart.tokens / cost（ステップ単位の中間値）

auth:
- PUT /auth/{providerID} body: ApiAuth | OAuth | WellKnownAuth
- DELETE /auth/{providerID}
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, NotRequired

from typing_extensions import TypedDict


class OpenCodeHealth(TypedDict):
    """GET /global/health の応答。"""

    healthy: Literal[True]
    version: str


class OpenCodeApiAuth(TypedDict):
    """PUT /auth/{providerID} の API キー認証。"""

    type: Literal['api']
    key: str
    metadata: NotRequired[dict[str, str]]


class OpenCodeOAuthAuth(TypedDict):
    """PUT /auth/{providerID} の OAuth 認証（通常は serve 側が保持）。"""

    type: Literal['oauth']
    refresh: str
    access: str
    expires: int
    accountId: NotRequired[str]
    enterpriseUrl: NotRequired[str]


class OpenCodeTokenUsage(TypedDict):
    """AssistantMessage / StepFinishPart の tokens オブジェクト。"""

    input: float
    output: float
    reasoning: float
    cache: dict[str, float]
    total: NotRequired[float]


class OpenCodeModelRef(TypedDict):
    """prompt リクエストの model 指定。"""

    providerID: str
    modelID: str


class OpenCodeJsonSchemaFormat(TypedDict):
    """format: json_schema 指定。"""

    type: Literal['json_schema']
    schema: dict[str, Any]
    retryCount: NotRequired[int]


class OpenCodeTextPartInput(TypedDict):
    """prompt parts の text 要素。"""

    type: Literal['text']
    text: str


class OpenCodePromptRequest(TypedDict):
    """POST /session/{id}/message の主要フィールド。"""

    parts: list[OpenCodeTextPartInput]
    model: NotRequired[OpenCodeModelRef]
    agent: NotRequired[str]
    format: NotRequired[OpenCodeJsonSchemaFormat]
    system: NotRequired[str]
    noReply: NotRequired[bool]


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
    """OpenCode 応答の tokens/cost を月次台帳向けに正規化する。

    Args:
        tokens: AssistantMessage.tokens または StepFinishPart.tokens。
        cost: AssistantMessage.cost または StepFinishPart.cost。取得不能時は None。

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
