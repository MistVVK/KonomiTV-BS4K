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
    IsCandidateBroadcastConsistent,
)
from app.metadata.RecordedSeriesSettings import IsTmdbExternalMetadataEnabled
from app.metadata.SeriesTitleParser import (
    ExtractEPGSearchQueries,
    ExtractMovieWorkTitle,
)
from app.models.RecordedEpisode import SeriesEpisode
from app.models.RecordedProgram import RecordedProgram
from app.models.Series import Series, TmdbMediaType
from app.utils.KonomiTVBS4KTmdbStore import KonomiTVBS4KTmdbStore


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
    # TV は初回放送日、映画は公開日 (YYYY-MM-DD)。不明なときは空文字。
    first_air_date: str
    # 人気度と評価。不明なときは None。
    popularity: float | None
    vote_average: float | None


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
        # 一方の検索が失敗しても、取得できた種別の hints は照合へ渡す。
        results_by_type: dict[TmdbMediaType, list[dict[str, Any]]] = {}
        for media_type in ('tv', 'movie'):
            try:
                results_by_type[media_type] = await cls._search(f'/search/{media_type}', series_title, api_key)
                if not results_by_type[media_type] and quoted_title and quoted_title != series_title:
                    results_by_type[media_type] = await cls._search(f'/search/{media_type}', quoted_title, api_key)
                # Series タイトルと引用内作品名の両方が空のとき、EPG 由来の補助クエリを
                ## 順に試す。EPG 全文をクエリへ足すことはせず、作品名だけを検索語にする。
                ## 補助クエリの1件が失敗しても、取得済みの結果は捨てない。
                for auxiliary_query in auxiliary_queries or []:
                    if results_by_type[media_type]:
                        break
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
        if media_type == 'tv':
            seasons = payload.get('seasons')
            if not isinstance(seasons, list):
                raise ValueError('TMDb seasons response is invalid.')
            for season in seasons:
                if not isinstance(season, dict) or type(season.get('season_number')) is not int:
                    raise ValueError('TMDb season number is invalid.')
                season_numbers.append(season['season_number'])
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
            candidate = await cls._matchSeries(series, api_key, raise_on_error=raise_on_error)
            if candidate is None:
                return False
            # 照合結果を先に永続化し、enrich が失敗しても次回 pass で検索をやり直さない。
            if await cls._bindSeries(series, candidate) is False:
                return False
        return await cls.enrichSeries(series, api_key)


    @classmethod
    async def _matchSeries(
        cls,
        series: Series,
        api_key: str,
        *,
        raise_on_error: bool,
    ) -> TmdbSearchCandidate | None:
        """
        検索 hints と AI 選択で Series に対応する TMDb 作品を決める。

        Args:
            series (Series): 未照合の Series。
            api_key (str): TMDb API キー。
            raise_on_error: 候補主検索の通信・応答失敗を伝播するか。

        Returns:
            TmdbSearchCandidate | None: 採用できる候補。曖昧または失敗時は None。
        """

        # 所属録画の EPG を証拠として組み立てる。番組名はタイトル検索が空のときの
        ## 補助クエリ、放送日は候補の初放送・公開日との突合、概要と詳細とチャンネルは
        ## AI 選択の入力に使う。
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
        min_broadcast_date = epg_context['min_broadcast_date'] if epg_context is not None else ''
        # すべての録画より後に初放送・公開される候補は、その録画の取得元になり得ないため落とす。
        candidates = cls._filterBroadcastConsistentCandidates(candidates, min_broadcast_date)
        # 日付突合などで採用候補が空になったとき、EPG 由来の補助クエリを対象検索へ
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
            retry_candidates = cls._filterBroadcastConsistentCandidates(retry_candidates, min_broadcast_date)
            if len(retry_candidates) > 0:
                candidates = retry_candidates
        if len(candidates) == 0:
            return None
        # 同名のリメイクや別媒体を採点だけで結び付けず、全候補を AI 選択へ回す。
        from app.metadata.KonomiTVBS4KTmdbSeriesSearch import (
            KonomiTVBS4KTmdbSeriesSearch,
        )
        return await KonomiTVBS4KTmdbSeriesSearch.findCandidateForSeries(
            series, candidates, epg_context=epg_context,
        )


    @staticmethod
    def _filterBroadcastConsistentCandidates(
        candidates: list[TmdbSearchCandidate],
        min_broadcast_date: str,
    ) -> list[TmdbSearchCandidate]:
        """
        所属録画の最古放送日より後に初放送・公開される候補を落とす。

        Args:
            candidates (list[TmdbSearchCandidate]): 媒体種別の絞り込み済み TMDb 候補。
            min_broadcast_date (str): 所属録画の最古の放送開始日 (YYYY-MM-DD)。不明は空文字。

        Returns:
            list[TmdbSearchCandidate]: 放送日・公開日が矛盾しない候補。
        """

        # 検索結果が空文字の日付は不明として保持するため、候補の排除はここに一元する。
        return [
            candidate for candidate in candidates
            if IsCandidateBroadcastConsistent(candidate['first_air_date'], min_broadcast_date)
        ]


    @classmethod
    async def _bindSeries(cls, series: Series, candidate: TmdbSearchCandidate) -> bool:
        """
        照合できた TMDb 作品 ID を Series へ保存する。

        Args:
            series (Series): 対象 Series。
            candidate (TmdbSearchCandidate): 採用する TMDb 候補。

        Returns:
            bool: 保存できた場合は True。他の Series が同じ作品を持つ場合は False。
        """

        # TV と映画を区別した複合一意索引により、同じ作品を2つの Series へは結び付けない。
        ## Bangumi のような統合は行わず、先に照合した Series を正本として今回は skip する。
        if not IsTmdbExternalMetadataEnabled() or not KonomiTVBS4KTmdbStore.isConfigured():
            return False
        owner = await Series.get_or_none(
            tmdb_media_type=candidate['media_type'], tmdb_id=candidate['tmdb_id'],
        )
        if owner is not None and owner.id != series.id:
            logging.info(
                f'[KonomiTVBS4KTmdbClient][_bindSeries] TMDb title is already bound to another series. '
                f'[series_id: {series.id}, tmdb_id: {candidate["tmdb_id"]}, owner_series_id: {owner.id}]',
            )
            return False
        try:
            # 検索中に別処理が確定した ID や、Bangumi 統合で削除された Series を復活させない。
            updated = await Series.filter(id=series.id, tmdb_id__isnull=True).update(
                tmdb_id=candidate['tmdb_id'], tmdb_media_type=candidate['media_type'],
                tmdb_enrichment_pending=True,
                updated_at=datetime.now(tz=JST),
            )
        except IntegrityError:
            # 別プロセスが同じ作品を先に確定させた。一意索引を最終防線にして再試行しない。
            logging.info(
                f'[KonomiTVBS4KTmdbClient][_bindSeries] TMDb title was bound by another process. '
                f'[series_id: {series.id}, tmdb_id: {candidate["tmdb_id"]}]',
            )
            return False
        if updated == 0:
            return False
        series.tmdb_id = candidate['tmdb_id']
        series.tmdb_media_type = candidate['media_type']
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
        enrichment_fields: dict[str, Any] = {}
        parsed_first_air_date = (
            cls._parseTmdbDate(details['first_air_date']) if details['first_air_date'] != '' else None
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
        created_count = 0
        episode_structure_completed = True
        for season_number in details['season_numbers']:
            # 特別編 (season 0) は放送話数ではないため、話数構造の列へは載せない。
            if season_number < 1:
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
