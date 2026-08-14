import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from app import schemas
from app.constants import JST, LIBRARY_PATH
from app.metadata.KonomiTVBS4KMMTSInfoAnalyzer import KonomiTVBS4KMMTSInfoAnalyzer
from app.metadata.MetadataAnalyzer import (
    FFprobeAudioStream,
    FFprobeResult,
    FFprobeSampleResult,
    MetadataAnalyzer,
)


def test_ts_audio_streams_are_merged_by_pid_across_probe_samples() -> None:
    """部分probeでstream indexが変わっても同じPIDを別Trackとして登録しない。"""

    full_streams = [
        FFprobeAudioStream(index=2, codec_type='audio', codec_name='aac', id='0x110', channels=2),
        FFprobeAudioStream(index=3, codec_type='audio', codec_name='aac', id='0x111', channels=0),
    ]
    sample_streams = [
        FFprobeAudioStream(
            index=4,
            codec_type='audio',
            codec_name='aac',
            id='0x111',
            channels=2,
            channel_layout='stereo',
            tags={'language': 'jpn'},
        ),
        FFprobeAudioStream(index=5, codec_type='audio', codec_name='aac', id='0x112', channels=2),
    ]

    merged = MetadataAnalyzer._MetadataAnalyzer__mergeTSAudioStreams(  # pyright: ignore[reportPrivateUsage]
        full_streams,
        sample_streams,
    )

    assert [(stream.index, stream.id) for stream in merged] == [(2, '0x110'), (4, '0x111'), (5, '0x112')]
    assert merged[1].channels == 2
    assert merged[1].tags['language'] == 'jpn'


def test_mmt_tlv_ffprobe_forces_libaribtlv_demuxer(monkeypatch) -> None:
    """拡張子だけに依存せず、MMT/TLV 録画の FFprobe 入力 demuxer を固定する。"""

    commands: list[list[str]] = []
    payload = {
        'format': {
            'format_name': 'libaribtlv',
            'format_long_name': 'ARIB STD-B60 TLV',
            'duration': '60.0',
        },
        'streams': [
            {
                'index': 0,
                'codec_type': 'video',
                'codec_name': 'hevc',
                'profile': 'Main 10',
                'width': 3840,
                'height': 2160,
                'avg_frame_rate': '60000/1001',
                'r_frame_rate': '60000/1001',
                'field_order': 'progressive',
            },
            {
                'index': 1,
                'codec_type': 'audio',
                'codec_name': 'aac',
                'profile': 'LC',
                'channels': 2,
                'sample_rate': '48000',
            },
        ],
        'programs': [],
    }

    def Run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(payload).encode(), b'')

    monkeypatch.setattr('app.metadata.MetadataAnalyzer.subprocess.run', Run)
    analyzer = MetadataAnalyzer(Path('/recording.tlv'))

    result = analyzer._MetadataAnalyzer__analyzeFFprobe()  # pyright: ignore[reportPrivateUsage]

    assert result is not None
    assert commands[0][-3:] == ['-f', 'libaribtlv', '/recording.tlv']


def test_mmt_tlv_metadata_uses_only_raw_mfu_timed_id3_subtitle_tracks(tmp_path: Path, monkeypatch) -> None:
    """復号済み TTML と raw MFU の timed ID3 を同じ字幕として二重登録しない。"""

    recorded_file_path = tmp_path / 'recording.tlv'
    recorded_file_path.write_bytes(bytes(4 * 1024 * 1024))
    streams = [
        {
            'index': 0,
            'codec_type': 'video',
            'codec_name': 'hevc',
            'duration': 60.0,
            'profile': 'Main 10',
            'width': 3840,
            'height': 2160,
            'avg_frame_rate': '60000/1001',
            'r_frame_rate': '60000/1001',
            'field_order': 'progressive',
        },
        {
            'index': 1,
            'codec_type': 'audio',
            'codec_name': 'aac',
            'duration': 60.0,
            'profile': 'LC',
            'channels': 2,
            'sample_rate': '48000',
        },
        {
            'index': 2,
            'codec_type': 'subtitle',
            'codec_name': 'ttml',
            'tags': {'language': 'jpn', 'title': '字幕（復号済み）'},
        },
        {
            'index': 3,
            'codec_type': 'data',
            'codec_name': 'timed_id3',
            'tags': {'component_tag': '48', 'language': 'jpn', 'title': '字幕'},
        },
        {
            'index': 4,
            'codec_type': 'subtitle',
            'codec_name': 'ttml',
            'tags': {'language': 'jpn', 'title': '文字スーパー（復号済み）'},
        },
        {
            'index': 5,
            'codec_type': 'data',
            'codec_name': 'timed_id3',
            'tags': {'component_tag': '56', 'language': 'jpn', 'title': '文字スーパー'},
        },
    ]
    full_probe = FFprobeResult.model_validate({
        'format': {
            'format_name': 'libaribtlv',
            'format_long_name': 'ARIB STD-B60 TLV',
            'duration': '60.0',
        },
        'streams': streams,
        'programs': [],
    })
    sample_probe = FFprobeSampleResult.model_validate({'streams': streams, 'frames': []})

    # このテストでは FFprobe の実行結果だけを固定し、MetadataAnalyzer の track 構築全体を通す。
    monkeypatch.setattr('app.metadata.MetadataAnalyzer.Config', lambda: object())
    monkeypatch.setattr('app.metadata.MetadataAnalyzer.logging.debug', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        MetadataAnalyzer,
        '_MetadataAnalyzer__analyzeFFprobe',
        lambda _self: (full_probe, sample_probe, None),
    )
    monkeypatch.setattr(KonomiTVBS4KMMTSInfoAnalyzer, 'analyze', lambda _self: None)

    recorded_program = MetadataAnalyzer(recorded_file_path).analyze()

    assert recorded_program is not None
    assert recorded_program.recorded_video.subtitle_tracks == [
        {
            'index': 1,
            'stream_index': 3,
            'codec': 'arib_ttml',
            'language': 'jpn',
            'title': '字幕',
            'component_tag': 0x30,
        },
        {
            'index': 2,
            'stream_index': 5,
            'codec': 'arib_ttml',
            'language': 'jpn',
            'title': '文字スーパー',
            'component_tag': 0x38,
        },
    ]


