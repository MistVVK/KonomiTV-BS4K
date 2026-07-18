from dataclasses import dataclass
from typing import Literal

from app.metadata.RecordedPlaybackIndex import (
    RECORDED_PLAYBACK_INDEX_VERSION,
    RecordedPlaybackIndexStatus,
)


ContentState = Literal['New', 'Changed', 'Unchanged']
AnalysisAction = Literal['AnalyzeMetadata', 'BuildPlaybackIndex', 'DetectCM', 'GenerateThumbnail']
AnalysisRequest = Literal['Automatic', 'MetadataReanalysis', 'PlaybackIndexRetry', 'CMDetection']


@dataclass(frozen=True, slots=True)
class RecordedAnalysisPlan:
    """録画内容の状態と利用者の要求から、実行すべき処理集合を表す。"""

    content_state: ContentState
    actions: frozenset[AnalysisAction]
    index_priority: Literal[0, 1, 2] | None


def BuildRecordedAnalysisPlan(
    content_state: ContentState,
    request: AnalysisRequest,
    playback_index_status: RecordedPlaybackIndexStatus | None,
    playback_index_version: int | None,
) -> RecordedAnalysisPlan:
    """録画解析マトリックスから処理計画を構築する。

    Args:
        content_state: DBと実ファイルを比較した内容状態。
        request: 自動スキャンまたは明示的な再解析要求。
        playback_index_status: 現在DBへ公開されている索引状態。新規録画ではNone。
        playback_index_version: 現在DBへ公開されている索引Version。新規録画ではNone。

    Returns:
        実行すべき処理集合と索引キュー優先度。
    """

    # ファイル内容が新規または変更済みなら、呼び出し元の要求にかかわらず
    # 新しい内容に依存する解析結果をすべて作り直す。
    if content_state in ('New', 'Changed'):
        return RecordedAnalysisPlan(
            content_state=content_state,
            actions=frozenset({
                'AnalyzeMetadata',
                'BuildPlaybackIndex',
                'DetectCM',
                'GenerateThumbnail',
            }),
            index_priority=1,
        )

    # ここから先は内容が同一の録画だけを扱う。
    # 手動メタデータ再解析では索引まで再構築するが、CMとサムネイルは保持する。
    if request == 'MetadataReanalysis':
        return RecordedAnalysisPlan(
            content_state=content_state,
            actions=frozenset({'AnalyzeMetadata', 'BuildPlaybackIndex'}),
            index_priority=1,
        )

    # 明示的な索引生成・失敗再実行はMetadataAnalyzerを通さず、再生要求と同じ最優先で処理する。
    if request == 'PlaybackIndexRetry':
        return RecordedAnalysisPlan(
            content_state=content_state,
            actions=frozenset({'BuildPlaybackIndex'}),
            index_priority=0,
        )

    # CM再判定APIは他の解析結果へ触れない。
    if request == 'CMDetection':
        return RecordedAnalysisPlan(
            content_state=content_state,
            actions=frozenset({'DetectCM'}),
            index_priority=None,
        )

    # 自動スキャンでは、未作成または旧Versionの索引だけを低優先度で補完する。
    # 現行VersionでFailedの録画は無限再試行せず、明示的な再実行を待つ。
    should_backfill_index = (
        playback_index_status == 'Pending' or
        (
            playback_index_status == 'Ready' and
            playback_index_version != RECORDED_PLAYBACK_INDEX_VERSION
        )
    )
    if should_backfill_index is True:
        return RecordedAnalysisPlan(
            content_state=content_state,
            actions=frozenset({'BuildPlaybackIndex'}),
            index_priority=2,
        )

    return RecordedAnalysisPlan(
        content_state=content_state,
        actions=frozenset(),
        index_priority=None,
    )
