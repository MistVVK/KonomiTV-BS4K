from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict

from app.constants import API_REQUEST_HEADERS


MEDIAWIKI_API_URL = 'https://ja.wikipedia.org/w/api.php'


class WikipediaCandidate(TypedDict):
    """MediaWiki APIから取得し、候補IDと本文を固定したWikipedia記事候補。"""

    page_id: int
    title: str
    extract: str


class SeriesChoiceCandidate(TypedDict):
    """AIへ選択肢として渡す、サーバー側で生成済みの候補。"""

    choice_id: str
    kind: Literal['Local', 'ExistingSeries', 'Wikipedia', 'Standalone', 'Unresolved']
    title: str
    description: str


class RecordedSeriesProgramPrompt(TypedDict):
    """AIへ渡す録画番組メタデータの公開型。"""

    title: str
    description: str
    detail_items: list[RecordedSeriesProgramDetailItem]
    genres: list[str]
    channel_id: str | None
    channel_name: str | None
    broadcast_datetime: str
    duration_seconds: float


class RecordedSeriesProgramDetailItem(TypedDict):
    """入力上限を適用した番組詳細の1項目。"""

    name: str
    value: str


class AIChoiceOutput(BaseModel):
    """候補選択 AI から受理する最小出力（OpenCode / ACP 共通）。"""

    model_config = ConfigDict(extra='forbid')

    choice_id: Annotated[str, Field(min_length=1, max_length=96)]
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]


# 旧 private 名の互換 alias（OpenCode / ACP 実装が参照）。
_AIChoiceOutput = AIChoiceOutput


@dataclass(frozen=True, slots=True)
class AIChoiceResult:
    """検証済みAI選択とAPI利用量を保持する。"""

    choice_id: str
    confidence: float
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    http_status: int
    latency_ms: int


class RecordedSeriesAIError(Exception):
    """秘密情報や生レスポンスを保持しないAI APIエラー。"""

    def __init__(
        self,
        code: str,
        *,
        http_status: int | None = None,
        latency_ms: int | None = None,
    ) -> None:
        """監査ログへ保存可能な安全な情報だけで例外を初期化する。

        Args:
            code: 呼び出し元が分岐・表示に使う固定エラーコード。
            http_status: APIが応答した場合のHTTPステータス。
            latency_ms: エラー確定までの経過時間。
        """

        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.latency_ms = latency_ms


async def SearchWikipediaCandidates(query: str, limit: int = 5) -> list[WikipediaCandidate]:
    """日本語WikipediaをMediaWiki APIで検索し、固定ID付き候補を取得する。

    Args:
        query: ローカル判定で抽出済みのシリーズ名候補。
        limit: 取得する候補数。過剰な本文をAIへ渡さないよう最大8件に制限する。

    Returns:
        page ID、記事名、冒頭本文だけを含む候補リスト。

    Raises:
        httpx.HTTPError: MediaWiki APIへの接続または応答に失敗した場合。
    """

    safe_limit = max(1, min(limit, 8))
    params = {
        'action': 'query',
        'generator': 'search',
        'gsrsearch': query,
        'gsrnamespace': 0,
        'gsrlimit': safe_limit,
        'prop': 'extracts',
        'exintro': 1,
        'explaintext': 1,
        'redirects': 1,
        'format': 'json',
        'formatversion': 2,
        'utf8': 1,
    }
    async with httpx.AsyncClient(
        headers=API_REQUEST_HEADERS,
        timeout=httpx.Timeout(10.0),
        follow_redirects=True,
    ) as client:
        response = await client.get(MEDIAWIKI_API_URL, params=params)
        response.raise_for_status()
        payload = response.json()

    if isinstance(payload, dict) is False:
        return []
    query_payload = payload.get('query')
    if isinstance(query_payload, dict) is False:
        return []
    pages = query_payload.get('pages')
    if isinstance(pages, list) is False:
        return []

    typed_pages = [cast(dict[str, object], item) for item in pages if isinstance(item, dict)]
    typed_pages.sort(
        key=lambda item: cast(int, item['index']) if isinstance(item.get('index'), int) else 2**31 - 1,
    )
    candidates: list[WikipediaCandidate] = []
    for page in typed_pages:
        page_id = page.get('pageid')
        title = page.get('title')
        extract = page.get('extract', '')
        if not isinstance(page_id, int) or not isinstance(title, str):
            continue
        if not isinstance(extract, str):
            extract = ''
        candidates.append(WikipediaCandidate(
            page_id=page_id,
            title=title.strip(),
            extract=' '.join(extract.split())[:600],
        ))
    return candidates
