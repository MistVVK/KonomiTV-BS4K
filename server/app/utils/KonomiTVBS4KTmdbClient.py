"""TMDb v3 API からシリーズメタデータを取得し、Series へ補完するクライアント。

責務は3つ。
1. TMDb v3 の検索・作品詳細・シーズン・別名タイトル取得（API キーは query string で送る）。
2. 保存済み API キーで TMDb へ実通信する接続試験。
3. 未照合の Series を一括で TMDb 作品へ照合し、`tmdb_*` カラムと TMDb 由来の話数構造を補完する。

`description` / `genres` は Wikipedia AI 生成が正本のため上書きしない。TMDb の値は必ず
`tmdb_` 接頭辞のカラムへだけ保存する。話数構造は Bangumi 由来の SeriesEpisode がある
Series では組み立てない（ユーザーコレクションを正とする）。

API キーは TMDb v3 の仕様上 URL の query string でしか送れない（同じキーを Bearer へ
入れると 401 になる）。そのためこのモジュールは例外メッセージや URL をログへ出さず、
失敗は例外クラス名と HTTP ステータスだけを残す。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

import httpx
from tortoise.exceptions import IntegrityError
from tortoise.expressions import F, Q
from tortoise.functions import Coalesce
from tortoise.transactions import in_transaction
from typing_extensions import TypedDict

from app import logging
from app.constants import JST, TMDB_HTTPX_CLIENT
from app.metadata.KonomiTVBS4KSeriesImage import KonomiTVBS4KSeriesImage
from app.metadata.RecordedSeriesCandidates import (
    BuildSeriesEPGContext,
)
from app.metadata.RecordedSeriesSettings import IsTmdbExternalMetadataEnabled
from app.metadata.SeriesIndexer import (
    PROGRAM_MARK_PATTERN,
    PROGRAM_SLOT_MARK_PATTERN,
    NormalizeSeriesTitle,
)
from app.metadata.SeriesTitleParser import (
    ExtractEPGSearchQueries,
    ExtractMovieWorkTitle,
)
from app.models.RecordedEpisode import RecordedEpisodeResolution, SeriesEpisode
from app.models.RecordedProgram import RecordedProgram
from app.models.Series import Series, TmdbMediaType
from app.utils.KonomiTVBS4KTmdbStore import KonomiTVBS4KTmdbStore


# 放映バリアントの接尾辞。コメンタリー版・完全版・ディレクターズカットは本編と同じ作品の
## 再編集版で、TMDb には本編だけが登録されるため、検索クエリからだけ外す
## (DB 保存タイトルと AI へ渡す原文は変えない)。
_BROADCAST_VARIANT_SUFFIX_PATTERN = re.compile(
    r'\s*(?:コメンタリー版|完全版|ディレクターズカット(?:版)?)$',
)
# 作品名の前に並ぶ `【放送枠】【属性】` 形式の装飾。TMDb の主検索が 0 件だった場合だけ
# fallback 検索語から外し、文中・末尾の括弧は作品名の一部として維持する。
_LEADING_BROADCAST_DESCRIPTOR_PATTERN = re.compile(r'^(?:【[^】]+】)+')
# 末尾の話数表記 (#07) と括弧年号 ((2024) / （2024）)。本編作品の検索を妨げる装飾だけを外す。
_TRAILING_EPISODE_MARK_PATTERN = re.compile(r'\s*#\d+(?:\.\d+)?$')
_TRAILING_YEAR_PATTERN = re.compile(r'\s*[\(（]\d{4}[\)）]$')


class TmdbSearchCandidate(TypedDict):
    """解決 hints へ載せる TMDb 検索結果1件。別名タイトルは永続化しない。"""

    media_type: TmdbMediaType
    tmdb_id: int
    name: str
    original_name: str
    overview: str
    # TV は初回放送日、映画は公開日。不明なときは空文字。
    first_air_date: str
    genre_names: list[str]
    alternative_titles: list[str]


class TmdbSeasonInfo(TypedDict):
    """TMDb 作品詳細に含まれるシーズン1件。Season 判定と enrich のスコープに使う。"""

    season_number: int
    # シーズン初回放送日 (YYYY-MM-DD)。不明なときは空文字。
    air_date: str
    episode_count: int


class TmdbSeriesDetails(TypedDict):
    """Series への enrich に使う TMDb 作品詳細。"""

    media_type: TmdbMediaType
    tmdb_id: int
    name: str
    overview: str
    poster_path: str | None
    backdrop_url: str | None
    # TV のレギュラーシーズンと特別編の season_number。映画では空リスト。
    season_numbers: list[int]
    # TV のシーズン構成 (air_date・話数付き)。映画では空リスト。
    seasons: list[TmdbSeasonInfo]
    # TV は初回放送日、映画は公開日 (YYYY-MM-DD)。不明なときは空文字。
    first_air_date: str
    # 人気度と評価。不明なときは None。
    popularity: float | None
    vote_average: float | None


class TmdbMatchDecision(TypedDict):
    """AI 照合で確定した TMDb 作品と、その Series が参照する Season。"""

    candidate: TmdbSearchCandidate
    # 確定した Season 番号。None は作品全体バインド (Season 判定不能・whole・movie)。
    season_number: int | None


class TmdbSeasonEpisode(TypedDict):
    """話数照合に使う TMDb シーズンエピソード1件。"""

    episode_number: int
    tmdb_episode_id: int
    name: str
    # YYYY-MM-DD。不明なときは空文字。
    air_date: str
    # 本編尺（分）。不明なときは None。
    runtime: int | None


@dataclass(frozen=True, slots=True)
class TmdbConnectionTestResult:
    """TMDb 接続試験の結果。API キー本体を含めない。"""

    success: bool
    latency_ms: int
    message: str
    http_status: int | None
    error_code: str | None


class TmdbMetadataSyncError(RuntimeError):
    """pipelineへ秘密を含めずTMDb同期失敗を通知する。"""


class KonomiTVBS4KTmdbClient:
    """KonomiTV の録画シリーズを TMDb の作品とシーズンへ照合する。"""

    API_BASE_URL = 'https://api.themoviedb.org/3'
    IMAGE_BASE_URL = 'https://image.tmdb.org/t/p'
    # ポスターは一覧表示向けの中解像度、バックドロップは元解像度を使う。
    POSTER_SIZE = 'w500'
    BACKDROP_SIZE = 'original'
    # 日本語を主にし、名前・概要が欠ける作品だけ英語で補完する。
    PRIMARY_LANGUAGE = 'ja-JP'
    FALLBACK_LANGUAGE = 'en-US'
    # 検索は TV・映画それぞれ上位5件まで。hints へ載せるのは TV と映画を交互に6件。
    SEARCH_LIMIT = 5
    HINT_LIMIT = 6
    # 主クエリが 0 件のときに試す正規化フォールバッククエリの上限。
    ## TMDb への再検索は 0-hit 時だけに限定し、無限リトライを防ぐ。
    NORMALIZED_FALLBACK_QUERY_LIMIT = 4
    # 1作品あたりの別名タイトル上限。プロンプトを肥大させないため検索結果の一部だけ使う。
    ALTERNATIVE_TITLE_LIMIT = 8
    # 接続試験に使う、認証だけを確認できる最も軽いエンドポイント。
    CONNECTION_TEST_PATH = '/configuration'
    # scheduleSeriesSync() が起動した未完了タスク。完了時に discard する。
    _sync_tasks: set[asyncio.Task[Any]] = set()
    # pipeline が await する task は同じ所有集合へ加え、通常 scheduler からは再予約しない。
    _pipeline_sync_tasks: set[asyncio.Task[Any]] = set()
    # 実行中に再予約されたら、完了後にもう一度だけ照合する。
    _sync_rerun: bool = False
    # 起動時の直接呼び出しと設定更新後の予約を直列化し、同じ候補へ AI を重複実行しない。
    _sync_lock = asyncio.Lock()


    @classmethod
    async def testConnection(cls) -> TmdbConnectionTestResult:
        """
        保存済み API キーで TMDb へ実通信し、成否を返す。

        Returns:
            TmdbConnectionTestResult: 秘密を含まない接続試験結果。
        """

        api_key = KonomiTVBS4KTmdbStore.getAPIKey()
        # キー未設定は TMDb 経路だけを止める状態なので、接続試験では警告として返す。
        if api_key is None:
            return TmdbConnectionTestResult(
                success=False,
                latency_ms=0,
                message='TMDb API キーが設定されていません。',
                http_status=None,
                error_code='APIKeyNotConfigured',
            )
        started_at = time.monotonic()
        try:
            payload = await cls._getJSON(cls.CONNECTION_TEST_PATH, api_key, {})
        except httpx.TimeoutException:
            return TmdbConnectionTestResult(
                success=False,
                latency_ms=cls._elapsedMilliseconds(started_at),
                message='TMDb への接続がタイムアウトしました。',
                http_status=None,
                error_code='Timeout',
            )
        except httpx.HTTPStatusError as ex:
            return cls._httpErrorResult(ex.response.status_code, started_at)
        except httpx.HTTPError as ex:
            return cls._networkErrorResult(type(ex).__name__, started_at)
        except ValueError:
            # 応答が JSON object でない場合は TMDb 側の仕様変更として警告する。
            logging.warning('[KonomiTVBS4KTmdbClient][testConnection] TMDb API response is invalid.')
            return TmdbConnectionTestResult(
                success=False,
                latency_ms=cls._elapsedMilliseconds(started_at),
                message='TMDb API の応答を読み取れませんでした。',
                http_status=None,
                error_code='InvalidResponse',
            )
        latency_ms = cls._elapsedMilliseconds(started_at)
        # _getJSON() は 404 を None で返すが、/configuration に 404 は無い。
        if payload is None:
            return TmdbConnectionTestResult(
                success=False,
                latency_ms=latency_ms,
                message='TMDb API が未知のステータスを返しました。',
                http_status=404,
                error_code='NotFound',
            )
        # JSON object だけの不正応答を成功扱いせず、configuration の必須情報を確認する。
        if not isinstance(payload.get('images'), dict):
            return TmdbConnectionTestResult(
                success=False,
                latency_ms=latency_ms,
                message='TMDb API の応答を読み取れませんでした。',
                http_status=200,
                error_code='InvalidResponse',
            )
        return TmdbConnectionTestResult(
            success=True,
            latency_ms=latency_ms,
            message='TMDb API と通信できました。',
            http_status=200,
            error_code=None,
        )


    @classmethod
    async def searchCandidates(
        cls,
        series_title: str,
        api_key: str,
        auxiliary_queries: list[str] | None = None,
        *,
        raise_on_error: bool = False,
    ) -> list[TmdbSearchCandidate]:
        """
        TV と映画を検索し、AI 選択へ渡す hints を組み立てる。

        Args:
            series_title (str): ローカル Series の表示タイトル。
            api_key (str): TMDb API キー。
            auxiliary_queries (list[str] | None): 所属録画の EPG から抽出した補助クエリ。
                タイトル検索が空のときだけ、空でなくなるまで順に試す。
            raise_on_error: 主検索の通信・応答失敗を呼び出し元へ伝播するか。

        Returns:
            list[TmdbSearchCandidate]: TV と映画を交互に並べた上位候補。失敗時は空リスト。
        """

        # 放送枠「作品名」の全体では検索できない場合だけ、引用内の作品名も候補検索に使う。
        # DB の題名や AI に渡す原文は変えず、副題などの誤一致は後段の AI 選択で拒否する。
        quoted_title_match = re.search(r'「([^「」]+)」$|『([^『』]+)』$', series_title.strip())
        quoted_title = (
            (quoted_title_match.group(1) or quoted_title_match.group(2)).strip()
            if quoted_title_match is not None else ''
        )
        # 主クエリが 0 件のときの再検索語。bgm 準拠の正規化と放映バリアント除去を
        ## Series タイトルと EPG 由来の補助クエリから組み立て、重複を除いて有限件数に切る。
        fallback_queries = cls._buildNormalizedFallbackQueries(series_title, auxiliary_queries or [])
        # 一方の検索が失敗しても、取得できた種別の hints は照合へ渡す。
        results_by_type: dict[TmdbMediaType, list[dict[str, Any]]] = {}
        for media_type in ('tv', 'movie'):
            # 主・引用部・フォールバック・EPG 補助の全段階を横断して同じ検索語を一度だけ送る。
            ## fallback や引用部が別の EPG 補助クエリと一致しても再送しない (task の重複排除)。
            tried_queries = {series_title}
            try:
                results_by_type[media_type] = await cls._search(f'/search/{media_type}', series_title, api_key)
                if not results_by_type[media_type] and quoted_title and quoted_title != series_title:
                    tried_queries.add(quoted_title)
                    results_by_type[media_type] = await cls._search(f'/search/{media_type}', quoted_title, api_key)
                # 主クエリと引用部が 0 件のとき、正規化済みのフォールバッククエリを順に試す。
                ## 装飾付きタイトルでは本編作品に届かないため、検索語だけを変えて再検索する。
                ## フォールバックの1件が失敗しても、取得済みの結果は捨てない。
                for fallback_query in fallback_queries:
                    if results_by_type[media_type]:
                        break
                    if fallback_query in tried_queries:
                        continue
                    tried_queries.add(fallback_query)
                    try:
                        results_by_type[media_type] = await cls._search(f'/search/{media_type}', fallback_query, api_key)
                    except (httpx.HTTPError, ValueError) as ex:
                        logging.warning(
                            f'[KonomiTVBS4KTmdbClient] TMDb fallback search skipped. '
                            f'[media_type: {media_type}, error: {type(ex).__name__}]',
                        )
                        if raise_on_error:
                            raise
                        break
                # Series タイトルと引用内作品名の両方が空のとき、EPG 由来の補助クエリを
                ## 順に試す。EPG 全文をクエリへ足すことはせず、作品名だけを検索語にする。
                ## 補助クエリの1件が失敗しても、取得済みの結果は捨てない。
                for auxiliary_query in auxiliary_queries or []:
                    if results_by_type[media_type]:
                        break
                    if auxiliary_query in tried_queries:
                        continue
                    tried_queries.add(auxiliary_query)
                    try:
                        results_by_type[media_type] = await cls._search(f'/search/{media_type}', auxiliary_query, api_key)
                    except (httpx.HTTPError, ValueError) as ex:
                        logging.warning(
                            f'[KonomiTVBS4KTmdbClient] TMDb auxiliary search skipped. '
                            f'[media_type: {media_type}, error: {type(ex).__name__}]',
                        )
                        if raise_on_error:
                            raise
                        break
            except (httpx.HTTPError, ValueError) as ex:
                logging.warning(
                    f'[KonomiTVBS4KTmdbClient] TMDb search skipped. '
                    f'[media_type: {media_type}, error: {type(ex).__name__}]',
                )
                if raise_on_error:
                    raise
                results_by_type[media_type] = []
        tv_results = results_by_type['tv']
        movie_results = results_by_type['movie']
        if len(tv_results) == 0 and len(movie_results) == 0:
            return []
        # 映画だけの作品と TV だけの作品を取りこぼさないよう、交互に並べてから上限で切る。
        interleaved: list[tuple[TmdbMediaType, dict[str, Any]]] = []
        for index in range(max(len(tv_results), len(movie_results))):
            if index < len(tv_results):
                interleaved.append(('tv', tv_results[index]))
            if index < len(movie_results):
                interleaved.append(('movie', movie_results[index]))
        genre_names_by_id = {
            'tv': await cls._getGenreNames('/genre/tv/list', api_key),
            'movie': await cls._getGenreNames('/genre/movie/list', api_key),
        }
        candidates: list[TmdbSearchCandidate] = []
        for media_type, result in interleaved[:cls.HINT_LIMIT]:
            tmdb_id = result['tmdb_id']
            genre_ids = result.get('genre_ids')
            if not isinstance(genre_ids, list):
                genre_ids = []
            candidates.append(TmdbSearchCandidate(
                media_type=media_type,
                tmdb_id=tmdb_id,
                name=str(result.get('name') or ''),
                original_name=str(result.get('original_name') or ''),
                overview=' '.join(str(result.get('overview') or '').split())[:600],
                first_air_date=str(result.get('first_air_date') or result.get('release_date') or ''),
                genre_names=[
                    genre_names_by_id[media_type][genre_id]
                    for genre_id in genre_ids
                    if isinstance(genre_id, int) and genre_id in genre_names_by_id[media_type]
                ],
                # 別名タイトルは日本語表記と原題の揺れを AI へ示すためだけで、DB へは保存しない。
                alternative_titles=await cls._getAlternativeTitles(media_type, tmdb_id, api_key),
            ))
        return candidates


    @classmethod
    def _buildNormalizedFallbackQueries(cls, series_title: str, auxiliary_queries: list[str]) -> list[str]:
        """
        主クエリが 0 件のときに試す再検索クエリを、bgm 準拠の正規化で組み立てる。

        Args:
            series_title (str): ローカル Series の表示タイトル。
            auxiliary_queries (list[str]): 所属録画の EPG から抽出した補助クエリ。

        Returns:
            list[str]: 重複を除いた再検索クエリ。NORMALIZED_FALLBACK_QUERY_LIMIT 件まで。
        """

        queries: list[str] = []

        def Append(query: str) -> None:
            """主クエリや既出クエリと同じ検索語は再試行しない。

            Args:
                query (str): 追加候補の検索語。
            """

            if query != '' and query != series_title and query not in queries:
                queries.append(query)

        # (a) Bangumi と同じ正規化。NormalizeSeriesTitle (NFKC・空白除去・casefold) のあと
        ## 末尾句読点を外す変換で、KonomiTVBS4KBangumiClient._normalizeSubjectTitle と同じ形。
        Append(NormalizeSeriesTitle(series_title).rstrip('。.!！?？'))
        # (b) 放映バリアント接尾辞の除去。コメンタリー版などの再編集版は TMDb では本編に
        ## 登録されるため、Series タイトルと所属 EPG 由来の補助クエリの両方から本編名を引く。
        ## task の (a)→(b) の順に合成し、「作品名 完全版！」のように句読点が接尾辞より後ろへ
        ## 来るタイトルでも本編クエリへ届くよう、正規化・句読点除去済みの結果へ適用する。
        for source in [series_title, *auxiliary_queries]:
            normalized_source = NormalizeSeriesTitle(source).rstrip('。.!！?？')
            stripped = cls._stripBroadcastVariantMarkers(normalized_source)
            # 変換しても元と同じ補助クエリは既存の補助検索ループがそのまま試すため、
            ## fallback へは追加しない (同じ検索語の重複送信を防ぐ)。
            if source != series_title and stripped == source:
                continue
            Append(stripped)
        # (c) 先頭の `【…】` 群を除去。既存 fallback を優先するため (a)・(b) の全候補より
        ## 後ろに置き、主検索が 0 件のときだけ検索語を変える。放送枠が重なっていても
        ## 先頭から連続する群だけを外し、文中・末尾の引用や話数には触れない。
        for source in [series_title, *auxiliary_queries]:
            normalized_source = NormalizeSeriesTitle(source).rstrip('。.!！?？')
            variant_stripped = cls._stripBroadcastVariantMarkers(normalized_source)
            stripped = _LEADING_BROADCAST_DESCRIPTOR_PATTERN.sub('', variant_stripped).strip()
            # 先頭括弧がない未変換の補助クエリは既存の補助検索ループへ任せる。
            if source != series_title and stripped == source:
                continue
            Append(stripped)
        return queries[:cls.NORMALIZED_FALLBACK_QUERY_LIMIT]


    @staticmethod
    def _stripBroadcastVariantMarkers(title: str) -> str:
        """
        検索クエリ用に、タイトル末尾の放映バリアント装飾を外す。

        Args:
            title (str): Series タイトルまたは EPG 由来の補助クエリ。

        Returns:
            str: 番組記号・放送枠マーク・末尾の話数 #NN・括弧年号・再編集版接尾辞を
                外したタイトル。外すものが無ければ入力と同じ文字列。
        """

        stripped = title.strip()
        # 「コメンタリー版 #07[多]」のように末尾装飾が重なるため、外すと次の装飾が
        ## 末尾へ出る。変化がなくなるまで最大3回剥がし、無限ループは防ぐ。
        for _ in range(3):
            previous = stripped
            # KonomiTV が番組記号・放送枠として既知の [多][字]・<+Ultra> 系を外す。
            stripped = PROGRAM_MARK_PATTERN.sub('', stripped)
            stripped = PROGRAM_SLOT_MARK_PATTERN.sub('', stripped)
            stripped = _TRAILING_EPISODE_MARK_PATTERN.sub('', stripped)
            stripped = _TRAILING_YEAR_PATTERN.sub('', stripped)
            stripped = _BROADCAST_VARIANT_SUFFIX_PATTERN.sub('', stripped).strip()
            if stripped == previous:
                break
        return stripped


    @staticmethod
    def _parseTmdbDate(value: str) -> date | None:
        """TMDb の日付文字列 (YYYY-MM-DD) を date へ変換する。

        Args:
            value (str): 変換する日付文字列。

        Returns:
            date | None: 解析できない日付は無効値として None を返す。
        """

        try:
            return date.fromisoformat(value)
        except ValueError:
            # 外部 API の日付表記の揺れは、保存の妨げにならないよう破棄する。
            return None


    @classmethod
    async def getDetails(
        cls,
        media_type: TmdbMediaType,
        tmdb_id: int,
        api_key: str,
    ) -> TmdbSeriesDetails | None:
        """
        作品詳細を取得し、日本語が欠ける欄だけ英語で補完する。

        Args:
            media_type (TmdbMediaType): 'tv' または 'movie'。
            tmdb_id (int): TMDb 作品 ID。
            api_key (str): TMDb API キー。

        Returns:
            TmdbSeriesDetails | None: 作品が無い・取得できない場合は None。
        """

        path = cls._detailsPath(media_type, tmdb_id)
        payload = await cls._getJSON(path, api_key, {'language': cls.PRIMARY_LANGUAGE})
        if payload is None:
            return None
        if payload.get('id') != tmdb_id:
            raise ValueError('TMDb detail response is invalid.')
        name = cls._extractName(payload)
        overview = ' '.join(str(payload.get('overview') or '').split())
        # 日本語の題名・概要が未登録の作品は、英語の値で欠損だけ補完する。
        if name == '' or overview == '':
            # 英語の取得失敗は、取得済みの日本語の詳細を捨てる理由にしない。
            try:
                fallback_payload = await cls._getJSON(path, api_key, {'language': cls.FALLBACK_LANGUAGE})
            except (httpx.HTTPError, ValueError) as ex:
                logging.warning(
                    f'[KonomiTVBS4KTmdbClient] English details skipped. [error: {type(ex).__name__}]',
                )
                fallback_payload = None
            if fallback_payload is not None:
                if name == '':
                    name = cls._extractName(fallback_payload)
                if overview == '':
                    overview = ' '.join(str(fallback_payload.get('overview') or '').split())
        season_numbers: list[int] = []
        seasons: list[TmdbSeasonInfo] = []
        if media_type == 'tv':
            raw_seasons = payload.get('seasons')
            if not isinstance(raw_seasons, list):
                raise ValueError('TMDb seasons response is invalid.')
            for season in raw_seasons:
                if not isinstance(season, dict) or type(season.get('season_number')) is not int:
                    raise ValueError('TMDb season number is invalid.')
                season_numbers.append(season['season_number'])
                raw_episode_count = season.get('episode_count')
                seasons.append(TmdbSeasonInfo(
                    season_number=season['season_number'],
                    air_date=str(season.get('air_date') or ''),
                    episode_count=raw_episode_count if type(raw_episode_count) is int else 0,
                ))
        # 初放送日・人気度・評価はソート用の補完メタデータ。日本語化対象ではないため
        ## 英語フォールバック応答は参照せず、主応答の値だけを使う。
        first_air_date = str(payload.get('first_air_date') or payload.get('release_date') or '')
        popularity = payload.get('popularity')
        vote_average = payload.get('vote_average')
        return TmdbSeriesDetails(
            media_type=media_type,
            tmdb_id=tmdb_id,
            name=name,
            overview=overview,
            poster_path=cls._extractImagePath(payload.get('poster_path')),
            backdrop_url=cls._buildImageURL(payload.get('backdrop_path'), cls.BACKDROP_SIZE),
            season_numbers=season_numbers,
            seasons=seasons,
            first_air_date=first_air_date,
            popularity=(
                float(popularity)
                if isinstance(popularity, (int, float)) and not isinstance(popularity, bool)
                else None
            ),
            vote_average=(
                float(vote_average)
                if isinstance(vote_average, (int, float)) and not isinstance(vote_average, bool)
                else None
            ),
        )


    @classmethod
    async def getSeasonEpisodes(
        cls,
        tmdb_id: int,
        season_number: int,
        api_key: str,
    ) -> list[tuple[int, int]]:
        """
        TV シーズンの通常エピソード番号と TMDb エピソード ID を取得する。

        Args:
            tmdb_id (int): TMDb の TV 作品 ID。
            season_number (int): シーズン番号。
            api_key (str): TMDb API キー。

        Returns:
            list[tuple[int, int]]: (episode_number, tmdb_episode_id)。未取得時は空リスト。
        """

        payload = await cls._getJSON(
            f'/tv/{tmdb_id}/season/{season_number}',
            api_key,
            {'language': cls.PRIMARY_LANGUAGE},
        )
        if payload is None:
            raise ValueError('TMDb season was not found.')
        episodes = payload.get('episodes')
        if not isinstance(episodes, list):
            raise ValueError('TMDb episodes response is invalid.')
        result: list[tuple[int, int]] = []
        for episode in episodes:
            if (
                not isinstance(episode, dict)
                or type(episode.get('episode_number')) is not int
                or type(episode.get('id')) is not int
                or episode['episode_number'] < 1
                or episode['id'] < 1
            ):
                raise ValueError('TMDb episode is invalid.')
            result.append((episode['episode_number'], episode['id']))
        return result


    @classmethod
    async def getSeasonEpisodeDetails(
        cls,
        tmdb_id: int,
        season_number: int,
        api_key: str,
    ) -> list[TmdbSeasonEpisode]:
        """
        TV シーズンの通常エピソードを、話数照合に必要な欄付きで取得する。

        Args:
            tmdb_id (int): TMDb の TV 作品 ID。
            season_number (int): シーズン番号。
            api_key (str): TMDb API キー。

        Returns:
            list[TmdbSeasonEpisode]: 照合用の名前・放送日・尺を含むエピソード。

        Raises:
            httpx.HTTPError: 接続失敗、タイムアウト、404 以外の HTTP エラー。
            ValueError: 応答が無い、または想定外の形状の場合。
        """

        # getSeasonEpisodes() と同じ言語・パスの応答を使い、照合に要る欄だけ足す。
        ## 番号と ID の検証も同じ基準にし、構造組み立てと照合で別物を見ない。
        payload = await cls._getJSON(
            f'/tv/{tmdb_id}/season/{season_number}',
            api_key,
            {'language': cls.PRIMARY_LANGUAGE},
        )
        if payload is None:
            raise ValueError('TMDb season was not found.')
        episodes = payload.get('episodes')
        if not isinstance(episodes, list):
            raise ValueError('TMDb episodes response is invalid.')
        result: list[TmdbSeasonEpisode] = []
        for episode in episodes:
            if (
                not isinstance(episode, dict)
                or type(episode.get('episode_number')) is not int
                or type(episode.get('id')) is not int
                or episode['episode_number'] < 1
                or episode['id'] < 1
            ):
                raise ValueError('TMDb episode is invalid.')
            runtime = episode.get('runtime')
            result.append(TmdbSeasonEpisode(
                episode_number=episode['episode_number'],
                tmdb_episode_id=episode['id'],
                name=' '.join(str(episode.get('name') or '').split()),
                air_date=str(episode.get('air_date') or ''),
                runtime=runtime if type(runtime) is int and runtime > 0 else None,
            ))
        return result


    @classmethod
    async def syncAllSeries(
        cls,
        *,
        maximum_series: int | None = None,
        raise_on_error: bool = False,
        unmatched_only: bool = False,
        continue_on_item_error: bool = False,
        item_failure_callback: Callable[[], None] | None = None,
        progress_callback: Callable[[int, int, int], Awaitable[None]] | None = None,
    ) -> int:
        """
        未照合の Series を TMDb 作品へ照合し、`tmdb_*` と話数構造を補完する。

        Args:
            maximum_series: 今回処理する未照合・enrich 未完了 Series の上限。None は全件を再同期する。
            raise_on_error: 外部API失敗を秘密を含まない例外として呼び出し元へ伝播するか。
            unmatched_only: 上限なしでも未照合 Series だけを処理するか。
            continue_on_item_error: 認証失敗以外の作品単位エラー後も開始時スナップショットを走査するか。
            item_failure_callback: 継続可能な作品単位エラーを呼び出し元へ通知する関数。
            progress_callback: 処理済み件数・総件数・照合件数を作品ごとに通知する関数。

        Returns:
            int: 今回 TMDb 作品を確定できた Series 数。
        """

        async with cls._sync_lock:
            # None / BangumiOnly では TMDb 経路を一切動かさない。既存の tmdb_* は残す。
            if IsTmdbExternalMetadataEnabled() is False:
                if progress_callback is not None:
                    await progress_callback(0, 0, 0)
                return 0
            api_key = KonomiTVBS4KTmdbStore.getAPIKey()
            # API キー未設定は TMDb 経路だけを skip し、Wikipedia / Bangumi 側は止めない。
            if api_key is None:
                logging.info(
                    '[KonomiTVBS4KTmdbClient][syncAllSeries] TMDb API key is not configured. '
                    'Skipping the TMDb metadata pass.',
                )
                if progress_callback is not None:
                    await progress_callback(0, 0, 0)
                return 0
            # pipeline は開始時点の未照合 Series だけを固定する。通常の有限バッチは未照合に加え、
            ## binding 後に中断した enrich も同じ最久試行順へ戻し、別の retry 所有経路にする。
            if unmatched_only:
                target_query = Series.filter(tmdb_id__isnull=True).order_by('tmdb_last_attempt_at', 'id')
            elif maximum_series is not None:
                target_query = Series.filter(
                    Q(tmdb_id__isnull=True) | Q(tmdb_enrichment_pending=True),
                ).order_by('tmdb_last_attempt_at', 'id')
            else:
                target_query = Series.all().order_by('tmdb_last_attempt_at', 'id')
            if maximum_series is not None:
                target_query = target_query.limit(maximum_series)
            target_series = await target_query
            matched_series_count = 0
            if progress_callback is not None:
                await progress_callback(0, len(target_series), matched_series_count)
            for processed_series_count, loaded_series in enumerate(target_series, start=1):
                # 長い同期の途中でモードを無効化した場合は、次の作品から外部通信を止める。
                if not IsTmdbExternalMetadataEnabled() or not KonomiTVBS4KTmdbStore.isConfigured():
                    break
                series_completed = False
                series_matched = False
                item_failed = False
                was_unmatched = loaded_series.tmdb_id is None
                try:
                    series_matched = await cls._syncSeries(
                        loaded_series,
                        api_key,
                        raise_on_error=raise_on_error,
                    )
                    series_completed = True
                except (httpx.HTTPError, ValueError) as ex:
                    # rate limit・通信エラー・不正応答は該当 Series だけ skip し、pass 全体は続行する。
                    ## URL には API キーが含まれるため、例外メッセージとトレースバックは記録しない。
                    logging.warning(
                        f'[KonomiTVBS4KTmdbClient][syncAllSeries] Failed to fetch TMDb metadata. '
                        f'[series_id: {loaded_series.id}, error: {type(ex).__name__}]',
                    )
                    authentication_failed = (
                        isinstance(ex, httpx.HTTPStatusError)
                        and ex.response.status_code in {401, 403}
                    )
                    if raise_on_error and (continue_on_item_error is False or authentication_failed):
                        # HTTP例外のURLにはAPIキーが含まれるため、元例外を連鎖させない。
                        raise TmdbMetadataSyncError(type(ex).__name__) from None
                    item_failed = True
                    if item_failure_callback is not None:
                        item_failure_callback()
                finally:
                    # 未照合キューは binding の永続化を完了境界とする。binding 後の enrich 待機中に
                    ## cancel されても再実行対象と remaining が食い違わないよう、DB の確定値を読み直す。
                    if was_unmatched and series_matched is False:
                        series_matched = await Series.filter(
                            id=loaded_series.id,
                            tmdb_id__not_isnull=True,
                        ).exists()
                    if series_matched:
                        matched_series_count += 1
                        series_completed = True
                    elif item_failed:
                        series_completed = True
                    # 成否やキャンセルにかかわらず、外部照合を開始した Series は次の有限バッチで後方へ回す。
                    await Series.filter(id=loaded_series.id).update(tmdb_last_attempt_at=datetime.now(tz=JST))
                    if progress_callback is not None and series_completed:
                        await progress_callback(
                            processed_series_count,
                            len(target_series),
                            matched_series_count,
                        )
        logging.info(
            f'[KonomiTVBS4KTmdbClient][syncAllSeries] Synchronized TMDb metadata. '
            f'[matched_series: {matched_series_count}]',
        )
        return matched_series_count


    @classmethod
    def startPipelineSync(
        cls,
        *,
        progress_callback: Callable[[int, int, int], Awaitable[None]] | None = None,
    ) -> asyncio.Task[int]:
        """pipeline用TMDb同期を通常schedulerと同じ所有集合へ登録する。

        Args:
            progress_callback: 処理済み件数・総件数・照合件数を作品ごとに通知する関数。

        Returns:
            pipelineが完了を待ち、作品単位の失敗数を返すTMDb同期task。

        Raises:
            RuntimeError: 別のTMDb同期がすでに実行中の場合。
        """

        # 確認から登録までawaitせず、通常schedulerとの開始競合を同一イベントループ上で防ぐ。
        if cls.hasRunningSync():
            raise RuntimeError('TmdbSyncBusy')

        async def SyncPipeline() -> int:
            """開始時スナップショットを走査する。

            Returns:
                int: 作品単位の失敗数。
            """

            failed_series_count = 0

            def CountItemFailure() -> None:
                nonlocal failed_series_count
                failed_series_count += 1

            await cls.syncAllSeries(
                raise_on_error=True,
                unmatched_only=True,
                continue_on_item_error=True,
                item_failure_callback=CountItemFailure,
                progress_callback=progress_callback,
            )
            return failed_series_count

        task = asyncio.create_task(SyncPipeline())
        cls._sync_tasks.add(task)
        cls._pipeline_sync_tasks.add(task)
        task.add_done_callback(cls._sync_tasks.discard)
        task.add_done_callback(cls._pipeline_sync_tasks.discard)
        return task


    @classmethod
    def scheduleSeriesSync(cls) -> None:
        """
        新規 Series 確定後の TMDb 照合を、呼び出し元の処理と切り離して開始する。

        Returns:
            None
        """

        if IsTmdbExternalMetadataEnabled() is False:
            return
        if KonomiTVBS4KTmdbStore.isConfigured() is False:
            logging.info('[KonomiTVBS4KTmdbClient] TMDb API key is not configured. Skipping metadata sync.')
            return
        # pipeline中は別taskを積み増さない。通常scheduler同士だけは1回の再実行へ合流する。
        if any(task.done() is False for task in cls._pipeline_sync_tasks):
            return
        if any(task.done() is False for task in cls._sync_tasks):
            cls._sync_rerun = True
            return

        async def Sync() -> None:
            while True:
                cls._sync_rerun = False
                try:
                    await cls.syncAllSeries(maximum_series=1)
                except (httpx.HTTPError, ValueError) as ex:
                    logging.error(
                        '[KonomiTVBS4KTmdbClient][scheduleSeriesSync] Failed to synchronize TMDb metadata. '
                        f'[error: {type(ex).__name__}]',
                    )
                if cls._sync_rerun is False:
                    break

        # 実行中タスクへの強参照を保持し、完了時だけ集合から取り除く。
        task = asyncio.create_task(Sync())
        cls._sync_tasks.add(task)
        task.add_done_callback(cls._sync_tasks.discard)


    @classmethod
    def hasRunningSync(cls) -> bool:
        """TMDb 同期が実行中または起動済みかを返す。

        Returns:
            新しい同期を開始できない場合は True。
        """

        return cls._sync_lock.locked() or any(
            task.done() is False for task in cls._sync_tasks
        )


    @classmethod
    async def _syncSeries(
        cls,
        series: Series,
        api_key: str,
        *,
        raise_on_error: bool,
    ) -> bool:
        """
        1件の Series を TMDb 作品へ照合し、確定したら enrich する。

        Args:
            series (Series): 対象 Series。
            api_key (str): TMDb API キー。
            raise_on_error: 候補主検索の通信・応答失敗を伝播するか。

        Returns:
            bool: TMDb 作品を確定して enrich まで完了した場合は True。
        """

        if series.tmdb_id is None or series.tmdb_media_type is None:
            decision = await cls._matchSeries(series, api_key, raise_on_error=raise_on_error)
            if decision is None:
                return False
            # 照合結果を先に永続化し、enrich が失敗しても次回 pass で検索をやり直さない。
            if await cls._bindSeries(series, decision['candidate'], decision['season_number']) is False:
                return False
        return await cls.enrichSeries(series, api_key)


    @classmethod
    async def _matchSeries(
        cls,
        series: Series,
        api_key: str,
        *,
        raise_on_error: bool,
    ) -> TmdbMatchDecision | None:
        """
        検索 hints と AI 選択で Series に対応する TMDb 作品と Season を決める。

        Args:
            series (Series): 未照合の Series。
            api_key (str): TMDb API キー。
            raise_on_error: 候補主検索の通信・応答失敗を伝播するか。

        Returns:
            TmdbMatchDecision | None: 採用できる候補と Season 判定。曖昧または失敗時は None。
        """

        # 所属録画の EPG を証拠として組み立てる。番組名はタイトル検索が空のときの
        ## 補助クエリ、放送日・概要・詳細・チャンネルは AI 選択の入力に使う。
        ## 放送日は候補の排除 (ハードゲート) には使わず、矛盾判断は証拠を受け取った
        ## AI へ委ねる (コミュニティ入力の初放送日は誤登録・特別版先行で硬ゲートが成立しない)。
        member_programs = await RecordedProgram.filter(series_id=series.id).values(
            'title', 'genres', 'description', 'detail', 'start_time', 'channel__name',
        )
        epg_context = BuildSeriesEPGContext(member_programs)
        auxiliary_queries = ExtractEPGSearchQueries(member_programs, existing_title=series.title)
        candidates = await cls.searchCandidates(
            series.title,
            api_key,
            auxiliary_queries=auxiliary_queries,
            raise_on_error=raise_on_error,
        )
        # Indexer と同じ映画識別で作品種別を決め、映画 Series に TV 作品を、
        ## TV Series に映画作品を結び付けない。所属録画の EPG 題名・ジャンルを
        ## 同じ抽出で判定するため、アニメジャンルの劇場版も movie になる。
        ## 所属録画が無い Series は絞らず、AI 選択へ委ねる。
        is_movie_series: bool | None = None
        if len(member_programs) > 0:
            is_movie_series = any(
                ExtractMovieWorkTitle(
                    str(member_program.get('title') or ''),
                    (
                        member_program['genres'][0]['major']
                        if isinstance(member_program.get('genres'), list)
                        and len(member_program['genres']) > 0
                        and isinstance(member_program['genres'][0], dict)
                        else None
                    ),
                ) is not None
                for member_program in member_programs
            )
            candidates = [
                candidate for candidate in candidates
                if candidate['media_type'] == ('movie' if is_movie_series else 'tv')
            ]
        # 媒体種別の絞り込みで採用候補が空になったとき、EPG 由来の補助クエリを対象検索へ
        ## 使ってもう一度同じ採用条件を通す。それでも一意でなければ unresolved を維持する。
        for auxiliary_query in auxiliary_queries:
            if len(candidates) > 0:
                break
            retry_candidates = await cls.searchCandidates(
                auxiliary_query,
                api_key,
                raise_on_error=raise_on_error,
            )
            if is_movie_series is not None:
                retry_candidates = [
                    candidate for candidate in retry_candidates
                    if candidate['media_type'] == ('movie' if is_movie_series else 'tv')
                ]
            if len(retry_candidates) > 0:
                candidates = retry_candidates
        if len(candidates) == 0:
            return None
        # TV 候補のシーズン構成 (季番号・air_date・話数) を hints と Season 検証に使う。
        ## 候補ごとの取得失敗はその候補のシーズン情報を空にするだけで、作品候補そのものは捨てない。
        season_hints_by_id: dict[int, list[TmdbSeasonInfo]] = {}
        for candidate in candidates:
            if candidate['media_type'] != 'tv':
                continue
            try:
                candidate_details = await cls.getDetails('tv', candidate['tmdb_id'], api_key)
            except (httpx.HTTPError, ValueError) as ex:
                logging.warning(
                    f'[KonomiTVBS4KTmdbClient] TMDb candidate details skipped. '
                    f'[series_id: {series.id}, error: {type(ex).__name__}]',
                )
                candidate_details = None
            if candidate_details is not None:
                season_hints_by_id[candidate['tmdb_id']] = candidate_details['seasons']
        # 判定信号 R (解決済み話数のシーズン) を AI の Season 判定へ渡す。
        ## 録画が未解決の Series では空になり、タイトルと放送日の信号だけで判定する。
        resolved_seasons = sorted(cast(
            list[int],
            await RecordedEpisodeResolution.filter(
                recorded_program__series_id=series.id,
                season_number__not_isnull=True,
            ).distinct().values_list('season_number', flat=True),
        ))
        # 同名のリメイクや別媒体を採点だけで結び付けず、全候補を AI 選択へ回す。
        from app.metadata.KonomiTVBS4KTmdbSeriesSearch import (
            KonomiTVBS4KTmdbSeriesSearch,
        )
        return await KonomiTVBS4KTmdbSeriesSearch.findCandidateForSeries(
            series,
            candidates,
            epg_context=epg_context,
            season_hints_by_id=season_hints_by_id,
            resolved_seasons=resolved_seasons,
        )


    @classmethod
    async def _bindSeries(
        cls,
        series: Series,
        candidate: TmdbSearchCandidate,
        season_number: int | None,
    ) -> bool:
        """
        照合できた TMDb 作品と Season を Series へ保存する。

        Args:
            series (Series): 対象 Series。
            candidate (TmdbSearchCandidate): 採用する TMDb 候補。
            season_number (int | None): 確定した Season 番号。None は作品全体バインド
                (Season 判定不能・movie を含む従来動作)。

        Returns:
            bool: 保存できた場合は True。参照枠を他の Series が持つ場合は False。
        """

        # 参照単位は (作品, Season)。season 付きバインドは同じ (作品, Season) 枠だけを見て、
        ## 他 Season や作品全体バインドの有無とは無関係に許可する (task の所有規則)。
        ## season NULL の作品全体バインドは、その作品に season 付き・全体を問わず
        ## 既存バインドが1件も無いときだけ許可する (movie は常にこの経路で実質 1:1)。
        if not IsTmdbExternalMetadataEnabled() or not KonomiTVBS4KTmdbStore.isConfigured():
            return False
        if season_number is not None:
            # 同一 (作品, Season) 枠の所有者だけを見る。部分 unique Index で高々1件のため
            ## 複数行にはならないが、自分自身の再バインドは exclude で所有者扱いしない。
            owned = await Series.filter(
                tmdb_media_type=candidate['media_type'],
                tmdb_id=candidate['tmdb_id'],
                tmdb_season_number=season_number,
            ).exclude(id=series.id).exists()
        else:
            # 同一作品への既存バインドは season 付き・全体を問わず1件でもあれば競合。
            ## 多対一参照の正常状態 (同一作品に複数 Series) では複数行がヒットするため、
            ## 単一行取得 (get_or_none) は MultipleObjectsReturned で落ちる。exists() で
            ## 「1件でもある」だけを判定する。
            owned = await Series.filter(
                tmdb_media_type=candidate['media_type'], tmdb_id=candidate['tmdb_id'],
            ).exclude(id=series.id).exists()
        if owned:
            logging.info(
                f'[KonomiTVBS4KTmdbClient][_bindSeries] TMDb title is already bound to another series. '
                f'[series_id: {series.id}, tmdb_id: {candidate["tmdb_id"]}, '
                f'season_number: {season_number}]',
            )
            return False
        try:
            # 検索中に別処理が確定した ID や、Bangumi 統合で削除された Series を復活させない。
            updated = await Series.filter(id=series.id, tmdb_id__isnull=True).update(
                tmdb_id=candidate['tmdb_id'], tmdb_media_type=candidate['media_type'],
                tmdb_season_number=season_number,
                tmdb_enrichment_pending=True,
                updated_at=datetime.now(tz=JST),
            )
        except IntegrityError:
            # 別プロセスが同じ参照枠を先に確定させた。部分 unique Index を最終防線にして再試行しない。
            logging.info(
                f'[KonomiTVBS4KTmdbClient][_bindSeries] TMDb title was bound by another process. '
                f'[series_id: {series.id}, tmdb_id: {candidate["tmdb_id"]}, '
                f'season_number: {season_number}]',
            )
            return False
        if updated == 0:
            return False
        series.tmdb_id = candidate['tmdb_id']
        series.tmdb_media_type = candidate['media_type']
        series.tmdb_season_number = season_number
        return True


    @classmethod
    async def enrichSeries(cls, series: Series, api_key: str) -> bool:
        """
        確定済み TMDb 作品の詳細を `tmdb_*` カラムへ保存し、話数構造を補完する。

        Args:
            series (Series): tmdb_id と tmdb_media_type が確定済みの Series。
            api_key (str): TMDb API キー。

        Returns:
            bool: 詳細を取得して保存できた場合は True。
        """

        tmdb_id = series.tmdb_id
        media_type = series.tmdb_media_type
        if tmdb_id is None or media_type is None or not IsTmdbExternalMetadataEnabled():
            return False
        details = await cls.getDetails(media_type, tmdb_id, api_key)
        if details is None:
            logging.warning(
                f'[KonomiTVBS4KTmdbClient][enrichSeries] TMDb title was not found. '
                f'[series_id: {series.id}, tmdb_id: {tmdb_id}, media_type: {media_type}]',
            )
            return False
        # description / genres は Wikipedia AI 生成が正本のため触らず、tmdb_* だけを補完する。
        # 外部待ちの間の無効化と、別処理で統合・削除された Series を保存直前に再確認する。
        if not IsTmdbExternalMetadataEnabled() or not KonomiTVBS4KTmdbStore.isConfigured():
            return False
        # 初放送日は TMDb → Bangumi → ローカル放送開始日の優先順のため、TMDb 側は
        ## 値が取れたときだけ上書きする (空は既存の Bangumi・ローカル値を保持する)。
        ## 由来は Bangumi 側が判定するため、日付の取得有無を first_air_date_source へ記録する。
        ## season 付きバインドでは担当 Season の air_date だけを使う。作品初回は必ず
        ## 第1季の日付になり、第2季以降の Series へは誤った初放送日が入るため、
        ## 担当 Season の air_date が空・不正なら作品初回日へは倒さず、first_air_date と
        ## source を更新せず既存の Bangumi / ローカル値を保持する。
        ## 作品初回日へのフォールバックは season NULL (作品全体バインド) のみで使う。
        enrichment_fields: dict[str, Any] = {}
        if series.tmdb_season_number is not None:
            effective_first_air_date = next(
                (
                    season['air_date'] for season in details['seasons']
                    if season['season_number'] == series.tmdb_season_number
                ),
                '',
            )
        else:
            effective_first_air_date = details['first_air_date']
        parsed_first_air_date = (
            cls._parseTmdbDate(effective_first_air_date) if effective_first_air_date != '' else None
        )
        if parsed_first_air_date is not None:
            enrichment_fields['first_air_date'] = parsed_first_air_date
            enrichment_fields['first_air_date_source'] = 'Tmdb'
        # 人気度・評価は「一度だけの保存」のため、未設定の Series だけを COALESCE で埋める。
        ## 既存値がある Series は再取得しても上書きしない。
        enrichment_fields['tmdb_popularity'] = Coalesce(F('tmdb_popularity'), details['popularity'])
        enrichment_fields['tmdb_vote_average'] = Coalesce(F('tmdb_vote_average'), details['vote_average'])
        poster_identifier_changed = series.tmdb_poster_url != details['poster_path']
        updated = await Series.filter(id=series.id, tmdb_id=tmdb_id, tmdb_media_type=media_type).update(
            tmdb_name=details['name'],
            tmdb_overview=details['overview'],
            tmdb_poster_url=details['poster_path'],
            tmdb_backdrop_url=details['backdrop_url'],
            updated_at=datetime.now(tz=JST),
            **enrichment_fields,
        )
        if updated == 0:
            return False
        # 作品 ID は同じでも poster_path は差し替わるため、次の GET で新しい表紙を取り直す。
        if poster_identifier_changed:
            await KonomiTVBS4KSeriesImage.invalidateTmdbPoster(media_type, tmdb_id)
        if media_type == 'tv':
            created_count, episode_structure_completed = await cls._buildEpisodeStructure(
                series.id,
                tmdb_id,
                details,
                api_key,
            )
            if created_count > 0:
                logging.info(
                    f'[KonomiTVBS4KTmdbClient][enrichSeries] Built TMDb episode structure. '
                    f'[series_id: {series.id}, tmdb_id: {tmdb_id}, created_episodes: {created_count}]',
                )
            # 設定変更やSeries消失でseason処理を中断した場合は、通常同期へretry所有権を残す。
            if episode_structure_completed is False:
                return False
        # 詳細保存と話数構造の両方が完了してから retry 所有権を解放する。
        ## この更新前に失敗・cancel された Series は通常の有限バッチが再試行する。
        return await Series.filter(
            id=series.id,
            tmdb_id=tmdb_id,
            tmdb_media_type=media_type,
        ).update(tmdb_enrichment_pending=False) == 1


    @classmethod
    async def _buildEpisodeStructure(
        cls,
        series_id: int,
        tmdb_id: int,
        details: TmdbSeriesDetails,
        api_key: str,
    ) -> tuple[int, bool]:
        """
        Bangumi 由来の話数構造が無い Series だけ、TMDb シーズンから話を追加する。

        Args:
            series_id (int): 対象 Series ID。
            tmdb_id (int): TMDb の TV 作品 ID。
            details (TmdbSeriesDetails): シーズン一覧を含む作品詳細。
            api_key (str): TMDb API キー。

        Returns:
            tuple[int, bool]: 今回追加した SeriesEpisode の件数と、必須のseason処理を完了したか。
        """

        # Bangumi の episode ID が付いた話数構造がある Series では、ユーザーコレクションを正とし、
        # TMDb のシーズン構成で上書きも追加もしない。
        if await SeriesEpisode.filter(series_id=series_id, bangumi_episode_id__not_isnull=True).exists():
            return (0, True)
        # season 付きバインドでは担当 Season の話数構造だけを作る。season NULL (作品全体) は
        ## 従来どおり全レギュラーシーズンを構築する (movie はここへ来ない)。
        owned_season_numbers = cast(
            list[int | None],
            await Series.filter(id=series_id).values_list('tmdb_season_number', flat=True),
        )
        owned_season_number = owned_season_numbers[0] if len(owned_season_numbers) > 0 else None
        created_count = 0
        episode_structure_completed = True
        for season_number in details['season_numbers']:
            # 特別編 (season 0) は放送話数ではないため、話数構造の列へは載せない。
            if season_number < 1:
                continue
            # season 付きバインドの Series へ他 Season の話数構造は作らない。
            if owned_season_number is not None and season_number != owned_season_number:
                continue
            if not IsTmdbExternalMetadataEnabled() or not KonomiTVBS4KTmdbStore.isConfigured():
                episode_structure_completed = False
                break
            episodes = await cls.getSeasonEpisodes(tmdb_id, season_number, api_key)
            # ネットワーク待ちをトランザクションの外に出し、話数の作成と優先ソースの検査だけを直列化する。
            async with in_transaction():
                if not IsTmdbExternalMetadataEnabled() or not KonomiTVBS4KTmdbStore.isConfigured():
                    episode_structure_completed = False
                    break
                if not await Series.filter(id=series_id, tmdb_id=tmdb_id, tmdb_media_type='tv').exists():
                    episode_structure_completed = False
                    break
                if await SeriesEpisode.filter(series_id=series_id, bangumi_episode_id__not_isnull=True).exists():
                    break
                for episode_number, tmdb_episode_id in episodes:
                    # ローカル確定済みの話数と参照は保ち、欠けた TMDb ID だけを補完する。
                    episode, created = await SeriesEpisode.get_or_create(
                        series_id=series_id,
                        season_number=season_number,
                        episode_number=Decimal(episode_number),
                        defaults={'tmdb_episode_id': tmdb_episode_id},
                    )
                    if created:
                        created_count += 1
                    elif episode.tmdb_episode_id is None:
                        episode.tmdb_episode_id = tmdb_episode_id
                        await episode.save(update_fields=['tmdb_episode_id', 'updated_at'])
        # 担当 Season が変わった Series に残る旧 Season の構造を後始末する。削除するのは
        ## TMDb 由来 (tmdb_episode_id 付き) で、録画・現在の判定・手動レーンのいずれからも
        ## 参照されていない行だけ。手動レーン (manual_episode_id) は AI 採用後も
        ## 「再び手動へ戻せる」永続契約のため、そこから参照される行も残す。
        ## 参照済みの行は再生・一覧の正本のため、担当外 Season であっても残す。
        if owned_season_number is not None:
            # 手動レーン (manual_episode_id) が参照する話数 ID を独立 SELECT で取得する。
            ## recorded_episode_resolutions (episode_id) と manual_recorded_episode_resolutions
            ## (manual_episode_id) は同じテーブルへの reverse FK で、Tortoise 1.1.8 は
            ## 1つの queryset 内で JOIN をテーブル単位に共用するため、両者を同一 filter に
            ## 重ねると manual_episode_id の JOIN/WHERE が生成されず手動参照-only の行が
            ## 誤って stale 候補に入る。手動レーンの保護は別クエリで行う。
            manual_referenced_ids = cast(
                list[int],
                await RecordedEpisodeResolution.filter(
                    manual_episode_id__not_isnull=True,
                    manual_episode__series_id=series_id,
                ).values_list('manual_episode_id', flat=True),
            )
            # Tortoise の queryset.delete() は reverse-FK 参照を含めた JOIN 付き DELETE を
            ## 生成し SQLite で構文エラーになるため、ID 集合を SELECT で先に確定してから
            ## 主キーだけで削除する。現在判定レーン (recorded_episode_resolutions__isnull)
            ## の JOIN は episode_id 用で正しく生成されるため queryset に残す。
            stale_episode_ids = cast(
                list[int],
                await SeriesEpisode.filter(
                    series_id=series_id,
                    tmdb_episode_id__not_isnull=True,
                    recorded_programs__isnull=True,
                    recorded_episode_resolutions__isnull=True,
                ).exclude(
                    season_number=owned_season_number,
                ).exclude(
                    id__in=manual_referenced_ids,
                ).values_list('id', flat=True),
            )
            if len(stale_episode_ids) > 0:
                await SeriesEpisode.filter(id__in=stale_episode_ids).delete()
                logging.info(
                    f'[KonomiTVBS4KTmdbClient][_buildEpisodeStructure] Removed stale TMDb episode structure. '
                    f'[series_id: {series_id}, tmdb_id: {tmdb_id}, removed_episodes: {len(stale_episode_ids)}]',
                )
        return (created_count, episode_structure_completed)


    @classmethod
    async def _search(
        cls,
        path: str,
        query: str,
        api_key: str,
    ) -> list[dict[str, Any]]:
        """
        TV または映画を検索し、hints に必要な欄だけを取り出す。

        Args:
            path (str): '/search/tv' または '/search/movie'。
            query (str): ローカル Series タイトル。
            api_key (str): TMDb API キー。

        Returns:
            list[dict[str, Any]]: 検索結果。失敗時は空リスト。
        """

        payload = await cls._getJSON(
            path,
            api_key,
            {
                'query': query,
                'language': cls.PRIMARY_LANGUAGE,
                'include_adult': 'false',
                'page': 1,
            },
        )
        if payload is None:
            return []
        results = payload.get('results')
        if isinstance(results, list) is False:
            raise ValueError('TMDb search response is invalid.')
        # 検索結果にも日本語の欠損補完を適用する。並び順と候補 ID は日本語検索を優先する。
        fallback_by_id: dict[int, dict[str, Any]] = {}
        if not results or any(
            isinstance(result, dict) and (not cls._extractName(result) or not result.get('overview'))
            for result in results[:cls.SEARCH_LIMIT]
        ):
            try:
                fallback = await cls._getJSON(
                    path, api_key,
                    {'query': query, 'language': cls.FALLBACK_LANGUAGE, 'include_adult': 'false', 'page': 1},
                )
                fallback_results = fallback.get('results') if fallback is not None else None
                if isinstance(fallback_results, list):
                    fallback_by_id = {
                        result['id']: result for result in fallback_results[:cls.SEARCH_LIMIT]
                        if isinstance(result, dict) and type(result.get('id')) is int
                    }
                    if not results:
                        results = list(fallback_by_id.values())
            except (httpx.HTTPError, ValueError) as ex:
                logging.warning(f'[KonomiTVBS4KTmdbClient] English search skipped. [error: {type(ex).__name__}]')
        searched: list[dict[str, Any]] = []
        for result in cast(list[Any], results)[:cls.SEARCH_LIMIT]:
            if isinstance(result, dict) is False:
                continue
            typed_result = cast(dict[str, Any], result)
            tmdb_id = typed_result.get('id')
            if type(tmdb_id) is not int or tmdb_id < 1:
                continue
            fallback_result = fallback_by_id.get(tmdb_id, {})
            searched.append({
                'tmdb_id': tmdb_id,
                # TV は name / original_name、映画は title / original_title を返す。
                'name': cls._extractName(typed_result) or cls._extractName(fallback_result),
                'original_name': str(
                    typed_result.get('original_name') or typed_result.get('original_title') or '',
                ),
                'overview': typed_result.get('overview') or fallback_result.get('overview'),
                'genre_ids': typed_result.get('genre_ids'),
                'first_air_date': typed_result.get('first_air_date'),
                'release_date': typed_result.get('release_date'),
            })
        return searched


    @classmethod
    async def _getGenreNames(cls, path: str, api_key: str) -> dict[int, str]:
        """
        TMDb のジャンル一覧を ID から日本語名へ引ける形にして返す。

        Args:
            path (str): '/genre/tv/list' または '/genre/movie/list'。
            api_key (str): TMDb API キー。

        Returns:
            dict[int, str]: ジャンル ID と表示名。失敗時は空辞書。
        """

        # ジャンルは hints の補足であり、取得失敗で作品候補そのものを捨てない。
        try:
            payload = await cls._getJSON(path, api_key, {'language': cls.PRIMARY_LANGUAGE})
        except (httpx.HTTPError, ValueError) as ex:
            logging.warning(f'[KonomiTVBS4KTmdbClient] Genre hints skipped. [error: {type(ex).__name__}]')
            return {}
        if payload is None:
            return {}
        genres = payload.get('genres')
        if isinstance(genres, list) is False:
            return {}
        return {
            int(genre['id']): str(genre.get('name') or '')
            for genre in cast(list[Any], genres)
            if isinstance(genre, dict) and isinstance(genre.get('id'), int)
        }


    @classmethod
    async def _getAlternativeTitles(
        cls,
        media_type: TmdbMediaType,
        tmdb_id: int,
        api_key: str,
    ) -> list[str]:
        """
        作品の別名タイトルを解決 hints 用に取得する。永続化はしない。

        Args:
            media_type (TmdbMediaType): 'tv' または 'movie'。
            tmdb_id (int): TMDb 作品 ID。
            api_key (str): TMDb API キー。

        Returns:
            list[str]: 重複を除いた別名タイトル。失敗時は空リスト。
        """

        # 別名は補足 hints なので、このエンドポイントだけ失敗しても通常の題名で照合を続ける。
        try:
            payload = await cls._getJSON(
                cls._detailsPath(media_type, tmdb_id) + '/alternative_titles',
                api_key,
                {},
            )
        except (httpx.HTTPError, ValueError) as ex:
            logging.warning(f'[KonomiTVBS4KTmdbClient] Alternative titles skipped. [error: {type(ex).__name__}]')
            return []
        if payload is None:
            return []
        # TV は results、映画は titles へ別名を返す。
        raw_titles = payload.get('results') if media_type == 'tv' else payload.get('titles')
        if isinstance(raw_titles, list) is False:
            return []
        titles: list[str] = []
        for raw_title in cast(list[Any], raw_titles):
            if isinstance(raw_title, dict) is False:
                continue
            title = ' '.join(str(cast(dict[str, Any], raw_title).get('title') or '').split())
            if title == '' or title in titles:
                continue
            titles.append(title)
            if len(titles) >= cls.ALTERNATIVE_TITLE_LIMIT:
                break
        return titles


    @classmethod
    async def _getJSON(
        cls,
        path: str,
        api_key: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        """
        TMDb v3 API を1回呼び、JSON object を返す。

        Args:
            path (str): API_BASE_URL に続くパス。
            api_key (str): query string で送る TMDb v3 API キー。
            params (dict[str, Any]): パス以外の query 引数。

        Returns:
            dict[str, Any] | None: 応答 JSON。作品が無い (404) 場合は None。

        Raises:
            httpx.HTTPError: 接続失敗、タイムアウト、404 以外の HTTP エラー。
            ValueError: 応答が JSON object ではない場合。
        """

        async with TMDB_HTTPX_CLIENT() as httpx_client:
            try:
                # read timeout をリセットし続ける低速応答でも、1リクエストの総待機を有限にする。
                async with asyncio.timeout(15):
                    response = await httpx_client.get(
                        url=f'{cls.API_BASE_URL}{path}',
                        params={**params, 'api_key': api_key},
                    )
            except (TimeoutError, httpx.TimeoutException):
                raise httpx.TimeoutException('TMDb request timed out.') from None
            except httpx.HTTPError as ex:
                # URL に API キーが含まれるため、例外メッセージとトレースバックは残さない。
                logging.warning(
                    f'[KonomiTVBS4KTmdbClient][_getJSON] TMDb request failed. '
                    f'[path: {path}, error: {type(ex).__name__}]',
                )
                raise httpx.HTTPError('TMDb request failed.') from None
            # 削除済み・未公開の作品は 404 になり、これは障害ではない。
            if response.status_code == 404:
                return None
            if response.status_code == 401:
                # キーの無効化・失効は利用者が調査すべき状態なので、明示的に警告する。
                logging.warning(
                    f'[KonomiTVBS4KTmdbClient][_getJSON] TMDb API key was rejected. [path: {path}]',
                )
            elif response.status_code == 429:
                logging.warning(
                    f'[KonomiTVBS4KTmdbClient][_getJSON] TMDb rate limit was reached. [path: {path}]',
                )
            if response.status_code >= 400:
                # raise_for_status() は API キーを含む URL をメッセージへ埋め込むため使わない。
                ## 呼び出し側の except httpx.HTTPError はそのまま機能させる。
                raise httpx.HTTPStatusError(
                    f'TMDb API returned HTTP {response.status_code} for {path}',
                    request=response.request,
                    response=response,
                )
            try:
                payload = response.json()
            except ValueError:
                raise ValueError('TMDb API response is invalid.') from None
        if isinstance(payload, dict) is False:
            raise ValueError('TMDb API response is invalid.')
        return cast(dict[str, Any], payload)


    @classmethod
    def _httpErrorResult(
        cls,
        http_status: int,
        started_at: float,
    ) -> TmdbConnectionTestResult:
        """
        接続試験の HTTP エラーを、利用者が対処できるメッセージへ変換する。

        Args:
            http_status (int): TMDb が返したステータスコード。
            started_at (float): 試験開始時の time.monotonic()。

        Returns:
            TmdbConnectionTestResult: 失敗を示す接続試験結果。
        """

        latency_ms = cls._elapsedMilliseconds(started_at)
        if http_status == 401:
            return TmdbConnectionTestResult(
                success=False,
                latency_ms=latency_ms,
                message='TMDb API キーが無効です。',
                http_status=http_status,
                error_code='InvalidAPIKey',
            )
        if http_status == 429:
            return TmdbConnectionTestResult(
                success=False,
                latency_ms=latency_ms,
                message='TMDb のレート制限に達しました。しばらくしてから再試行してください。',
                http_status=http_status,
                error_code='RateLimited',
            )
        return TmdbConnectionTestResult(
            success=False,
            latency_ms=latency_ms,
            message=f'TMDb API が HTTP {http_status} を返しました。',
            http_status=http_status,
            error_code='HTTPError',
        )


    @classmethod
    def _networkErrorResult(
        cls,
        error_name: str,
        started_at: float,
    ) -> TmdbConnectionTestResult:
        """
        接続試験の通信失敗を、URL を含まない結果へ変換する。

        Args:
            error_name (str): 発生した httpx 例外のクラス名。
            started_at (float): 試験開始時の time.monotonic()。

        Returns:
            TmdbConnectionTestResult: 失敗を示す接続試験結果。
        """

        logging.warning(
            f'[KonomiTVBS4KTmdbClient][testConnection] Failed to reach TMDb. [error: {error_name}]',
        )
        return TmdbConnectionTestResult(
            success=False,
            latency_ms=cls._elapsedMilliseconds(started_at),
            message='TMDb へ接続できませんでした。',
            http_status=None,
            error_code=error_name,
        )


    @staticmethod
    def _detailsPath(media_type: TmdbMediaType, tmdb_id: int) -> str:
        """
        作品種別に応じた詳細エンドポイントのパスを返す。

        Args:
            media_type (TmdbMediaType): 'tv' または 'movie'。
            tmdb_id (int): TMDb 作品 ID。

        Returns:
            str: '/tv/{id}' または '/movie/{id}'。
        """

        return f'/tv/{tmdb_id}' if media_type == 'tv' else f'/movie/{tmdb_id}'


    @staticmethod
    def _buildImageURL(raw_path: Any, size: str) -> str | None:
        """
        TMDb の画像パスを絶対 URL へ変換する。

        Args:
            raw_path (Any): 応答の poster_path / backdrop_path。
            size (str): 画像サイズ識別子。

        Returns:
            str | None: 画像が無い・形式が異なる場合は None。
        """

        if isinstance(raw_path, str) is False or raw_path == '':
            return None
        return f'{KonomiTVBS4KTmdbClient.IMAGE_BASE_URL}/{size}{raw_path}'


    @staticmethod
    def _extractImagePath(raw_path: Any) -> str | None:
        """
        TMDb 応答の画像パスをローカル取得用のソース識別子として取り出す。

        Args:
            raw_path (Any): 応答の poster_path。

        Returns:
            str | None: `/` 始まりの画像パス。画像が無い・形式が異なる場合は None。
        """

        if isinstance(raw_path, str) is False or raw_path.startswith('/') is False or raw_path.startswith('//'):
            return None
        return raw_path


    @staticmethod
    def _extractName(payload: dict[str, Any]) -> str:
        """
        TV / 映画の応答から作品名を取り出す。

        Args:
            payload (dict[str, Any]): TMDb の作品詳細応答。

        Returns:
            str: 作品名。無ければ空文字。
        """

        return ' '.join(str(payload.get('name') or payload.get('title') or '').split())


    @staticmethod
    def _elapsedMilliseconds(started_at: float) -> int:
        """
        開始時刻からの経過ミリ秒を返す。

        Args:
            started_at (float): time.monotonic() の開始値。

        Returns:
            int: 0 以上の経過ミリ秒。
        """

        return max(0, int((time.monotonic() - started_at) * 1000))
