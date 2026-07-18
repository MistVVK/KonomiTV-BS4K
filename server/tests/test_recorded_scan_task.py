from datetime import datetime, timedelta

from app.constants import JST
from app.metadata.RecordedScanTask import RecordedVideoSummary


def CreateSummary() -> RecordedVideoSummary:
    timestamp = datetime(2026, 7, 13, tzinfo=JST)
    return RecordedVideoSummary(
        id=1,
        file_path='/recorded/program.ts',
        created_at=timestamp,
        recorded_program_id=1,
        status='Recorded',
        file_created_at=timestamp,
        file_modified_at=timestamp,
        file_size=1024,
        file_hash='hash',
    )


def test_recorded_scan_ignores_ctime_only_changes() -> None:
    """権限変更などでctimeだけが変わっても、録画本体を再解析しない。"""

    summary = CreateSummary()
    summary.file_created_at += timedelta(days=1)

    assert summary.isFileContentUnchanged(summary.file_modified_at, summary.file_size) is True


def test_recorded_scan_detects_content_changes_and_recording() -> None:
    """mtime・サイズ変更と録画中のファイルは従来どおり解析対象にする。"""

    summary = CreateSummary()
    assert summary.isFileContentUnchanged(summary.file_modified_at + timedelta(seconds=1), summary.file_size) is False
    assert summary.isFileContentUnchanged(summary.file_modified_at, summary.file_size + 1) is False

    summary.status = 'Recording'
    assert summary.isFileContentUnchanged(summary.file_modified_at, summary.file_size) is False

    summary.status = 'Analyzing'
    assert summary.isFileContentUnchanged(summary.file_modified_at, summary.file_size) is False
