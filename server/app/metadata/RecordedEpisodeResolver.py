from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, cast

from tortoise import transactions
from tortoise.backends.base.client import BaseDBAsyncClient

from app import logging
from app.constants import JST
from app.metadata.RecordedSeriesGeneration import AISeriesMetadataResult
from app.models.RecordedEpisode import RecordedEpisodeResolution, SeriesEpisode
from app.models.RecordedProgram import RecordedProgram


_DECIMAL_TEXT_PATTERN = r'[0-9]{1,7}(?:\.[0-9]{1,3})?'
_JAPANESE_INTEGER_PATTERN = r'[0-9一二三四五六七八九十百千]+'
_MAX_SEASON_NUMBER = 2_147_483_647
_SEASON_EPISODE_PATTERN = re.compile(
    rf'(?i:Season)\s*(?P<season>{_JAPANESE_INTEGER_PATTERN})\s*'
    rf'(?:#\s*(?P<decimal>{_DECIMAL_TEXT_PATTERN})|'
    rf'第\s*(?P<japanese>{_JAPANESE_INTEGER_PATTERN})\s*(?:話|回|夜|週|巻|章|幕|局|戦))',
)
_HASH_EPISODE_PATTERN = re.compile(rf'#\s*(?P<decimal>{_DECIMAL_TEXT_PATTERN})')
_JAPANESE_EPISODE_PATTERN = re.compile(
    rf'第\s*(?P<japanese>{_JAPANESE_INTEGER_PATTERN})\s*(?:話|回|夜|週|巻|章|幕|局|戦)',
)
_PLAIN_EPISODE_PATTERN = re.compile(rf'(?P<decimal>{_DECIMAL_TEXT_PATTERN})')


class RecordedEpisodeProgramNotFoundError(Exception):
    """指定された再生可能な録画が存在しない。"""


class RecordedEpisodeSeriesNotAssignedError(Exception):
    """話数を設定する録画がSeriesへ所属していない。"""


class RecordedEpisodeAssignmentStaleError(Exception):
    """読み込み後にSeriesまたはEpisodeの割当が更新された。"""


class RecordedEpisodeCrossSeriesError(Exception):
    """別SeriesのEpisodeを録画へ割り当てようとした。"""


class RecordedEpisodeTargetNotFoundError(Exception):
    """指定されたSeriesEpisodeが存在しない。"""


class RecordedEpisodeInvalidNumberError(ValueError):
    """シーズン・話数がDBで表現できる範囲にない。"""


@dataclass(frozen=True, slots=True)
class ParsedEpisodeNumber:
    """厳格な既存表記解析で得たシーズン・話数。"""

    season_number: int
    episode_number: Decimal


def _parseJapaneseInteger(value: str) -> int | None:
    """数字または一万未満の一般的な漢数字を曖昧性なく整数へ変換する。

    Args:
        value: NFKC正規化済みの整数表記。

    Returns:
        厳格に変換できた非負整数。異常な単位順や数字の連続はNone。
    """

    if re.fullmatch(r'[0-9]+', value) is not None:
        return int(value)
    if re.fullmatch(r'[一二三四五六七八九十百千]+', value) is None:
        return None

    digit_values = {
        '一': 1,
        '二': 2,
        '三': 3,
        '四': 4,
        '五': 5,
        '六': 6,
        '七': 7,
        '八': 8,
        '九': 9,
    }
    unit_values = {'十': 10, '百': 100, '千': 1000}
    total = 0
    pending_digit: int | None = None
    previous_unit = 10000
    for character in value:
        if character in digit_values:
            # 「二三」のような位取りのない連続漢数字は解釈を推測しない。
            if pending_digit is not None:
                return None
            pending_digit = digit_values[character]
            continue

        unit = unit_values[character]
        # 「十百」や「百百」のような単位の逆転・重複は誤入力として拒否する。
        if unit >= previous_unit:
            return None
        total += (pending_digit if pending_digit is not None else 1) * unit
        pending_digit = None
        previous_unit = unit
    if pending_digit is not None:
        total += pending_digit
    return total


