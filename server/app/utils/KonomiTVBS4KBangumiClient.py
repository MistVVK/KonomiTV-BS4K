from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
from typing import Any, Protocol, cast

import httpx
from tortoise.exceptions import IntegrityError

from app import logging, schemas
from app.constants import BANGUMI_REQUEST_HEADERS, HTTPX_CLIENT
from app.metadata.RecordedEpisodeResolver import ParseSinglePositiveIntegerEpisode
from app.metadata.RecordedSeriesSettings import IsBangumiExternalMetadataEnabled
from app.metadata.SeriesIndexer import IsStrictSeriesTitlePrefix, NormalizeSeriesTitle
from app.metadata.SeriesMerger import SeriesMerger
from app.models.RecordedProgram import RecordedProgram
from app.models.Series import Series
from app.utils.KonomiTVBS4KBangumiSharedStore import (
    BangumiTokenDecryptionError,
    KonomiTVBS4KBangumiSharedStore,
)


class BangumiCollectionOwner(Protocol):
    """收藏一覧同期に必要な Bangumi 認証情報。"""

    bangumi_user_name: str

    def decryptBangumiAccessToken(self) -> str:
        """復号済みの個人アクセストークンを返す。"""
        ...


class _SharedCollectionOwner:
    """共有ストアから読む Bangumi 認証情報。"""

    def __init__(self, bangumi_user_name: str, access_token: str) -> None:
        self.bangumi_user_name = bangumi_user_name
        self._access_token = access_token

    def decryptBangumiAccessToken(self) -> str:
        return self._access_token