def test_mmt_tlv_si_analyzer_selects_bs4k_service_and_nearest_event(monkeypatch) -> None:
    """映像・音声を持つ BS4K context と録画中央に最も近い MMT-SI event を採用する。"""

    event_start = datetime(2026, 8, 13, 22, 0, 0, tzinfo=JST)
    recorded_video = schemas.RecordedVideo(
        status='Recorded',
        file_path='/recording.tlv',
        file_hash='test-hash',
        file_size=1024,
        file_created_at=event_start,
        file_modified_at=event_start + timedelta(seconds=60),
        recording_start_time=None,
        recording_end_time=None,
        duration=60.0,
        container_format='MMT/TLV',
        video_codec='hevc',
        video_codec_profile='Main 10',
        video_scan_type='Progressive',
        video_frame_rate=59.94,
        video_resolution_width=3840,
        video_resolution_height=2160,
        primary_audio_codec='aac',
        primary_audio_channel='Stereo',
        primary_audio_sampling_rate=48000,
        created_at=event_start,
        updated_at=event_start,
    )
    metadata = {
        'services': [
            {
                'context_id': 1,
                'service_id': 101,
                'tlv_stream_id': 1,
                'original_network_id': 0x0004,
                'service_name': '別サービス',
            },
            {
                'context_id': 2,
                'service_id': 101,
                'tlv_stream_id': 4,
                'original_network_id': 0x000B,
                'service_name': 'BS4K テスト',
            },
        ],
        'events': [
            {
                'context_id': 2,
                'service_id': 101,
                'event_id': 100,
                'start_time_unix_milliseconds': int((event_start - timedelta(hours=1)).timestamp() * 1000),
                'duration_seconds': 3600,
                'free_ca_mode': False,
                'title': '直前番組',
                'description': '',
                'extended_description': '',
                'genres': [],
                'audio_components': [],
            },
            {
                'context_id': 2,
                'service_id': 101,
                'event_id': 101,
                'start_time_unix_milliseconds': int(event_start.timestamp() * 1000),
                'duration_seconds': 60,
                'free_ca_mode': False,
                'title': '対象番組',
                'description': '番組概要',
                'extended_description': '詳細情報',
                'genres': [],
                'audio_components': [],
            },
        ],
        'tracks': [
            {'context_id': 1, 'kind': 'Video'},
            {'context_id': 1, 'kind': 'Audio'},
            {'context_id': 1, 'kind': 'Audio'},
            {'context_id': 2, 'kind': 'Video'},
            {'context_id': 2, 'kind': 'Audio'},
        ],
        'tots': [
            {
                'context_id': 2,
                'time_unix_milliseconds': int(event_start.timestamp() * 1000),
                'input_offset': 1024,
            },
            {
                'context_id': 2,
                'time_unix_milliseconds': int((event_start + timedelta(seconds=60)).timestamp() * 1000),
                'input_offset': 2048,
            },
        ],
    }
    commands: list[list[str]] = []

    def Run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(metadata).encode(), b'')

    monkeypatch.setattr('app.metadata.KonomiTVBS4KMMTSInfoAnalyzer.subprocess.run', Run)

    recorded_program = KonomiTVBS4KMMTSInfoAnalyzer(recorded_video).analyze()

    assert recorded_program is not None
    assert commands == [[LIBRARY_PATH['KonomiTVBS4KTLVMetadata'], '/recording.tlv']]
    assert recorded_program.event_id == 101
    assert recorded_program.title == '対象番組'
    assert recorded_program.description == '番組概要'
    assert recorded_program.detail == {'番組内容': '詳細情報'}
    assert recorded_program.channel is not None
    assert recorded_program.channel.type == 'BS4K'
    assert recorded_program.channel.name == 'BS4K テスト'
    assert recorded_program.channel.transport_stream_id == 4
    assert recorded_program.recorded_video.recording_start_time == event_start
    assert recorded_program.recorded_video.recording_end_time == event_start + timedelta(seconds=60)
    assert recorded_program.is_partially_recorded is False
