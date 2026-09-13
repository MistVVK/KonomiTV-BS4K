from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing_extensions import TypedDict

from app.constants import API_REQUEST_HEADERS
from app.metadata.SeriesTitleParser import NormalizeTitleReading


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
    # 候補選択と同時に取得するシリーズタイトルの読み。旧応答の互換のため省略可。
    title_reading: Annotated[str | None, Field(max_length=255)] = None
    # TMDb 照合時の Season 判定。季番号・'whole' (作品全体)・'unresolved' (判定不能) のいずれか。
    ## Season 選択を求めない照合 (Bangumi 等) では null のまま返り、サーバー側は null を
    ## 判定不能 (unresolved) と同じ従来動作へ倒す。旧応答の互換のため省略可。
    season: Annotated[int | Literal['whole', 'unresolved'] | None, Field()] = None

    @field_validator('title_reading')
    @classmethod
    def validateTitleReading(cls, value: str | None) -> str | None:
        """読みはソート用の正規化済みひらがなへ整え、空は null と同じにする。"""

        return NormalizeTitleReading(value)


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
    # 候補選択の返答と同時に取得したシリーズタイトルの読み。旧応答では None。
    title_reading: str | None = None
    # TMDb 照合時の Season 判定 (季番号 / 'whole' / 'unresolved')。それ以外の照合では None。
    season: int | str | None = None


class RecordedSeriesAIError(Exception):
    """秘密情報や生レスポンスを保持しないAI APIエラー。"""

    def __init__(
        self,
        code: str,
        *,
        http_status: int | None = None,
        latency_ms: int | None = None,
        recovery_attempt_summaries: tuple[str, ...] = (),
        provider_error_excerpt: str | None = None,
    ) -> None:
        """監査ログへ保存可能な安全な情報だけで例外を初期化する。

        Args:
            code: 呼び出し元が分岐・表示に使う固定エラーコード。
            http_status: APIが応答した場合のHTTPステータス。
            latency_ms: エラー確定までの経過時間。
            recovery_attempt_summaries: 失敗時ポリシーによる試行サマリ（秘密なし）。
            provider_error_excerpt: 非2xx時に provider が返した本文の sanitizer 済み抜粋
                （秘密なし・長さ上限済み）。raise 元の backend だけが設定する。
        """

        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.latency_ms = latency_ms
        # 主系・予備系の試行列。単一試行失敗時は空または1件。
        self.recovery_attempt_summaries = recovery_attempt_summaries
        # 生レスポンスではなく sanitizer 済みの抜粋だけを保持する。
        self.provider_error_excerpt = provider_error_excerpt


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


class SeriesEPGContext(TypedDict):
    """外部メタデータ照合へ渡す、所属録画の EPG 証拠。

    Series レコード自体は放送日・チャンネルを持たないため、所属録画
    (RecordedProgram) の EPG 列から代表値と最古放送日を組み立てる。
    """

    # 重複を除いた所属録画の EPG 番組名。放送枠名を含むため API クエリには直送しない。
    epg_titles: list[str]
    # 番組概要が最も長い代表録画の EPG 概要。Series.description とは別の一次情報。
    description: str
    # 代表録画の EPG 詳細 (見出し→本文)。照合 AI の入力へ渡すための上限付き項目。
    detail_items: list[RecordedSeriesProgramDetailItem]
    # 代表録画の放送開始時刻 (ISO 8601)。代表録画が時刻を持たない場合は空文字。
    broadcast_datetime: str
    # 所属録画全体で最も古い放送開始日 (YYYY-MM-DD)。日付の矛盾判断はハードゲートではなく
    ## AI へ渡す証拠とするため、BuildEPGEvidenceText 経由でプロンプトへ載せる。
    min_broadcast_date: str
    # 代表録画のチャンネル名。チャンネル未設定の録画のみの場合は None。
    channel_name: str | None


def BuildSeriesEPGContext(member_programs: list[dict[str, Any]]) -> SeriesEPGContext | None:
    """所属録画の行から、外部メタデータ照合へ渡す EPG 証拠を組み立てる。

    Args:
        member_programs (list[dict[str, Any]]): RecordedProgram.values() の行。
            title / description / start_time / channel__name を含むこと。

    Returns:
        SeriesEPGContext | None: 所属録画が無い、または題名が全て空の場合は None。
    """

    rows = [program for program in member_programs if str(program.get('title') or '').strip() != '']
    if len(rows) == 0:
        return None
    # 番組概要が最も長い録画を代表にする。番組内容を語れる録画のほうが、
    ## 外部候補との突合と AI 判定の両方で有用な証拠になる。
    representative = max(rows, key=lambda program: len(str(program.get('description') or '').strip()))
    epg_titles: list[str] = []
    for program in rows:
        title = str(program.get('title') or '').strip()
        if title != '' and title not in epg_titles:
            epg_titles.append(title)
    start_time = representative.get('start_time')
    start_times = [program['start_time'] for program in rows if isinstance(program.get('start_time'), datetime)]
    # EPG 詳細は RecordedSeriesResolver の番組詳細と同じ上限 (8 項目 / name 120 / value 800)
    ## で切り詰め、照合 AI の入力証拠として残す。API クエリには使わない。
    representative_detail = representative.get('detail')
    detail_items: list[RecordedSeriesProgramDetailItem] = []
    if isinstance(representative_detail, dict):
        detail_items = [
            RecordedSeriesProgramDetailItem(
                name=str(name)[:120],
                value=str(value)[:800],
            )
            for name, value in sorted(representative_detail.items())[:8]
            if str(value).strip() != ''
        ]
    return SeriesEPGContext(
        epg_titles=epg_titles[:8],
        description=str(representative.get('description') or '').strip()[:600],
        detail_items=detail_items,
        broadcast_datetime=start_time.isoformat() if isinstance(start_time, datetime) else '',
        min_broadcast_date=min(start_times).date().isoformat() if len(start_times) > 0 else '',
        channel_name=str(representative.get('channel__name') or '').strip() or None,
    )


def BuildEPGEvidenceText(epg_context: SeriesEPGContext | None) -> str:
    """EPG 証拠を AI へ渡す入力文の末尾に追加するブロックへ変換する。

    Args:
        epg_context (SeriesEPGContext | None): 所属録画の EPG 証拠。無い場合は None。

    Returns:
        str: 追加するブロック。証拠が空の場合は空文字。
    """

    if epg_context is None:
        return ''
    lines: list[str] = []
    if len(epg_context['epg_titles']) > 0:
        lines.append('Program names: ' + ' / '.join(epg_context['epg_titles'][:4]))
    if epg_context['description'] != '':
        lines.append(f"Program description: {epg_context['description']}")
    for detail_item in epg_context['detail_items']:
        lines.append(f"Detail {detail_item['name']}: {detail_item['value']}")
    if epg_context['broadcast_datetime'] != '':
        lines.append(f"Recorded broadcast datetime: {epg_context['broadcast_datetime']}")
    if epg_context['min_broadcast_date'] != '':
        lines.append(f"Oldest recorded broadcast date: {epg_context['min_broadcast_date']}")
    if epg_context['channel_name'] is not None:
        lines.append(f"Recorded channel: {epg_context['channel_name']}")
    if len(lines) == 0:
        return ''
    return '\n\nRecorded program EPG evidence:\n' + '\n'.join(lines)
