import pytest

from app.metadata.RecordedAnalysisPlan import BuildRecordedAnalysisPlan
from app.metadata.RecordedPlaybackIndex import RECORDED_PLAYBACK_INDEX_VERSION


@pytest.mark.parametrize(
    ('content_state', 'analysis_request', 'status', 'version', 'actions', 'priority'),
    [
        (
            'New',
            'Automatic',
            None,
            None,
            {'AnalyzeMetadata', 'BuildPlaybackIndex', 'DetectCM', 'GenerateThumbnail'},
            1,
        ),
        (
            'Changed',
            'Automatic',
            'Ready',
            8,
            {'AnalyzeMetadata', 'BuildPlaybackIndex', 'DetectCM', 'GenerateThumbnail'},
            1,
        ),
        (
            'Unchanged',
            'MetadataReanalysis',
            'Ready',
            RECORDED_PLAYBACK_INDEX_VERSION,
            {'AnalyzeMetadata', 'BuildPlaybackIndex'},
            1,
        ),
        ('Unchanged', 'Automatic', 'Pending', None, {'BuildPlaybackIndex'}, 2),
        ('Unchanged', 'Automatic', 'Ready', 7, {'BuildPlaybackIndex'}, 2),
        ('Unchanged', 'PlaybackIndexRetry', 'Failed', None, {'BuildPlaybackIndex'}, 0),
        ('Unchanged', 'CMDetection', 'Ready', RECORDED_PLAYBACK_INDEX_VERSION, {'DetectCM'}, None),
        ('Unchanged', 'Automatic', 'Ready', RECORDED_PLAYBACK_INDEX_VERSION, set(), None),
        ('Unchanged', 'Automatic', 'Failed', None, set(), None),
    ],
)
def test_recorded_analysis_matrix(
    content_state,
    analysis_request,
    status,
    version,
    actions,
    priority,
) -> None:
    """確定済みマトリックスの各行が対応する処理集合へ分類される。"""

    plan = BuildRecordedAnalysisPlan(content_state, analysis_request, status, version)

    assert plan.actions == actions
    assert plan.index_priority == priority
