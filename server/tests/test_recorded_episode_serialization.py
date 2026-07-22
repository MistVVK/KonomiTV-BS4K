from decimal import Decimal

import pytest

from app import schemas
from app.routers.RecordedSeriesRouter import (
    RecordedEpisodeAssignmentEpisode,
    RecordedEpisodeAssignmentResolution,
)


@pytest.mark.parametrize(
    ('episode_number', 'expected'),
    [
        (Decimal('1E+1'), '10'),
        (Decimal('2E+1'), '20'),
        (Decimal('10.5'), '10.5'),
        (Decimal('7.3E+2'), '730'),
    ],
)
def test_episode_numbers_are_serialized_without_exponents(
    episode_number: Decimal,
    expected: str,
) -> None:
    """Series APIと手動割当APIの話数を指数表記にせず返す。"""

    series_episode = schemas.SeriesEpisode(
        id=1,
        season_number=1,
        episode_number=episode_number,
    )
    assignment_episode = RecordedEpisodeAssignmentEpisode(
        id=1,
        season_number=1,
        episode_number=episode_number,
    )
    assignment_resolution = RecordedEpisodeAssignmentResolution(
        status='NeedsReview',
        source='WebSearch',
        proposed_season_number=1,
        proposed_episode_number=episode_number,
        confidence=0.75,
        web_search_performed=True,
        citations=[],
        error_code='AcceptancePolicyRejected',
    )

    assert series_episode.model_dump(mode='json')['episode_number'] == expected
    assert assignment_episode.model_dump(mode='json')['episode_number'] == expected
    assert assignment_resolution.model_dump(mode='json')['proposed_episode_number'] == expected
