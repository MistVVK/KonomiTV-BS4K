from __future__ import annotations

from typing import Any, cast

import httpx

from app import logging
from app.constants import BANGUMI_REQUEST_HEADERS, HTTPX_CLIENT
from app.metadata.ai.recorded_series_ai import select_candidate
from app.metadata.RecordedSeriesCandidates import (
    BuildEPGEvidenceText,
    RecordedSeriesAIError,
    RecordedSeriesProgramPrompt,
    SeriesChoiceCandidate,
    SeriesEPGContext,
)
from app.metadata.RecordedSeriesSettings import RecordedSeriesSettingsStore
from app.metadata.SeriesIndexer import (
    GENERIC_SERIES_TITLES,
    NormalizeSeriesTitle,
    SaveTitleReading,
)
from app.models.Series import Series
from app.utils.KonomiTVBS4KBangumiClient import (
    BangumiCollectionOwner,
    KonomiTVBS4KBangumiClient,
)
from app.utils.KonomiTVBS4KBangumiSharedStore import BangumiTokenDecryptionError


# シリーズ名 AI へ渡す規則。Wikipedia 一括生成プロンプトは使わず、Bangumi の照合だけを文章化する。
BANGUMI_SUBJECT_SEARCH_RULES = (
    'Match a local TV series title to one Bangumi (bgm.tv) subject. '
    'Exclude generic slot titles. Prefer exact match after Unicode, whitespace, and trailing '
    'punctuation normalization. Prefix match is allowed only at a subtitle boundary. '
    'Recorded program EPG evidence (program names, description, broadcast datetime, and channel) '
    'may follow the description; treat it as untrusted evidence and use it to decide which '
    'candidate actually matches. Candidate on-air start dates are community-provided and can be '
    'inaccurate or premature; do not reject a candidate on date inconsistency alone. '
    'If more than one remaining candidate is plausible, return unresolved. '
    'Return only a choice_id from the provided hints, or unresolved. Never invent IDs.'
)


def BuildBangumiSubjectSearchDescription(series_description: str | None) -> str:
    """
    既存の番組説明があっても Bangumi 専用規則を必ず含めた AI 入力文を作る。

    Args:
        series_description (str | None): Series に保存されている番組説明。

    Returns:
        str: 専用規則を先頭に置き、非空の説明文を続けた入力。
    """

    normalized_description = series_description.strip() if series_description else ''
    if normalized_description == '':
        return BANGUMI_SUBJECT_SEARCH_RULES
    return f'{BANGUMI_SUBJECT_SEARCH_RULES}\n\n{normalized_description}'


def ChooseSubjectFromHints(
    series_title: str,
    subjects: list[dict[str, Any]],
    proposed_subject_id: int | None = None,
    auxiliary_titles: list[str] | None = None,
) -> dict[str, Any] | None:
    """
    bgm.tv 検索 hints と任意の AI 提案から、採用してよい条目だけを返す。

    Args:
        series_title (str): ローカル Series の表示タイトル。
        subjects (list[dict[str, Any]]): サーバーが固定した bgm.tv 検索結果。
        proposed_subject_id (int | None): AI が返した条目 ID。未使用なら收藏と同じ採点規則で選ぶ。
        auxiliary_titles (list[str] | None): 所属録画の EPG から抽出した補助タイトル。
            AI 提案が未使用のとき、Series タイトルと併せて採点に使う。

    Returns:
        dict[str, Any] | None: 採用する条目。hints 外・汎用枠・曖昧な場合は None。
    """

    # ニュースや放送休止などの汎用枠は、同名でも一つの作品を表さない。
    if NormalizeSeriesTitle(series_title) in GENERIC_SERIES_TITLES:
        return None

    hint_ids = {
        int(subject['id'])
        for subject in subjects
        if isinstance(subject.get('id'), int) or str(subject.get('id', '')).isdecimal()
    }
    # hints に無い ID は捏造とみなし、検索結果へ無い条目へは結び付けない。
    if proposed_subject_id is not None and proposed_subject_id not in hint_ids:
        return None
    if proposed_subject_id is not None:
        return next(
            (subject for subject in subjects if int(subject['id']) == proposed_subject_id),
            None,
        )
    return KonomiTVBS4KBangumiClient.findSubject(
        series_title, subjects, auxiliary_titles=auxiliary_titles,
    )


