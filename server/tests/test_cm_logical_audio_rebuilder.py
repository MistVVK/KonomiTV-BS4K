# pyright: reportPrivateUsage=false

import json
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from app.metadata import CMLogicalAudioRebuilder
from app.metadata.CMLogicalAudioRebuilder import (
    CMLogicalAudioRebuildStatistics,
    _CMLogicalAudioRebuildFailure,
    _decodePacket,
    _isMPEGTS,
    _PCMFrameAssembler,
    _selectAudioStream,
)


def test_pts_gap_duplicates_next_valid_frame_instead_of_silence() -> None:
    writes: list[tuple[bytes, int, int]] = []
    assembler = _PCMFrameAssembler(
        lambda pcm, samples, start: writes.append((pcm, samples, start)),
        maximum_samples=3072,
    )
    pcm = b'\x5A' * (1024 * 4)

    assembler.push(3072, 1024, pcm)

    assert writes == [
        (pcm, 1024, 0),
        (pcm, 1024, 1024),
        (pcm, 1024, 2048),
    ]
    assert assembler.statistics.duplicated_frames == 2
    assert all(set(output) == {0x5A} for output, _, _ in writes)


def test_old_pts_frame_is_skipped() -> None:
    writes: list[tuple[bytes, int, int]] = []
    assembler = _PCMFrameAssembler(
        lambda pcm, samples, start: writes.append((pcm, samples, start)),
        maximum_samples=2048,
    )
    pcm = b'\x11' * (1024 * 4)

    assembler.push(0, 1024, pcm)
    assembler.push(0, 1024, pcm)

    assert len(writes) == 1
    assert assembler.statistics.skipped_frames == 1


def test_large_gap_is_capped_and_last_frame_is_trimmed_to_video_duration() -> None:
    writes: list[tuple[bytes, int, int]] = []
    assembler = _PCMFrameAssembler(
        lambda pcm, samples, start: writes.append((pcm, samples, start)),
        maximum_samples=1500,
    )
    pcm = b'\x22' * (1024 * 4)

    assembler.push(48_000_000, 1024, pcm)

    assert [(samples, start, len(output)) for output, samples, start in writes] == [
        (1024, 0, 4096),
        (476, 1024, 1904),
    ]
    assert assembler.statistics.output_samples == 1500


def test_finish_zero_pads_only_the_tail() -> None:
    writes: list[tuple[bytes, int, int]] = []
    assembler = _PCMFrameAssembler(
        lambda pcm, samples, start: writes.append((pcm, samples, start)),
        maximum_samples=1500,
    )
    pcm = b'\x33' * (1024 * 4)

    assembler.push(0, 1024, pcm)
    assembler.finish()

    assert writes[0] == (pcm, 1024, 0)
    assert writes[1] == (bytes(476 * 4), 476, 1024)
    assert assembler.statistics.padded_samples == 476
    assert assembler.statistics.output_samples == 1500


def test_mpegts_detection_uses_demuxer_name_not_filename() -> None:
    assert _isMPEGTS('mpegts') is True
    assert _isMPEGTS('mpegtsraw,mpegts') is True
    assert _isMPEGTS('matroska,webm') is False
    assert _isMPEGTS(None) is False


