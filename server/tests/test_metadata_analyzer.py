from app.metadata.MetadataAnalyzer import FFprobeAudioStream, MetadataAnalyzer


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