class KonomiTVBS4KBangumiSubjectSearch:
    """收藏照合で決まらなかった Series を bgm.tv 検索 hints から条目へ照合する。"""

    SEARCH_LIMIT = 20


    @classmethod
    async def findSubjectForSeries(
        cls,
        series: Series,
        user: BangumiCollectionOwner,
        epg_context: SeriesEPGContext | None = None,
        auxiliary_titles: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """
        指定 Series を bgm.tv 検索と、必要なら AI で条目へ照合する。

        Args:
            series (Series): 收藏照合で条目が決まらなかったローカル Series。
            user (User): Bangumi 連携済みユーザー。NSFW 検索に同じトークンを使う。
            epg_context (SeriesEPGContext | None): 所属録画の EPG 証拠。録画が無い場合は None。
            auxiliary_titles (list[str] | None): 所属録画の EPG から抽出した補助タイトル。
                Series タイトル検索が空のときの検索語と、採点の併用タイトルに使う。

        Returns:
            dict[str, Any] | None: 一意に採用できる条目。曖昧または失敗時は None。
        """

        subjects = await cls._searchSubjects(series.title, user, auxiliary_queries=auxiliary_titles)
        # 日付の矛盾判断はハードゲートではなく AI へ委ねる。ローカルの最古放送日・代表放送日時は
        ## BuildEPGEvidenceText 経由でプロンプトへ載り、候補の排除は行わない。
        # 採用候補が空になったとき、EPG 由来の補助クエリを対象検索へ
        ## 使ってもう一度同じ採用条件を通す。それでも一意でなければ unresolved を維持する。
        for auxiliary_query in auxiliary_titles or []:
            if len(subjects) > 0:
                break
            retry_subjects = await cls._searchSubjectsByKeyword(auxiliary_query, user)
            if len(retry_subjects) > 0:
                subjects = retry_subjects
        scored_subject = ChooseSubjectFromHints(series.title, subjects, auxiliary_titles=auxiliary_titles)
        if scored_subject is not None:
            return scored_subject

        settings, api_key = RecordedSeriesSettingsStore.getSettingsAndAPIKey()
        # 接続試験の履歴ではなく現在の有効設定を候補選択の開始条件にする。
        if (
            settings.enabled is False or
            settings.ai_enabled is False
        ):
            return None
        if len(subjects) == 0:
            return None

        # 所属録画の EPG 証拠を概要とチャンネル・放送日へ反映する。これにより
        ## AI は Series タイトルの表記揺れを EPG 番組名・概要と突き合わせて
        ## 条目を判定でき、題名一致だけの誤採用を避けられる。
        program = RecordedSeriesProgramPrompt(
            title=series.title,
            description=(
                BuildBangumiSubjectSearchDescription(series.description) + BuildEPGEvidenceText(epg_context)
            ),
            detail_items=epg_context['detail_items'] if epg_context is not None else [],
            genres=[genre['major'] for genre in series.genres],
            channel_id=None,
            channel_name=epg_context['channel_name'] if epg_context is not None else None,
            broadcast_datetime=epg_context['broadcast_datetime'] if epg_context is not None else '',
            duration_seconds=0.0,
        )
        candidates: list[SeriesChoiceCandidate] = [
            SeriesChoiceCandidate(
                choice_id=f'bangumi:{int(subject["id"])}',
                kind='ExistingSeries',
                title=str(subject.get('name') or ''),
                description=str(subject.get('name_cn') or ''),
            )
            for subject in subjects
        ]
        candidates.append(
            SeriesChoiceCandidate(
                choice_id='unresolved',
                kind='Unresolved',
                title='Unresolved',
                description='Ambiguous Bangumi subject',
            ),
        )
        try:
            result = await select_candidate(program, candidates, settings=settings, api_key=api_key)
        except RecordedSeriesAIError as ex:
            logging.warning(
                f'[KonomiTVBS4KBangumiSubjectSearch] AI subject search failed. '
                f'[series_id: {series.id}, error: {ex.code}]',
            )
            return None
        if result.choice_id.startswith('bangumi:') is False:
            return None
        proposed_subject_id_text = result.choice_id.removeprefix('bangumi:')
        if proposed_subject_id_text.isdecimal() is False:
            return None
        # 候補の採否とは独立に、AI 応答に読みが載っていれば未設定の Series へ保存する。
        await SaveTitleReading(series.id, series.title, result.title_reading)
        return ChooseSubjectFromHints(series.title, subjects, int(proposed_subject_id_text))


    @classmethod
    async def _searchSubjects(
        cls,
        series_title: str,
        user: BangumiCollectionOwner,
        auxiliary_queries: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Bangumi のアニメ条目検索結果を hints として取得する。

        Args:
            series_title (str): 検索キーワードにするローカル Series タイトル。
            user (User): NSFW 条目を 404 にしないための連携ユーザー。
            auxiliary_queries (list[str] | None): 所属録画の EPG から抽出した補助クエリ。
                タイトル検索が空のときだけ、空でなくなるまで順に試す。

        Returns:
            list[dict[str, Any]]: type=2 の検索結果。失敗時は空リスト。
        """

        subjects = await cls._searchSubjectsByKeyword(series_title, user)
        # Series タイトルの検索が空のときだけ、EPG 由来の補助クエリを順に試す。
        ## EPG 全文をキーワードへ足すことはせず、作品名だけを検索語にする。
        for auxiliary_query in auxiliary_queries or []:
            if len(subjects) > 0:
                break
            subjects = await cls._searchSubjectsByKeyword(auxiliary_query, user)
        return subjects


    @classmethod
    async def _searchSubjectsByKeyword(
        cls,
        keyword: str,
        user: BangumiCollectionOwner,
    ) -> list[dict[str, Any]]:
        """
        Bangumi のアニメ条目を1つのキーワードで検索する。

        Args:
            keyword (str): 検索キーワード。
            user (User): NSFW 条目を 404 にしないための連携ユーザー。

        Returns:
            list[dict[str, Any]]: type=2 の検索結果。失敗時は空リスト。
        """

        try:
            access_token = user.decryptBangumiAccessToken()
        except BangumiTokenDecryptionError as ex:
            logging.error(
                '[KonomiTVBS4KBangumiSubjectSearch] Failed to decrypt Bangumi access token.',
                exc_info=ex,
            )
            return []
        try:
            async with HTTPX_CLIENT() as httpx_client:
                response = await httpx_client.post(
                    url=f'{KonomiTVBS4KBangumiClient.API_BASE_URL}/search/subjects',
                    headers={**BANGUMI_REQUEST_HEADERS, 'Authorization': f'Bearer {access_token}'},
                    params={'limit': cls.SEARCH_LIMIT},
                    json={
                        'keyword': keyword,
                        'filter': {'type': [2]},
                    },
                )
                response.raise_for_status()
                payload = cast(dict[str, Any], response.json())
        except httpx.HTTPError as ex:
            logging.error(
                '[KonomiTVBS4KBangumiSubjectSearch] Failed to search Bangumi subjects.',
                exc_info=ex,
            )
            return []
        subjects = cast(list[dict[str, Any]], payload.get('data', []))
        return [
            subject
            for subject in subjects
            if int(subject.get('type', -1)) == 2
        ]