def _parseEpisodeDecimal(value: str) -> Decimal | None:
    """DBの固定精度内に収まる非負話数だけをDecimalへ変換する。

    Args:
        value: NFKC正規化済みの数値文字列。

    Returns:
        変換済みDecimal。不正または範囲外ならNone。
    """

    if re.fullmatch(_DECIMAL_TEXT_PATTERN, value) is None:
        return None
    try:
        episode_number = Decimal(value)
    except InvalidOperation:
        return None
    if episode_number < 0:
        return None
    return episode_number


def ParseLegacyEpisodeNumber(value: str | None) -> ParsedEpisodeNumber | None:
    """既存episode_numberを完全一致で構造化し、複合・範囲表記を拒否する。

    Args:
        value: RecordedProgramに保存済みの旧話数文字列。

    Returns:
        一意に解釈できるシーズン・話数。不明または曖昧ならNone。
    """

    if value is None:
        return None
    normalized = unicodedata.normalize('NFKC', value).strip()
    if normalized == '':
        return None

    season_match = _SEASON_EPISODE_PATTERN.fullmatch(normalized)
    if season_match is not None:
        season_number = _parseJapaneseInteger(season_match.group('season'))
        if season_number is None or season_number > _MAX_SEASON_NUMBER:
            return None
        if season_match.group('decimal') is not None:
            episode_number = _parseEpisodeDecimal(season_match.group('decimal'))
        else:
            japanese_episode = _parseJapaneseInteger(season_match.group('japanese'))
            episode_number = Decimal(japanese_episode) if japanese_episode is not None else None
        if episode_number is None:
            return None
        return ParsedEpisodeNumber(season_number=season_number, episode_number=episode_number)

    hash_match = _HASH_EPISODE_PATTERN.fullmatch(normalized)
    plain_match = _PLAIN_EPISODE_PATTERN.fullmatch(normalized)
    decimal_match = hash_match or plain_match
    if decimal_match is not None:
        episode_number = _parseEpisodeDecimal(decimal_match.group('decimal'))
        if episode_number is not None:
            return ParsedEpisodeNumber(season_number=1, episode_number=episode_number)

    japanese_match = _JAPANESE_EPISODE_PATTERN.fullmatch(normalized)
    if japanese_match is not None:
        japanese_episode = _parseJapaneseInteger(japanese_match.group('japanese'))
        if japanese_episode is not None:
            return ParsedEpisodeNumber(season_number=1, episode_number=Decimal(japanese_episode))
    return None


def FormatEpisodeNumber(season_number: int, episode_number: Decimal) -> str:
    """構造化話数を旧クライアント向けの標準文字列へ変換する。

    Args:
        season_number: 0以上のシーズン番号。
        episode_number: 固定精度内の非負話数。

    Returns:
        Season 1を省略した #N 形式の文字列。

    Raises:
        RecordedEpisodeInvalidNumberError: 値がDBの許容範囲外の場合。
    """

    if season_number < 0 or season_number > _MAX_SEASON_NUMBER:
        raise RecordedEpisodeInvalidNumberError
    normalized_episode = _parseEpisodeDecimal(format(episode_number, 'f'))
    if normalized_episode is None:
        raise RecordedEpisodeInvalidNumberError
    episode_text = format(normalized_episode, 'f')
    # 整数末尾の0は桁そのものなので、小数点以下が存在するときだけ余分な0を除く。
    if '.' in episode_text:
        episode_text = episode_text.rstrip('0').rstrip('.')
    if episode_text == '':
        episode_text = '0'
    if season_number == 1:
        return f'#{episode_text}'
    return f'Season {season_number} #{episode_text}'