class KonomiTVBS4KBangumiClient:
    """KonomiTV の録画番組を Bangumi の条目とエピソードへ照合する。"""

    API_BASE_URL = 'https://api.bgm.tv/v0'
    COLLECTION_PAGE_SIZE = 100
    EPISODE_PAGE_SIZE = 200
    # scheduleUserCollectionSync() が起動した未完了タスク。完了時に discard する。
    _sync_tasks: set[asyncio.Task[None]] = set()
    # 実行中に再予約されたら、完了後にもう一度だけ收藏同期する。
    _sync_rerun: bool = False
    _subject_merge_locks: dict[int, asyncio.Lock] = {}
    _series_merge_locks: dict[int, asyncio.Lock] = {}


    @staticmethod
    def parseEpisodeNumber(episode_number: str | None) -> int | None:
        """
        自動同期で安全に扱える単一の正整数話数だけを取得する。

        Args:
            episode_number (str | None): EPG から抽出した話数。

        Returns:
            int | None: 単一の正整数であればその値、それ以外は None。
        """

        return ParseSinglePositiveIntegerEpisode(episode_number)


    @staticmethod
    def _normalizeSubjectTitle(title: str) -> str:
        """
        ローカル Series と Bangumi 条目のタイトル比較キーを生成する。

        Args:
            title (str): ローカル Series または Bangumi 条目のタイトル。

        Returns:
            str: Unicode・空白・末尾句読点の表記差を吸収した比較キー。
        """

        # EPG と Bangumi では長い作品名の末尾句点だけが欠けることがあるため、その差だけを追加で吸収する。
        return NormalizeSeriesTitle(title).rstrip('。.!！?？')


    @classmethod
    def _scoreSubjectTitle(cls, series_title: str, subject: dict[str, Any]) -> int:
        """
        ユーザーの收藏済みアニメからローカル Series の条目候補を採点する。

        Args:
            series_title (str): ローカル Series の表示タイトル。
            subject (dict[str, Any]): Bangumi 收藏一覧に含まれる条目概要。

        Returns:
            int: タイトル一致度。候補外の場合は 0。
        """

        local_title = cls._normalizeSubjectTitle(series_title)
        subject_titles = {
            cls._normalizeSubjectTitle(str(subject.get('name', ''))),
            cls._normalizeSubjectTitle(str(subject.get('name_cn', ''))),
        } - {''}
        if len(subject_titles) == 0:
            return 0
        if local_title in subject_titles:
            return 100

        # 放送局が副題を省略した表記は、安全な副題境界を持つ場合だけ弱い候補として認める。
        if any(
            IsStrictSeriesTitlePrefix(local_title, subject_title) or
            IsStrictSeriesTitlePrefix(subject_title, local_title)
            for subject_title in subject_titles
        ):
            return 80

        # 收藏一覧に絞った後も僅かな記号・転写差が残るため、長いタイトル同士だけ保守的に類似判定する。
        similarity = max(
            (SequenceMatcher(None, local_title, subject_title).ratio() for subject_title in subject_titles),
            default = 0.0,
        )
        if min([len(local_title), *(len(subject_title) for subject_title in subject_titles)]) >= 8 and similarity >= 0.9:
            return int(similarity * 75)
        return 0


    @staticmethod
    def isPlaybackCompleted(
        playback_position: float,
        duration: float,
        cm_sections: list[schemas.CMSection] | None,
    ) -> bool:
        """
        プレイヤーが報告した実再生時間から Bangumi の視聴完了を判定する。

        Args:
            playback_position (float): 現在の再生位置 (秒)。
            duration (float): サーバーに保存されている録画時間 (秒)。
            cm_sections (list[schemas.CMSection] | None): サーバーで検出済みの CM 区間。

        Returns:
            bool: CM 区間を考慮した視聴完了位置まで再生済みなら True。
        """

        return playback_position >= schemas.GetPlaybackCompletionThreshold(duration, cm_sections)


    @classmethod
    def findSubject(cls, series_title: str, subjects: list[dict[str, Any]]) -> dict[str, Any] | None:
        """
        用户の在看・看過收藏からローカル Series に対応する条目を一意に選ぶ。

        Args:
            series_title (str): ローカル Series の表示タイトル。
            subjects (list[dict[str, Any]]): 在看・看過のアニメ条目一覧。

        Returns:
            dict[str, Any] | None: 十分に一意な最高得点候補。
        """

        candidates = sorted(
            (
                (cls._scoreSubjectTitle(series_title, subject), subject)
                for subject in subjects
            ),
            key = lambda candidate: candidate[0],
            reverse = True,
        )
        if len(candidates) == 0 or candidates[0][0] < 67:
            return None
        if len(candidates) >= 2 and candidates[0][0] - candidates[1][0] < 10:
            return None
        return candidates[0][1]


    @classmethod
    async def _getCollectionSubjects(cls, user: BangumiCollectionOwner) -> list[dict[str, Any]]:
        """
        連携ユーザーの「在看」「看過」アニメ条目を收藏一覧から一括取得する。

        Args:
            user (User): Bangumi アカウント連携済みの KonomiTV ユーザー。

        Returns:
            list[dict[str, Any]]: 条目 ID ごとに重複を除いた收藏済みアニメ概要。

        Raises:
            httpx.HTTPError: Bangumi API への接続または HTTP エラーが発生した場合。
        """

        assert user.bangumi_user_name is not None
        access_token = user.decryptBangumiAccessToken()
        headers = {**BANGUMI_REQUEST_HEADERS, 'Authorization': f'Bearer {access_token}'}
        subjects: dict[int, dict[str, Any]] = {}
        offset = 0
        async with HTTPX_CLIENT() as httpx_client:
            while True:
                # 收藏状態を API 側で分割せず全件取得し、在看・看過だけをローカルで選ぶ。
                ## これにより、ユーザーごとの候補一覧はページ数分のリクエストだけで揃う。
                response = await httpx_client.get(
                    url = f'{cls.API_BASE_URL}/users/{user.bangumi_user_name}/collections',
                    headers = headers,
                    params = {
                        'subject_type': 2,
                        'limit': cls.COLLECTION_PAGE_SIZE,
                        'offset': offset,
                    },
                )
                response.raise_for_status()
                payload = cast(dict[str, Any], response.json())
                collections = cast(list[dict[str, Any]], payload.get('data', []))
                for collection in collections:
                    if int(collection.get('type', -1)) not in {2, 3}:
                        continue
                    subject = collection.get('subject')
                    if not isinstance(subject, dict) or int(subject.get('type', -1)) != 2:
                        continue
                    subjects[int(subject['id'])] = cast(dict[str, Any], subject)

                offset += len(collections)
                if offset >= int(payload.get('total', 0)) or len(collections) == 0:
                    break
        return list(subjects.values())


    @classmethod
    async def _getEpisodes(cls, subject_id: int, access_token: str) -> list[dict[str, Any]]:
        """
        照合済み Bangumi 条目の通常エピソードを全ページ取得する。

        Args:
            subject_id (int): Bangumi 条目 ID。
            access_token (str): Bangumi 個人アクセストークン。

        Returns:
            list[dict[str, Any]]: 条目に属する通常エピソード。

        Raises:
            httpx.HTTPError: Bangumi API への接続または HTTP エラーが発生した場合。
        """

        episodes: list[dict[str, Any]] = []
        offset = 0
        async with HTTPX_CLIENT() as httpx_client:
            while True:
                response = await httpx_client.get(
                    url = f'{cls.API_BASE_URL}/episodes',
                    # NSFW 条目は匿名アクセスを 404 に偽装するため、收藏一覧と同じ認証を必ず引き継ぐ。
                    headers = {**BANGUMI_REQUEST_HEADERS, 'Authorization': f'Bearer {access_token}'},
                    params = {
                        'subject_id': subject_id,
                        'type': 0,
                        'limit': cls.EPISODE_PAGE_SIZE,
                        'offset': offset,
                    },
                )
                # 認証後も閲覧できない条目、または削除済み条目だけ episode 未照合として扱う。
                if response.status_code == 404:
                    logging.warning(
                        f'[KonomiTVBS4KBangumiClient][_getEpisodes] Bangumi subject was not found. '
                        f'[subject_id: {subject_id}]',
                    )
                    return []
                response.raise_for_status()
                payload = cast(dict[str, Any], response.json())
                page_episodes = cast(list[dict[str, Any]], payload.get('data', []))
                episodes.extend(page_episodes)
                offset += len(page_episodes)
                if offset >= int(payload.get('total', 0)) or len(page_episodes) == 0:
                    break
        return episodes


    @classmethod
    async def syncUserCollections(cls, user: BangumiCollectionOwner) -> int:
        """
        連携ユーザーの收藏一覧を候補プールとしてローカル Series と全録画を照合する。

        Args:
            user (User): Bangumi アカウント連携済みの KonomiTV ユーザー。

        Returns:
            int: 今回 Bangumi 条目へ照合できた Series 数。

        Raises:
            httpx.HTTPError: Bangumi API への接続または HTTP エラーが発生した場合。
        """

        # 公開の同期経路を直接呼んだ場合も、無効モードでは新規の照合を開始しない。
        if IsBangumiExternalMetadataEnabled() is False:
            return 0
        anime_series = [
            series for series in await Series.all()
            if any(genre['major'] == 'アニメ・特撮' for genre in series.genres)
        ]
        # ローカルにアニメ・特撮の Series が一件もなければ、Bangumi API 自体へアクセスしない。
        if len(anime_series) == 0:
            return 0

        subjects = await cls._getCollectionSubjects(user)
        access_token = user.decryptBangumiAccessToken()
        episodes_by_subject_id: dict[int, list[dict[str, Any]]] = {}
        matched_series_ids: set[int] = set()

        for loaded_series in anime_series:
            if IsBangumiExternalMetadataEnabled() is False:
                break
            series_lock = cls._series_merge_locks.setdefault(loaded_series.id, asyncio.Lock())
            async with series_lock:
                # ロック後に DB を読み直し、開始時スナップショットの未確定判定で上書きしない。
                series = await Series.get_or_none(id=loaded_series.id)
                if series is None:
                    continue

                # すでに条目が確定している Series は、他ユーザーの收藏・検索・AI で上書きしない。
                if series.bangumi_subject_id is not None:
                    subject = next(
                        (subject for subject in subjects if int(subject['id']) == series.bangumi_subject_id),
                        None,
                    )
                    subject_id = series.bangumi_subject_id
                    if subject is None:
                        # 收藏に無くても既存 ID を正本とし、新規録画の episode 対応だけ進める。
                        matched_series_ids.add(series.id)
                        await cls._mapRecordedProgramEpisodes(
                            series_id = series.id,
                            subject_id = subject_id,
                            access_token = access_token,
                            episodes_by_subject_id = episodes_by_subject_id,
                        )
                        continue
                else:
                    subject = cls.findSubject(series.title, subjects)
                    # 收藏照合で一意に決まらない作品だけ、bgm.tv 検索結果を hints にした AI 照合へ回す。
                    if subject is None:
                        from app.metadata.KonomiTVBS4KBangumiSubjectSearch import (
                            KonomiTVBS4KBangumiSubjectSearch,
                        )
                        subject = await KonomiTVBS4KBangumiSubjectSearch.findSubjectForSeries(series, user)
                    if subject is None:
                        continue
                    subject_id = int(subject['id'])

                # 検索・AI の待機中に無効化された結果は新規 binding として保存しない。
                if IsBangumiExternalMetadataEnabled() is False:
                    break
                # 收藏一覧が返す SlimSubject を永続化すると同時に、同じ条目 ID に照合済みの Series を統合する。
                ## ロック中に外部 API は呼ばず、異なるユーザーの定期同期が重なっても subject ごとに直列化する。
                images = subject.get('images')
                image_url = str(images.get('large') or images.get('common') or '') if isinstance(images, dict) else ''
                subject_merge_lock = cls._subject_merge_locks.setdefault(subject_id, asyncio.Lock())
                async with subject_merge_lock:
                    try:
                        canonical_series = await SeriesMerger.mergeByBangumiSubject(
                            series_id = series.id,
                            subject_id = subject_id,
                            subject_name = str(subject.get('name', '')) or None,
                            subject_name_cn = str(subject.get('name_cn', '')) or None,
                            subject_summary = str(subject.get('short_summary', '')) or None,
                            subject_image_url = image_url or None,
                        )
                    except (IntegrityError, ValueError):
                        # 別プロセスが同じ subject を先に統合した場合は、一意索引の衝突または削除済み ID として観測される。
                        ## トランザクションのロールバック後に確定済みの主 Series を読み直し、外部 API を再試行しない。
                        canonical_series = await Series.get_or_none(bangumi_subject_id=subject_id)
                        if canonical_series is None:
                            raise
                matched_series_ids.add(canonical_series.id)
                await cls._mapRecordedProgramEpisodes(
                    series_id = canonical_series.id,
                    subject_id = subject_id,
                    access_token = access_token,
                    episodes_by_subject_id = episodes_by_subject_id,
                )
        return len(matched_series_ids)


    @classmethod
    async def _mapRecordedProgramEpisodes(
        cls,
        series_id: int,
        subject_id: int,
        access_token: str,
        episodes_by_subject_id: dict[int, list[dict[str, Any]]],
    ) -> None:
        """
        確定済み Bangumi 条目へ、Series 配下の録画の episode ID を結び付ける。

        Args:
            series_id (int): 対象 Series ID。
            subject_id (int): 上書きしない確定済み Bangumi 条目 ID。
            access_token (str): Bangumi 個人アクセストークン。
            episodes_by_subject_id (dict[int, list[dict[str, Any]]]): 条目ごとの episode 一覧キャッシュ。

        Returns:
            None
        """

        if IsBangumiExternalMetadataEnabled() is False:
            return
        recorded_programs = await RecordedProgram.filter(series_id=series_id).all()
        needs_episode_mapping = any(
            cls.parseEpisodeNumber(recorded_program.episode_number) is not None and (
                recorded_program.bangumi_subject_id != subject_id or
                recorded_program.bangumi_episode_id is None
            )
            for recorded_program in recorded_programs
        )
        if needs_episode_mapping and subject_id not in episodes_by_subject_id:
            episodes_by_subject_id[subject_id] = await cls._getEpisodes(subject_id, access_token)
        episodes = episodes_by_subject_id.get(subject_id, [])

        # Series 全体が同じ条目と確定したため、特別編や複数話録画にも subject ID までは保存する。
        ## 自然話数が一意な録画だけ、一覧取得時に確定した条目内の episode ID へ追加で結び付ける。
        for recorded_program in recorded_programs:
            # エピソード一覧の取得中や一括保存の途中でも、新規 bind の停止を反映する。
            if IsBangumiExternalMetadataEnabled() is False:
                break
            update_fields: list[str] = []
            subject_changed = recorded_program.bangumi_subject_id != subject_id
            if subject_changed:
                recorded_program.bangumi_subject_id = subject_id
                update_fields.append('bangumi_subject_id')

            episode_number = cls.parseEpisodeNumber(recorded_program.episode_number)
            if episode_number is not None and (subject_changed or recorded_program.bangumi_episode_id is None):
                episode = cls._findEpisode(episodes, episode_number)
                resolved_episode_id = int(episode['id']) if episode is not None else None
                if recorded_program.bangumi_episode_id != resolved_episode_id:
                    recorded_program.bangumi_episode_id = resolved_episode_id
                    update_fields.append('bangumi_episode_id')

            if update_fields:
                await recorded_program.save(update_fields=update_fields)


    @classmethod
    async def syncAllLinkedUsers(cls) -> None:
        """
        管理者1件の共有 Bangumi 連携があれば收藏一覧をローカル Series へ反映する。

        Returns:
            None
        """

        # None / TmdbOnly では新規の收藏同期を行わない。既存の bangumi_* は削除しない。
        if IsBangumiExternalMetadataEnabled() is False:
            logging.info(
                '[KonomiTVBS4KBangumiClient][syncAllLinkedUsers] Bangumi metadata source is disabled. '
                'Skipping collection sync.',
            )
            return
        owner = cls._sharedCollectionOwner()
        if owner is None:
            return
        try:
            matched_series_count = await cls.syncUserCollections(owner)
            logging.info(
                f'[KonomiTVBS4KBangumiClient][syncAllLinkedUsers] Synchronized shared Bangumi collections. '
                f'[matched_series: {matched_series_count}]',
            )
        except (httpx.HTTPError, ValueError, BangumiTokenDecryptionError) as ex:
            logging.error(
                '[KonomiTVBS4KBangumiClient][syncAllLinkedUsers] Failed to synchronize shared Bangumi collections.',
                exc_info = ex,
            )


    @classmethod
    def scheduleUserCollectionSync(cls, user: BangumiCollectionOwner | None = None) -> None:
        """
        共有アカウント連携直後の收藏一覧同期を API レスポンスと切り離して開始する。

        Args:
            user (BangumiCollectionOwner | None): 互換のため残す。未指定なら共有ストアを使う。

        Returns:
            None
        """

        del user

        # None / TmdbOnly では新しい收藏同期タスクを起動しない。
        if IsBangumiExternalMetadataEnabled() is False:
            return

        # 実行中の同期へ合流し、スキャンから外部 API を待たせない。
        if any(task.done() is False for task in cls._sync_tasks):
            cls._sync_rerun = True
            return

        async def Sync() -> None:
            while True:
                cls._sync_rerun = False
                try:
                    await cls.syncAllLinkedUsers()
                except (httpx.HTTPError, ValueError, BangumiTokenDecryptionError) as ex:
                    logging.error(
                        '[KonomiTVBS4KBangumiClient][scheduleUserCollectionSync] Failed to synchronize shared Bangumi collections.',
                        exc_info=ex,
                    )
                if cls._sync_rerun is False:
                    break

        # 実行中タスクへの強参照を保持し、完了時だけ集合から取り除く。
        task = asyncio.create_task(Sync())
        cls._sync_tasks.add(task)
        task.add_done_callback(cls._sync_tasks.discard)


    @staticmethod
    def _sharedCollectionOwner() -> _SharedCollectionOwner | None:
        """
        共有ストアから收藏同期用の認証情報を組み立てる。

        Returns:
            _SharedCollectionOwner | None: 未連携なら None。
        """

        profile = KonomiTVBS4KBangumiSharedStore.getProfile()
        access_token = KonomiTVBS4KBangumiSharedStore.decryptAccessToken()
        if profile['bangumi_user_name'] is None or access_token is None:
            return None
        return _SharedCollectionOwner(profile['bangumi_user_name'], access_token)


    @staticmethod
    def _findEpisode(episodes: list[dict[str, Any]], episode_number: int) -> dict[str, Any] | None:
        """
        Bangumi の対象条目から EPG 話数に対応する通常エピソードを取得する。

        Args:
            episodes (list[dict[str, Any]]): Bangumi 条目のエピソード。
            episode_number (int): EPG から抽出した話数。

        Returns:
            dict[str, Any] | None: ep または通し番号 sort + 1 が一致する通常エピソード。
        """

        # 分割クールでは ep が 1 に戻る一方、sort が前クールから続く場合がある
        matched_episodes = [
            episode for episode in episodes
            if int(episode.get('type', -1)) == 0 and (
                float(episode.get('ep', -1)) == episode_number or
                float(episode.get('sort', -2)) + 1 == episode_number
            )
        ]
        if len(matched_episodes) != 1:
            return None
        return matched_episodes[0]


    @classmethod
    async def bindRecordedProgramById(cls, recorded_program_id: int) -> None:
        """
        話数確定後の録画へ、所属 Series の確定条目から episode ID を結び付ける。

        Args:
            recorded_program_id (int): 話数が確定した録画番組 ID。

        Returns:
            None
        """

        # None / TmdbOnly では新規の episode bind を行わない。既存の bind はそのまま残す。
        if IsBangumiExternalMetadataEnabled() is False:
            return
        recorded_program = await RecordedProgram.get_or_none(id=recorded_program_id)
        if recorded_program is None or recorded_program.series_id is None:
            return
        series = await Series.get_or_none(id=recorded_program.series_id)
        if series is None or series.bangumi_subject_id is None:
            return
        # 録画の subject が現在 Series と違うときは旧 episode を捨てて再照合する。
        if recorded_program.bangumi_subject_id != series.bangumi_subject_id:
            if IsBangumiExternalMetadataEnabled() is False:
                return
            recorded_program.bangumi_subject_id = series.bangumi_subject_id
            recorded_program.bangumi_episode_id = None
            await recorded_program.save(update_fields=['bangumi_subject_id', 'bangumi_episode_id'])
        episode_number = cls.parseEpisodeNumber(recorded_program.episode_number)
        if episode_number is None:
            return

        bangumi_subject_id = recorded_program.bangumi_subject_id
        if bangumi_subject_id is None:
            return
        access_token = KonomiTVBS4KBangumiSharedStore.decryptAccessToken()
        if access_token is None:
            return
        try:
            episodes = await cls._getEpisodes(bangumi_subject_id, access_token)
        except httpx.HTTPError as ex:
            logging.error(
                f'[KonomiTVBS4KBangumiClient][bindRecordedProgramById] Failed to bind Bangumi episode. '
                f'[recorded_program_id: {recorded_program_id}]',
                exc_info = ex,
            )
            return
        episode = cls._findEpisode(episodes, episode_number)
        if episode is None:
            return
        resolved_episode_id = int(episode['id'])
        # 既存 episode は現在話数と一致するときだけ再利用する。
        if recorded_program.bangumi_episode_id == resolved_episode_id:
            return
        if IsBangumiExternalMetadataEnabled() is False:
            return
        recorded_program.bangumi_episode_id = resolved_episode_id
        await recorded_program.save(update_fields=['bangumi_episode_id'])