def test_permission_error_is_counted_and_decode_can_continue(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class PermissionDeniedPacket:
        def decode(self) -> list[Any]:
            raise PermissionError(1, 'avcodec_send_packet()')

    statistics = CMLogicalAudioRebuildStatistics()

    assert _decodePacket(PermissionDeniedPacket(), statistics) == []
    assert statistics.decode_errors == 1
    assert 'PermissionError' in capsys.readouterr().err


def test_decode_error_then_valid_frame_fills_the_timeline_by_duplication(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class PermissionDeniedPacket:
        def decode(self) -> list[tuple[int, int, bytes]]:
            raise PermissionError(1, 'avcodec_send_packet()')

    pcm = b'\x6A' * (1024 * 4)

    class ValidPacket:
        def decode(self) -> list[tuple[int, int, bytes]]:
            return [(3072, 1024, pcm)]

    writes: list[tuple[bytes, int, int]] = []
    statistics = CMLogicalAudioRebuildStatistics()
    assembler = _PCMFrameAssembler(
        lambda output, samples, start: writes.append((output, samples, start)),
        maximum_samples=3072,
        statistics=statistics,
    )

    for packet in (PermissionDeniedPacket(), ValidPacket()):
        for start_sample, samples, output in _decodePacket(packet, statistics):
            assembler.push(start_sample, samples, output)

    assert statistics.decode_errors == 1
    assert statistics.decoded_frames == 1
    assert statistics.duplicated_frames == 2
    assert statistics.padded_samples == 0
    assert statistics.output_samples == 3072
    assert [start for _, _, start in writes] == [0, 1024, 2048]
    assert all(output == pcm for output, _, _ in writes)
    assert 'PermissionError' in capsys.readouterr().err


def test_stream_id_is_preferred_and_index_is_only_the_fallback() -> None:
    class Stream:
        def __init__(self, index: int, stream_id: int) -> None:
            self.type = 'audio'
            self.index = index
            self.id = stream_id

    stream_by_id = Stream(5, 0x101)
    stream_by_index = Stream(2, 0x102)
    container = type('Container', (), {'streams': [stream_by_index, stream_by_id]})()

    assert _selectAudioStream(container, stream_index=2, stream_id=0x101) is stream_by_id
    assert _selectAudioStream(container, stream_index=2, stream_id=0x999) is stream_by_index


def test_rebuild_decodes_only_packets_from_the_selected_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Stream:
        def __init__(self, index: int, stream_id: int) -> None:
            self.type = 'audio'
            self.index = index
            self.id = stream_id
            self.layout = ''

    class Array:
        def tobytes(self) -> bytes:
            return b'\x42' * (1024 * 4)

    class ResampledFrame:
        pts = 0
        time_base = Fraction(1, 48_000)
        samples = 1024

        def to_ndarray(self) -> Array:
            return Array()

    class DecodedFrame:
        pts = 0
        time_base = Fraction(1, 48_000)
        sample_rate = 48_000
        format = type('Format', (), {'name': 'fltp'})()
        layout = type('Layout', (), {'name': 'stereo'})()

    selected_decode_calls = 0

    class Packet:
        def __init__(self, stream: Stream, *, selected: bool) -> None:
            self.stream = stream
            self.time_base = Fraction(1, 48_000)
            self._selected = selected

        def decode(self) -> list[DecodedFrame]:
            nonlocal selected_decode_calls
            if self._selected is False:
                raise AssertionError('A packet from an unselected audio stream was decoded.')
            selected_decode_calls += 1
            return [DecodedFrame()]

    selected_stream = Stream(2, 0x102)
    other_stream = Stream(3, 0x103)

    class InputContainer:
        streams = [selected_stream, other_stream]

        def demux(self) -> list[Packet]:
            return [
                Packet(other_stream, selected=False),
                Packet(selected_stream, selected=True),
            ]

        def close(self) -> None:
            pass

    muxed_packets: list[Any] = []

    class OutputContainer:
        def add_stream(self, codec: str, rate: int) -> Stream:
            assert (codec, rate) == ('pcm_s16le', 48_000)
            return Stream(0, 0)

        def mux(self, packet: Any) -> None:
            muxed_packets.append(packet)

        def close(self) -> None:
            pass

    class OutputPacket:
        def __init__(self, pcm: bytes) -> None:
            self.pcm = pcm

    class Resampler:
        def resample(self, frame: DecodedFrame | None) -> list[ResampledFrame]:
            return [] if frame is None else [ResampledFrame()]

    input_container = InputContainer()
    output_container = OutputContainer()

    def Open(path: str, **options: object) -> InputContainer | OutputContainer:
        del path
        return output_container if options.get('mode') == 'w' else input_container

    monkeypatch.setattr(CMLogicalAudioRebuilder.av, 'open', Open)
    monkeypatch.setattr(CMLogicalAudioRebuilder.av, 'Packet', OutputPacket)
    monkeypatch.setattr(
        CMLogicalAudioRebuilder.av,
        'AudioResampler',
        lambda **options: Resampler(),
    )

    statistics = CMLogicalAudioRebuilder.rebuildLogicalAudio(
        tmp_path / 'input.ts',
        tmp_path / 'output.wav',
        format_name='mpegts',
        stream_index=2,
        stream_id=0x102,
        video_start_time_seconds=0.0,
        video_duration_seconds=0.01,
    )

    assert selected_decode_calls == 1
    assert statistics.decoded_frames == 1
    assert muxed_packets


def test_cli_exit_2_always_reports_stage_statistics_and_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def RebuildLogicalAudio(*args: object, **kwargs: object) -> CMLogicalAudioRebuildStatistics:
        del args
        statistics = kwargs['statistics']
        assert isinstance(statistics, CMLogicalAudioRebuildStatistics)
        statistics.decode_errors = 3
        raise _CMLogicalAudioRebuildFailure(
            'Decode',
            statistics,
            'No decodable frame was found in logical audio stream 0.',
            exit_code=2,
        )

    monkeypatch.setattr(CMLogicalAudioRebuilder, 'rebuildLogicalAudio', RebuildLogicalAudio)
    monkeypatch.setattr(sys, 'argv', [
        'CMLogicalAudioRebuilder',
        '--input', str(tmp_path / 'input.ts'),
        '--output', str(tmp_path / 'output.wav'),
        '--stream-index', '2',
        '--video-start-time', '0',
        '--video-duration', '60',
    ])

    assert CMLogicalAudioRebuilder.main() == 2
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report == {
        'error': 'No decodable frame was found in logical audio stream 0.',
        'stage': 'Decode',
        'statistics': {
            'decode_errors': 3,
            'decoded_frames': 0,
            'demux_errors': 0,
            'duplicated_frames': 0,
            'output_samples': 0,
            'padded_samples': 0,
            'skipped_frames': 0,
        },
    }
    assert captured.err == 'No decodable frame was found in logical audio stream 0.\n'