class RecordedEpisodeResolver:
    """構造化Episodeの決定論的移行・手動割当・Series移動を一元管理する。"""

    @classmethod
    async def _getOrCreateEpisode(
        cls,
        *,
        series_id: int,
        season_number: int,
        episode_number: Decimal,
        connection: BaseDBAsyncClient,
    ) -> SeriesEpisode:
        """同一Seriesの同一話を複数録画から共有できるようupsertする。

        Args:
            series_id: 所属Series ID。
            season_number: 0以上のシーズン番号。
            episode_number: 固定精度内の非負話数。
            connection: 呼び出し元トランザクション。

        Returns:
            既存または新規のSeriesEpisode。

        Raises:
            RecordedEpisodeInvalidNumberError: 数値がDBの許容範囲外の場合。
        """

        # API以外の統合フックから呼ばれても、不正値をDBへ到達させない。
        FormatEpisodeNumber(season_number, episode_number)
        episode = await SeriesEpisode.filter(
            series_id=series_id,
            season_number=season_number,
            episode_number=episode_number,
        ).using_db(connection).first()
        # Tortoiseのget_or_create()は内部で別transactionを開始するため、SQLiteの呼び出し元
        # transaction内では使わず、同じconnection上で検索と作成を完結させる。
        if episode is None:
            episode = await SeriesEpisode.create(
                series_id=series_id,
                season_number=season_number,
                episode_number=episode_number,
                using_db=connection,
            )
        return episode

    @classmethod
    async def _backfillLegacyEpisode(cls, resolution_id: int) -> tuple[int, int]:
        """既存録画1件を独立transactionで構造化し、集計加算値を返す。"""

        async with transactions.in_transaction() as connection:
            resolution = await RecordedEpisodeResolution.filter(id=resolution_id) \
                .select_for_update() \
                .using_db(connection) \
                .first()
            # 再実行時、すでに別処理が確定した行には触れない。
            if resolution is None or resolution.status != 'Pending' or resolution.is_legacy_recording is False:
                return 0, 0
            recorded_program = await RecordedProgram.filter(id=resolution.recorded_program_id) \
                .select_for_update() \
                .using_db(connection) \
                .first()
            if recorded_program is None or recorded_program.series_id is None:
                resolution.status = 'NeedsReview'
                resolution.error_code = 'ProgramNotInSeries'
                resolution.resolved_at = datetime.now(tz=JST)
                await resolution.save(
                    update_fields=['status', 'error_code', 'resolved_at', 'updated_at'],
                    using_db=connection,
                )
                return 0, 1

            # 再実行前に手動割当済みなら、そのEpisodeをmigration結果として追認する。
            if recorded_program.series_episode_id is not None:
                assigned_episode = await SeriesEpisode.filter(
                    id=recorded_program.series_episode_id,
                    series_id=recorded_program.series_id,
                ).using_db(connection).first()
                if assigned_episode is not None:
                    resolution.status = 'Resolved'
                    resolution.episode_id = assigned_episode.id
                    resolution.season_number = assigned_episode.season_number
                    resolution.error_code = None
                    resolution.resolved_at = datetime.now(tz=JST)
                    await resolution.save(
                        update_fields=[
                            'status',
                            'episode_id',
                            'season_number',
                            'error_code',
                            'resolved_at',
                            'updated_at',
                        ],
                        using_db=connection,
                    )
                    return 1, 0

            parsed = ParseLegacyEpisodeNumber(recorded_program.episode_number)
            if parsed is None:
                resolution.status = 'NeedsReview'
                resolution.error_code = (
                    'LegacyEpisodeMissing'
                    if recorded_program.episode_number is None
                    else 'LegacyEpisodeUnknown'
                )
                resolution.resolved_at = datetime.now(tz=JST)
                await resolution.save(
                    update_fields=['status', 'error_code', 'resolved_at', 'updated_at'],
                    using_db=connection,
                )
                return 0, 1

            episode = await cls._getOrCreateEpisode(
                series_id=recorded_program.series_id,
                season_number=parsed.season_number,
                episode_number=parsed.episode_number,
                connection=connection,
            )
            # 旧episode_number文字列は互換性と移行監査のため書き換えない。
            recorded_program.series_episode_id = episode.id
            await recorded_program.save(
                update_fields=['series_episode_id', 'updated_at'],
                using_db=connection,
            )
            resolution.status = 'Resolved'
            resolution.episode_id = episode.id
            resolution.season_number = episode.season_number
            resolution.error_code = None
            resolution.resolved_at = datetime.now(tz=JST)
            await resolution.save(
                update_fields=[
                    'status',
                    'episode_id',
                    'season_number',
                    'error_code',
                    'resolved_at',
                    'updated_at',
                ],
                using_db=connection,
            )
            return 1, 0

    @classmethod
    async def backfillLegacyEpisodes(cls) -> tuple[int, int]:
        """migrationがseedした既存録画だけを無通信・冪等に構造化する。

        Returns:
            (構造化できた件数, 要確認として確定した件数)。
        """

        resolved_count = 0
        review_count = 0
        pending_ids = cast(list[int], await RecordedEpisodeResolution.filter(
            status='Pending',
            is_legacy_recording=True,
            recorded_program__series_id__not_isnull=True,
            recorded_program__recorded_video__status='Recorded',
        ).order_by('id').values_list('id', flat=True))
        for resolution_id in pending_ids:
            try:
                resolved_increment, review_increment = await cls._backfillLegacyEpisode(resolution_id)
                resolved_count += resolved_increment
                review_count += review_increment
            except Exception as ex:
                # 1件の異常で後続録画のmigrationを毎起動ブロックしない。
                logging.error(
                    f'[RecordedEpisodeResolver] Legacy episode backfill item failed. resolution_id: {resolution_id}',
                    exc_info=ex,
                )
                now = datetime.now(tz=JST)
                updated_count = await RecordedEpisodeResolution.filter(
                    id=resolution_id,
                    status='Pending',
                    is_legacy_recording=True,
                ).update(
                    status='NeedsReview',
                    source='Migration',
                    error_code='LegacyBackfillFailed',
                    error_message=None,
                    resolved_at=now,
                    updated_at=now,
                )
                review_count += int(updated_count > 0)
        return resolved_count, review_count

    @classmethod
    async def assignProgramEpisode(
        cls,
        recorded_program_id: int,
        *,
        expected_series_id: int,
        expected_series_episode_id: int | None,
        decision: str,
        episode_id: int | None = None,
        season_number: int | None = None,
        episode_number: Decimal | None = None,
    ) -> None:
        """管理者の話数判断を楽観ロック付きで録画とResolutionへ反映する。

        Args:
            recorded_program_id: 更新対象のRecordedProgram ID。
            expected_series_id: 編集画面を開いた時点のSeries ID。
            expected_series_episode_id: 編集画面を開いた時点のEpisode ID。
            decision: ExistingEpisode、StructuredEpisode、NoPublishedNumber、
                NotNumbered、Unknown、AdoptAIのいずれか。
            episode_id: ExistingEpisode時のSeriesEpisode ID。
            season_number: StructuredEpisode時のシーズン番号。
            episode_number: StructuredEpisode時の話数。

        Returns:
            None

        Raises:
            RecordedEpisodeProgramNotFoundError: 再生可能な録画が存在しない場合。
            RecordedEpisodeSeriesNotAssignedError: Series未所属の場合。
            RecordedEpisodeAssignmentStaleError: 編集開始後に割当が変わった場合。
            RecordedEpisodeTargetNotFoundError: 指定Episodeが存在しない場合。
            RecordedEpisodeCrossSeriesError: 別SeriesのEpisodeが指定された場合。
            RecordedEpisodeInvalidNumberError: 数値またはdecision引数が不正な場合。
        """

        async with transactions.in_transaction() as connection:
            recorded_program = await RecordedProgram.filter(
                id=recorded_program_id,
                recorded_video__status='Recorded',
            ).select_for_update().using_db(connection).first()
            if recorded_program is None:
                raise RecordedEpisodeProgramNotFoundError
            if recorded_program.series_id is None:
                raise RecordedEpisodeSeriesNotAssignedError
            if (
                recorded_program.series_id != expected_series_id or
                recorded_program.series_episode_id != expected_series_episode_id
            ):
                raise RecordedEpisodeAssignmentStaleError

            resolution = (
                await RecordedEpisodeResolution.filter(
                    recorded_program_id=recorded_program.id,
                )
                .select_for_update()
                .using_db(connection)
                .first()
            )

            selected_episode: SeriesEpisode | None
            selected_season_number: int | None
            resolution_status: Literal[
                'Resolved',
                'Unknown',
                'NotNumbered',
                'NoPublishedNumber',
            ]
            adopted_source: Literal['Manual', 'WebSearch']
            if decision == 'ExistingEpisode':
                if episode_id is None or season_number is not None or episode_number is not None:
                    raise RecordedEpisodeInvalidNumberError
                selected_episode = await SeriesEpisode.filter(id=episode_id).using_db(connection).first()
                if selected_episode is None:
                    raise RecordedEpisodeTargetNotFoundError
                if selected_episode.series_id != recorded_program.series_id:
                    raise RecordedEpisodeCrossSeriesError
                selected_season_number = selected_episode.season_number
                resolution_status = 'Resolved'
                adopted_source = 'Manual'
            elif decision == 'StructuredEpisode':
                if episode_id is not None or season_number is None or episode_number is None:
                    raise RecordedEpisodeInvalidNumberError
                selected_episode = await cls._getOrCreateEpisode(
                    series_id=recorded_program.series_id,
                    season_number=season_number,
                    episode_number=episode_number,
                    connection=connection,
                )
                selected_season_number = selected_episode.season_number
                resolution_status = 'Resolved'
                adopted_source = 'Manual'
            elif decision in {'NoPublishedNumber', 'NotNumbered'}:
                if episode_id is not None or episode_number is not None:
                    raise RecordedEpisodeInvalidNumberError
                if season_number is not None and (
                    season_number < 0 or season_number > _MAX_SEASON_NUMBER
                ):
                    raise RecordedEpisodeInvalidNumberError
                selected_episode = None
                selected_season_number = season_number
                resolution_status = cast(
                    Literal['NoPublishedNumber', 'NotNumbered'],
                    decision,
                )
                adopted_source = 'Manual'
            elif decision == 'Unknown':
                if episode_id is not None or season_number is not None or episode_number is not None:
                    raise RecordedEpisodeInvalidNumberError
                selected_episode = None
                selected_season_number = None
                resolution_status = 'Unknown'
                adopted_source = 'Manual'
            elif decision == 'AdoptAI':
                # AI レーンに保存済みの提案だけを正本へ採用する（再検索はしない）。
                if (
                    episode_id is not None
                    or season_number is not None
                    or episode_number is not None
                ):
                    raise RecordedEpisodeInvalidNumberError
                if resolution is None:
                    raise RecordedEpisodeInvalidNumberError
                if resolution.proposed_outcome in {'NotNumbered', 'NoPublishedNumber'}:
                    selected_episode = None
                    selected_season_number = resolution.proposed_season_number
                    resolution_status = cast(
                        Literal['NotNumbered', 'NoPublishedNumber'],
                        resolution.proposed_outcome,
                    )
                elif (
                    resolution.proposed_outcome == 'Resolved'
                    and
                    resolution.proposed_season_number is not None
                    and resolution.proposed_episode_number is not None
                ):
                    selected_episode = await cls._getOrCreateEpisode(
                        series_id=recorded_program.series_id,
                        season_number=resolution.proposed_season_number,
                        episode_number=resolution.proposed_episode_number,
                        connection=connection,
                    )
                    selected_season_number = selected_episode.season_number
                    resolution_status = 'Resolved'
                else:
                    raise RecordedEpisodeInvalidNumberError
                adopted_source = 'WebSearch'
            else:
                raise RecordedEpisodeInvalidNumberError

            recorded_program.series_episode_id = (
                selected_episode.id if selected_episode is not None else None
            )
            if selected_episode is None:
                recorded_program.episode_number = None
            else:
                recorded_program.episode_number = (
                    FormatEpisodeNumber(
                        selected_episode.season_number, selected_episode.episode_number
                    )
                    if selected_episode is not None
                    else None
                )
            await recorded_program.save(
                update_fields=['series_episode_id', 'episode_number', 'updated_at'],
                using_db=connection,
            )
            selected_episode_id = (
                selected_episode.id if selected_episode is not None else None
            )
            resolved_at = datetime.now(tz=JST)
            if resolution is None:
                await RecordedEpisodeResolution.create(
                    recorded_program_id=recorded_program.id,
                    episode_id=selected_episode_id,
                    season_number=selected_season_number,
                    status=resolution_status,
                    source=adopted_source,
                    manual_episode_id=selected_episode_id
                    if adopted_source == 'Manual'
                    else None,
                    manual_season_number=(
                        selected_season_number
                        if adopted_source == 'Manual'
                        else None
                    ),
                    manual_episode_number=(
                        selected_episode.episode_number
                        if adopted_source == 'Manual' and selected_episode is not None
                        else None
                    ),
                    manual_status=resolution_status
                    if adopted_source == 'Manual'
                    else None,
                    error_code=None,
                    error_message=None,
                    resolved_at=resolved_at,
                    using_db=connection,
                )
            else:
                resolution.episode_id = selected_episode_id
                resolution.season_number = selected_season_number
                resolution.status = resolution_status
                resolution.source = adopted_source
                # 手動確定では AI レーン（proposed / citations 等）を消さない。
                # AI 採用では lookup 監査を残し、手動レーンも消さない。
                if adopted_source == 'Manual':
                    # 外部検索中に Manual が勝った場合、正本側に in-flight 表示だけを
                    # 残さない。課金監査は Automation が応答後に Rejected へ閉じる。
                    if resolution.lookup_outcome == 'Pending':
                        resolution.lookup_outcome = None
                    resolution.manual_episode_id = selected_episode_id
                    resolution.manual_season_number = selected_season_number
                    resolution.manual_episode_number = (
                        selected_episode.episode_number
                        if selected_episode is not None
                        else None
                    )
                    resolution.manual_status = resolution_status
                resolution.error_code = None
                resolution.error_message = None
                resolution.resolved_at = resolved_at
                await resolution.save(
                    update_fields=[
                        'episode_id',
                        'season_number',
                        'status',
                        'source',
                        'lookup_outcome',
                        'manual_episode_id',
                        'manual_season_number',
                        'manual_episode_number',
                        'manual_status',
                        'error_code',
                        'error_message',
                        'resolved_at',
                        'updated_at',
                    ],
                    using_db=connection,
                )

    @classmethod
    async def applyGeneratedMetadata(
        cls,
        recorded_program: RecordedProgram,
        *,
        target_series_id: int,
        generated: AISeriesMetadataResult,
        input_fingerprint: str,
        connection: BaseDBAsyncClient,
        local_episode_fallback: ParsedEpisodeNumber | None = None,
    ) -> bool:
        """シリーズ生成と同じ transaction で AI 話数を正本へ反映する。

        Args:
            recorded_program: Series 割当を更新中の録画。
            target_series_id: サーバーすり合わせ後に確定した Series ID。
            generated: strict schema 検証済みの一括生成結果。
            input_fingerprint: 生成へ送った録画入力の fingerprint。
            connection: Series 更新と共有する transaction。
            local_episode_fallback: AI が話数を返さない場合に限り採用できる厳格なローカル解析結果。

        Returns:
            AI の話名反映を許可する場合は True。Manual Episode を保護した場合は False。
        """

        resolution = await RecordedEpisodeResolution.filter(
            recorded_program_id=recorded_program.id,
        ).using_db(connection).first()
        # Series の再判定を強制しても、管理者が確定した Episode レーンは上書きしない。
        if resolution is not None and resolution.source == 'Manual':
            return False

        # null は「話数なし」の明示ではなく、モデルが話数を生成できなかった欠損値として扱う。
        # 既に同じ Series で確定した話数がある場合は、出典を問わず欠損値で正本を消さない。
        # Series 変更時に無効となる WebSearch / EPG / AI は synchronizeSeriesAssignment() が先に除去する。
        # 明示的な NotNumbered はこの保護に入れず、後続処理で話数なしへ更新する。
        has_deterministic_episode = (
            recorded_program.series_episode_id is not None or
            ParseLegacyEpisodeNumber(recorded_program.episode_number) is not None
        )
        preserve_deterministic_episode = (
            generated.episode_number is None and
            generated.episode_not_numbered is False and
            generated.episode_no_published_number is False and
            has_deterministic_episode
        )
        if preserve_deterministic_episode:
            return True

        # AI が話数を明示しなかった初回判定では、タイトルから厳格に解析できた話数を採用する。
        # episode_not_numbered=True はモデルの明示判断なので、ローカル値で覆さない。
        use_local_episode_fallback = (
            generated.episode_number is None and
            generated.episode_not_numbered is False and
            generated.episode_no_published_number is False and
            local_episode_fallback is not None
        )
        resolved_season_number = (
            local_episode_fallback.season_number
            if use_local_episode_fallback and local_episode_fallback is not None
            else generated.season_number
        )
        resolved_episode_number = (
            local_episode_fallback.episode_number
            if use_local_episode_fallback and local_episode_fallback is not None
            else generated.episode_number
        )
        episode: SeriesEpisode | None = None
        if resolved_season_number is not None and resolved_episode_number is not None:
            episode = await cls._getOrCreateEpisode(
                series_id=target_series_id,
                season_number=resolved_season_number,
                episode_number=resolved_episode_number,
                connection=connection,
            )
        recorded_program.series_episode_id = episode.id if episode is not None else None
        recorded_program.episode_number = (
            FormatEpisodeNumber(episode.season_number, episode.episode_number)
            if episode is not None
            else None
        )

        resolution_status: Literal[
            'Resolved',
            'NeedsReview',
            'NotNumbered',
            'NoPublishedNumber',
        ]
        if episode is not None:
            resolution_status = 'Resolved'
        elif generated.episode_not_numbered:
            resolution_status = 'NotNumbered'
        elif generated.episode_no_published_number:
            resolution_status = 'NoPublishedNumber'
        else:
            # null はモデルが話数を特定できなかった欠損値であり、「番号なし」の明示判断ではない。
            # 後段の話数 Web 検索または手動訂正へ進めるよう、要確認のまま保持する。
            resolution_status = 'NeedsReview'
        canonical_season_number = (
            episode.season_number
            if episode is not None
            else generated.season_number
            if resolution_status in {'NotNumbered', 'NoPublishedNumber'}
            else None
        )
        proposed_outcome: Literal['Resolved', 'NotNumbered', 'NoPublishedNumber'] | None
        if generated.episode_number is not None:
            proposed_outcome = 'Resolved'
        elif generated.episode_not_numbered:
            proposed_outcome = 'NotNumbered'
        elif generated.episode_no_published_number:
            proposed_outcome = 'NoPublishedNumber'
        else:
            proposed_outcome = None
        resolved_at = datetime.now(tz=JST)
        if resolution is None:
            await RecordedEpisodeResolution.create(
                recorded_program_id=recorded_program.id,
                episode_id=episode.id if episode is not None else None,
                season_number=canonical_season_number,
                status=resolution_status,
                source='Local' if use_local_episode_fallback else 'AI',
                lookup_outcome=None,
                input_fingerprint=input_fingerprint,
                provider_fingerprint=None,
                proposed_outcome=proposed_outcome,
                proposed_season_number=generated.season_number,
                proposed_episode_number=generated.episode_number,
                confidence=None if use_local_episode_fallback else generated.confidence,
                web_search_performed=False,
                rationale_short=None if use_local_episode_fallback else generated.rationale_short,
                citations=[],
                ai_model=None if use_local_episode_fallback else generated.model,
                error_code=None,
                error_message=None,
                resolved_at=resolved_at,
                using_db=connection,
            )
            return True

        resolution.episode_id = episode.id if episode is not None else None
        resolution.season_number = canonical_season_number
        resolution.status = resolution_status
        resolution.source = 'Local' if use_local_episode_fallback else 'AI'
        resolution.lookup_outcome = None
        resolution.input_fingerprint = input_fingerprint
        resolution.provider_fingerprint = None
        resolution.proposed_outcome = proposed_outcome
        resolution.proposed_season_number = generated.season_number
        resolution.proposed_episode_number = generated.episode_number
        resolution.confidence = None if use_local_episode_fallback else generated.confidence
        resolution.web_search_performed = False
        resolution.rationale_short = None if use_local_episode_fallback else generated.rationale_short
        resolution.citations = []
        resolution.ai_model = None if use_local_episode_fallback else generated.model
        resolution.error_code = None
        resolution.error_message = None
        resolution.resolved_at = resolved_at
        # manual_* は別レーンとして保持し、AI 再生成では触らない。
        await resolution.save(using_db=connection)
        return True

    @classmethod
    async def synchronizeSeriesAssignment(
        cls,
        recorded_program: RecordedProgram,
        *,
        target_series_id: int | None,
        connection: BaseDBAsyncClient,
    ) -> None:
        """Series変更時に既知Episodeを移動先でupsertし、NotSeriesでは関連を解除する。

        Args:
            recorded_program: 呼び出し元が同一transactionでlock済みの録画。
            target_series_id: 移動先Series ID。NotSeries化ではNone。
            connection: Series更新と共有するtransaction。

        Returns:
            None
        """

        previous_series_id = recorded_program.series_id
        series_changed = previous_series_id != target_series_id
        resolution = await RecordedEpisodeResolution.filter(
            recorded_program_id=recorded_program.id,
        ).using_db(connection).first()
        # Web/EPGの結果はSeries文脈に依存する。別Seriesへ移したときは数値だけを流用せず、
        # AIが互換欄へ書いた値と根拠を破棄して移動先で再判定できる状態へ戻す。
        invalidate_contextual_result = (
            target_series_id is not None and
            series_changed and
            resolution is not None and
            resolution.source in {'WebSearch', 'EPG', 'AI'}
        )
        current_episode: SeriesEpisode | None = None
        if invalidate_contextual_result is False and recorded_program.series_episode_id is not None:
            current_episode = await SeriesEpisode.filter(
                id=recorded_program.series_episode_id,
            ).using_db(connection).first()
        if invalidate_contextual_result is False and current_episode is None:
            parsed = ParseLegacyEpisodeNumber(recorded_program.episode_number)
            if parsed is not None and target_series_id is not None:
                current_episode = await cls._getOrCreateEpisode(
                    series_id=target_series_id,
                    season_number=parsed.season_number,
                    episode_number=parsed.episode_number,
                    connection=connection,
                )

        replacement_episode: SeriesEpisode | None = None
        if target_series_id is not None and current_episode is not None:
            replacement_episode = await cls._getOrCreateEpisode(
                series_id=target_series_id,
                season_number=current_episode.season_number,
                episode_number=current_episode.episode_number,
                connection=connection,
            )
        recorded_program.series_episode_id = replacement_episode.id if replacement_episode is not None else None
        recorded_program_update_fields = ['series_episode_id', 'updated_at']
        if invalidate_contextual_result:
            recorded_program.episode_number = None
            recorded_program_update_fields.append('episode_number')
        await recorded_program.save(update_fields=recorded_program_update_fields, using_db=connection)

        if resolution is not None:
            resolution.episode_id = replacement_episode.id if replacement_episode is not None else None
            if target_series_id is None:
                resolution.season_number = None
                resolution.status = 'Unknown'
                resolution.error_code = 'ProgramNotInSeries'
            elif invalidate_contextual_result:
                resolution.season_number = None
                resolution.status = 'Pending'
                resolution.source = 'Migration' if resolution.is_legacy_recording else 'Local'
                resolution.lookup_outcome = None
                resolution.provider_fingerprint = None
                resolution.proposed_outcome = None
                resolution.proposed_season_number = None
                resolution.proposed_episode_number = None
                resolution.confidence = None
                resolution.web_search_performed = False
                resolution.rationale_short = None
                resolution.citations = []
                resolution.ai_model = None
                resolution.error_code = None
                resolution.error_message = None
            elif replacement_episode is not None:
                resolution.season_number = replacement_episode.season_number
                resolution.status = 'Resolved'
                resolution.error_code = None
            elif resolution.source == 'Manual':
                # 手動の番号なし状態は Series 移動でも保持する。Unknown も自動判定へ戻さない。
                resolution.season_number = resolution.manual_season_number
                resolution.status = resolution.manual_status or 'Unknown'
                resolution.error_code = None
            elif series_changed:
                # Web提案は元Seriesの文脈に依存するため、移動先へ持ち越さず再判定可能に戻す。
                resolution.status = 'Pending'
                resolution.season_number = None
                resolution.source = 'Migration' if resolution.is_legacy_recording else 'Local'
                resolution.lookup_outcome = None
                resolution.provider_fingerprint = None
                resolution.proposed_outcome = None
                resolution.proposed_season_number = None
                resolution.proposed_episode_number = None
                resolution.confidence = None
                resolution.web_search_performed = False
                resolution.rationale_short = None
                resolution.citations = []
                resolution.ai_model = None
                resolution.error_code = None
                resolution.error_message = None
            await resolution.save(
                update_fields=[
                    'episode_id',
                    'season_number',
                    'status',
                    'source',
                    'lookup_outcome',
                    'provider_fingerprint',
                    'proposed_outcome',
                    'proposed_season_number',
                    'proposed_episode_number',
                    'confidence',
                    'web_search_performed',
                    'rationale_short',
                    'citations',
                    'ai_model',
                    'error_code',
                    'error_message',
                    'updated_at',
                ],
                using_db=connection,
            )
