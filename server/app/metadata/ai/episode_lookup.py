"""話数 Web 検索バックエンド間で共有する結果契約。"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit


EpisodeLookupOutcome = Literal[
    'Pending',
    'Resolved',
    'NotNumbered',
    'NoPublishedNumber',
    'InsufficientEvidence',
    'SearchFailed',
    'SearchNotRun',
    'InvalidModelOutput',
    'Disabled',
    'RateLimited',
    'Cancelled',
]
ModelEpisodeLookupOutcome = Literal[
    'Resolved',
    'NotNumbered',
    'NoPublishedNumber',
    'InsufficientEvidence',
]
EpisodeResolutionStatus = Literal[
    'Pending',
    'Resolved',
    'Unknown',
    'NotNumbered',
    'NoPublishedNumber',
    'NeedsReview',
    'Failed',
]

_MODEL_OUTCOMES: frozenset[EpisodeLookupOutcome] = frozenset({
    'Resolved',
    'NotNumbered',
    'NoPublishedNumber',
    'InsufficientEvidence',
})
_ERROR_OUTCOMES: frozenset[EpisodeLookupOutcome] = frozenset({
    'SearchFailed',
    'SearchNotRun',
    'InvalidModelOutput',
    'Disabled',
    'RateLimited',
    'Cancelled',
})
_ERROR_CODE_PATTERN = re.compile(r'^[A-Za-z0-9_.:-]{1,255}$')
_DNS_LABEL_PATTERN = re.compile(r'^[a-z0-9-]+$')
_IPV4_LIKE_HOST_PATTERN = re.compile(
    r'^(?:(?:0x[0-9a-f]+|[0-9]+)\.)*(?:0x[0-9a-f]+|[0-9]+)$',
    re.IGNORECASE,
)
_NON_PUBLIC_HOST_SUFFIXES = (
    '.arpa',
    '.home',
    '.internal',
    '.invalid',
    '.lan',
    '.local',
    '.localhost',
    '.onion',
    '.test',
)


def IsPublicHTTPURL(url: str) -> bool:
    """citation として保存可能な public HTTP(S) URL かを検証する。

    DNS lookup は行わず、literal IP と予約済み・ローカル用途の hostname を
    保守的に拒否する。URL は表示用の根拠であり、この関数自体は取得しない。

    Args:
        url: adapter が検索実行記録から抽出した URL 候補。

    Returns:
        public HTTP(S) URL として扱える場合は True。
    """

    normalized_url = url.strip()
    if (
        len(normalized_url) == 0 or
        len(normalized_url) > 2048 or
        any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized_url)
    ):
        return False
    try:
        parsed_url = urlsplit(normalized_url)
        port = parsed_url.port
    except ValueError:
        return False
    if (
        parsed_url.scheme not in {'http', 'https'} or
        parsed_url.hostname is None or
        parsed_url.username is not None or
        parsed_url.password is not None
    ):
        return False
    if port is not None and not 1 <= port <= 65535:
        return False

    hostname = parsed_url.hostname.rstrip('.').lower()
    try:
        ascii_hostname = hostname.encode('idna').decode('ascii')
    except UnicodeError:
        return False
    if (
        ascii_hostname == 'localhost' or
        '.' not in ascii_hostname or
        _IPV4_LIKE_HOST_PATTERN.fullmatch(ascii_hostname) is not None or
        ascii_hostname.endswith(_NON_PUBLIC_HOST_SUFFIXES)
    ):
        return False
    try:
        address = ipaddress.ip_address(ascii_hostname)
    except ValueError:
        # DNS 名は解決せず、構文と予約 suffix のみを検証する。
        return all(
            0 < len(label) <= 63 and
            _DNS_LABEL_PATTERN.fullmatch(label) is not None and
            label[0] != '-' and
            label[-1] != '-'
            for label in ascii_hostname.split('.')
        )
    return address.is_global


@dataclass(frozen=True, slots=True)
class EpisodeLookupCitation:
    """adapter が実際の検索記録から検証した HTTP(S) 出典。"""

    url: str
    title: str

    def __post_init__(self) -> None:
        """URL と表示題名を安全な bounded 値へ制限する。"""

        normalized_url = self.url.strip()
        if IsPublicHTTPURL(normalized_url) is False:
            raise ValueError('Episode lookup citation requires a public HTTP(S) URL.')
        normalized_title = ' '.join(self.title.split())
        normalized_title = ''.join(
            character for character in normalized_title if character.isprintable()
        )
        object.__setattr__(self, 'url', normalized_url)
        object.__setattr__(self, 'title', normalized_title[:300])


@dataclass(frozen=True, slots=True)
class EpisodeLookupResult:
    """OpenAI 互換 API と ACP が返す共通の話数検索結果。

    モデルが選べる outcome は ``Resolved`` / ``NotNumbered`` /
    ``NoPublishedNumber`` / ``InsufficientEvidence`` のみである。transport・tool・schema の失敗は
    adapter が残りの outcome へ正規化する。
    """

    outcome: EpisodeLookupOutcome
    season_number: int | None
    episode_number: Decimal | None
    confidence: float | None
    rationale_short: str | None
    citations: tuple[EpisodeLookupCitation, ...]
    web_search_performed: bool
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    http_status: int | None
    latency_ms: int
    error_code: str | None = None
    error_message: str | None = None
    sources: tuple[EpisodeLookupCitation, ...] = ()
    # 失敗時ポリシーによる試行サマリ（秘密なし）。単一試行時は空でもよい。
    recovery_attempt_summaries: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """結果内の値の組み合わせを厳格に検証する。

        Args:
            なし。

        Returns:
            None

        Raises:
            ValueError: outcome と構造化値・理由・エラーの組み合わせが不正な場合。
        """

        if self.outcome not in {
            'Pending',
            'Resolved',
            'NotNumbered',
            'NoPublishedNumber',
            'InsufficientEvidence',
            'SearchFailed',
            'SearchNotRun',
            'InvalidModelOutput',
            'Disabled',
            'RateLimited',
            'Cancelled',
        }:
            raise ValueError('Unknown episode lookup outcome.')
        normalized_model = ' '.join(self.model.split())
        if (
            normalized_model == ''
            or len(normalized_model) > 255
            or any(character.isprintable() is False for character in normalized_model)
        ):
            raise ValueError('Episode lookup model is invalid.')
        if self.latency_ms < 0:
            raise ValueError('Episode lookup latency must not be negative.')
        if self.http_status is not None and not 100 <= self.http_status <= 599:
            raise ValueError('Episode lookup HTTP status is out of range.')
        if self.prompt_tokens is not None and self.prompt_tokens < 0:
            raise ValueError('Episode lookup prompt token count must not be negative.')
        if self.completion_tokens is not None and self.completion_tokens < 0:
            raise ValueError('Episode lookup completion token count must not be negative.')
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError('Episode lookup confidence must be between 0.0 and 1.0.')
        if self.season_number is not None and not 0 <= self.season_number <= 2_147_483_647:
            raise ValueError('Episode lookup season number must not be negative.')
        if self.episode_number is not None:
            episode_exponent = self.episode_number.as_tuple().exponent
            exponent_is_invalid = isinstance(episode_exponent, int) is False
            has_too_many_decimal_places = (
                isinstance(episode_exponent, int) and
                episode_exponent < -3
            )
            if (
                self.episode_number.is_finite() is False or
                self.episode_number < 0 or
                self.episode_number > Decimal('9999999.999') or
                exponent_is_invalid or
                has_too_many_decimal_places
            ):
                raise ValueError('Episode lookup episode number is out of range.')
        if self.web_search_performed is False and (len(self.citations) > 0 or len(self.sources) > 0):
            raise ValueError('Episode lookup without Web search must not contain citations.')

        if self.outcome == 'Resolved' and (
            self.season_number is None or
            self.episode_number is None
        ):
            raise ValueError('Resolved episode lookup requires season and episode numbers.')
        if self.outcome == 'NotNumbered' and (
            self.episode_number is not None
        ):
            raise ValueError('NotNumbered episode lookup must not contain an episode number.')
        if self.outcome == 'NoPublishedNumber' and self.episode_number is not None:
            raise ValueError('NoPublishedNumber episode lookup must not contain an episode number.')
        if self.outcome == 'InsufficientEvidence' and (
            self.season_number is not None or
            self.episode_number is not None
        ):
            raise ValueError('InsufficientEvidence episode lookup must not contain episode numbers.')
        if self.outcome in _MODEL_OUTCOMES:
            if self.confidence is None:
                raise ValueError('Model episode lookup outcomes require confidence.')
            normalized_rationale = (
                ' '.join(self.rationale_short.split())
                if self.rationale_short is not None
                else ''
            )
            if normalized_rationale == '':
                raise ValueError('Model episode lookup outcomes require rationale_short.')
            if (
                len(normalized_rationale) > 500
                or any(
                    character.isprintable() is False
                    for character in normalized_rationale
                )
            ):
                raise ValueError('Episode lookup rationale_short is too long.')
            if self.error_code is not None or self.error_message is not None:
                raise ValueError('Successful model outcomes must not contain an error.')
            if self.web_search_performed is False:
                raise ValueError('Model episode lookup outcomes require verified Web search.')
        if self.outcome in _ERROR_OUTCOMES:
            if (
                self.season_number is not None or
                self.episode_number is not None or
                self.confidence is not None
            ):
                raise ValueError('Episode lookup failure outcomes must not contain model values.')
            if self.error_code is None or self.error_code.strip() == '':
                raise ValueError('Episode lookup failure outcomes require error_code.')
            if (
                self.error_message is None
                or ' '.join(self.error_message.split()) == ''
            ):
                raise ValueError('Episode lookup failure outcomes require error_message.')
            if _ERROR_CODE_PATTERN.fullmatch(self.error_code) is None:
                raise ValueError('Episode lookup error_code is invalid.')
            if (
                len(self.error_message) > 500
                or any(
                    character.isprintable() is False
                    for character in self.error_message
                )
            ):
                raise ValueError('Episode lookup error_message must be a safe one-line summary.')
            if self.rationale_short is not None:
                raise ValueError('Episode lookup failure outcomes must not contain rationale_short.')
        if self.outcome == 'SearchNotRun' and self.web_search_performed:
            raise ValueError('SearchNotRun cannot report that Web search was performed.')
        if self.outcome == 'InvalidModelOutput' and self.web_search_performed is False:
            raise ValueError('InvalidModelOutput requires verified Web search.')
        if self.outcome in {'Disabled', 'RateLimited'} and self.web_search_performed:
            raise ValueError(f'{self.outcome} cannot report that Web search was performed.')
        if self.outcome == 'Pending':
            if (
                self.season_number is not None or
                self.episode_number is not None or
                self.confidence is not None or
                self.rationale_short is not None or
                self.error_code is not None or
                self.error_message is not None or
                self.web_search_performed
            ):
                raise ValueError('Pending episode lookup must not contain a completed result.')
        if self.rationale_short is not None:
            object.__setattr__(
                self,
                'rationale_short',
                ' '.join(self.rationale_short.split()),
            )
        if self.error_code is not None:
            object.__setattr__(self, 'error_code', self.error_code.strip())
        if self.error_message is not None:
            object.__setattr__(
                self,
                'error_message',
                ' '.join(self.error_message.split()),
            )
        object.__setattr__(self, 'model', normalized_model)

    @property
    def numbered(self) -> bool:
        """旧呼び出し側の移行中に構造化数値の有無を返す。"""

        return self.season_number is not None and self.episode_number is not None


def MapLookupOutcomeToResolutionStatus(
    outcome: EpisodeLookupOutcome,
) -> EpisodeResolutionStatus:
    """lookup outcome を録画全体の話数解決 status へ写像する。

    ``Cancelled`` は呼び出し側が中断前の確定値を維持する必要があるため、
    未確定時に再試行できる ``Pending`` を返す。

    Args:
        outcome: 最後の Web 検索試行結果。

    Returns:
        RecordedEpisodeResolution.status に保存する値。
    """

    mapping: dict[EpisodeLookupOutcome, EpisodeResolutionStatus] = {
        'Pending': 'Pending',
        'Resolved': 'Resolved',
        'NotNumbered': 'NotNumbered',
        'NoPublishedNumber': 'NoPublishedNumber',
        'InsufficientEvidence': 'NeedsReview',
        'SearchFailed': 'Failed',
        'SearchNotRun': 'NeedsReview',
        'InvalidModelOutput': 'NeedsReview',
        'Disabled': 'Unknown',
        'RateLimited': 'Unknown',
        'Cancelled': 'Pending',
    }
    return mapping[outcome]
