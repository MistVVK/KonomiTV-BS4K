from typing import Literal


RECORDED_PLAYBACK_INDEX_VERSION = 13

RecordedPlaybackIndexStatus = Literal['Pending', 'Analyzing', 'Ready', 'Failed']
RecordedPlaybackIndexState = Literal['Pending', 'Analyzing', 'Ready', 'Stale', 'Failed']
RecordedPlaybackIndexStage = Literal['Queued', 'Probing', 'Scanning', 'Finalizing', 'Complete', 'Failed']


def GetRecordedPlaybackIndexState(
    index_status: RecordedPlaybackIndexStatus,
    index_version: int | None,
) -> RecordedPlaybackIndexState:
    """DB上の状態と索引Versionからクライアント向け状態を計算する。

    Args:
        index_status: DBへ永続化されている索引状態。
        index_version: 録画に保存されている索引Version。

    Returns:
        現行Versionとの差異をStaleとして表現した公開状態。
    """

    # 完了済み索引だけはVersionを比較し、更新が必要な状態をDB値を壊さずに公開する。
    if index_status == 'Ready' and index_version != RECORDED_PLAYBACK_INDEX_VERSION:
        return 'Stale'
    return index_status


def IsRecordedPlaybackIndexReady(
    index_status: RecordedPlaybackIndexStatus,
    index_version: int | None,
) -> bool:
    """録画を現行索引で再生開始できるかを返す。

    Args:
        index_status: DBへ永続化されている索引状態。
        index_version: 録画に保存されている索引Version。

    Returns:
        Readyかつ現行Versionの場合のみTrue。
    """

    return GetRecordedPlaybackIndexState(index_status, index_version) == 'Ready'
