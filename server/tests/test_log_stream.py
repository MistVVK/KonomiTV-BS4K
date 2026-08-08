import json
import os
from pathlib import Path

from app.routers.MaintenanceRouter import IterLogStreamEvents


def test_log_stream_initial_event_is_limited_to_tail(tmp_path: Path) -> None:
    """大容量ログの初回SSEが行数・byte上限内の末尾だけを返すことを検証する。"""

    logs_directory = tmp_path / 'logs'
    logs_directory.mkdir()
    log_path = logs_directory / 'access.log'
    log_path.write_text(''.join(f'line-{index:04d}\n' for index in range(200)), encoding='utf-8')

    events = IterLogStreamEvents(log_path, poll_interval=0, initial_max_bytes=256, initial_max_lines=5)
    try:
        initial_event = next(events)
    finally:
        events.close()

    assert initial_event['event'] == 'initial_log_update'
    assert json.loads(initial_event['data']) == [f'line-{index:04d}' for index in range(195, 200)]


def test_log_stream_drains_old_inode_and_follows_rotated_file(tmp_path: Path) -> None:
    """rotation境界で旧FDの未読行と新ログ先頭を欠落・重複なく順番に配信することを検証する。"""

    logs_directory = tmp_path / 'logs'
    logs_directory.mkdir()
    log_path = logs_directory / 'access.log'
    archive_path = logs_directory / 'access.rotated.log'
    log_path.write_text('initial\n', encoding='utf-8')

    events = IterLogStreamEvents(log_path, poll_interval=0)
    try:
        assert json.loads(next(events)['data']) == ['initial']

        with log_path.open('a', encoding='utf-8') as log_file:
            log_file.write('before-rotation\n')
        os.replace(log_path, archive_path)
        log_path.write_text('after-rotation\n', encoding='utf-8')

        old_inode_event = next(events)
        new_inode_event = next(events)
    finally:
        events.close()

    assert old_inode_event == {
        'event': 'log_update',
        'data': json.dumps('before-rotation', ensure_ascii=False),
    }
    assert new_inode_event == {
        'event': 'log_update',
        'data': json.dumps('after-rotation', ensure_ascii=False),
    }
