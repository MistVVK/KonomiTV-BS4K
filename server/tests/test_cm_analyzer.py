# pyright: reportPrivateUsage=false

import asyncio
import json
import struct
from collections.abc import Mapping
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import cast

import pytest

from app.metadata.CMAnalyzer import (
    CMAnalyzerRequest,
    CMInputDescriptor,
    CMInputUnsupportedError,
    GenericCMAnalyzer,
    _ProcessResult,
)


def WriteValidWAV(path: Path, *, samples: int = 1) -> None:
    """48kHz stereo s16leの最小テストWAVを生成する。"""

    data_size = samples * 4
    path.write_bytes(
        b'RIFF'
        + struct.pack('<I', 36 + data_size)
        + b'WAVEfmt '
        + struct.pack('<IHHIIHH', 16, 1, 2, 48_000, 48_000 * 4, 4, 16)
        + b'data'
        + struct.pack('<I', data_size)
        + bytes(data_size)
    )


def CreateRuntime(tmp_path: Path) -> GenericCMAnalyzer:
    runtime = tmp_path / 'runtime'
    (runtime / 'JL').mkdir(parents=True)
    for relative_path in (
        'libffms2.so',
        'ffmsindex',
        'chapter_exe',
        'logoframe',
        'join_logo_scp',
        'JL/JL_標準.txt',
    ):
        (runtime / relative_path).write_bytes(b'test')
    (runtime / 'Runtime-Manifest.json').write_text(
        '{"schema_version":1,"components":{},"patches":{}}\n',
        encoding='utf-8',
    )
    ffmpeg = tmp_path / 'ffmpeg8.elf'
    ffprobe = tmp_path / 'ffprobe8.elf'
    ffmpeg.write_bytes(b'test')
    ffprobe.write_bytes(b'test')
    analyzer = GenericCMAnalyzer(runtime_directory=runtime, ffmpeg_path=ffmpeg, ffprobe_path=ffprobe)

    async def RebuildLogicalAudio(
        request: CMAnalyzerRequest,
        descriptor: CMInputDescriptor,
        output_path: Path,
        environment: Mapping[str, str],
    ) -> _ProcessResult:
        del request, descriptor, environment
        WriteValidWAV(output_path)
        return _ProcessResult(
            0,
            (
                '{"error":null,"stage":"Completed","statistics":'
                '{"decode_errors":0,"decoded_frames":1,"demux_errors":0,"skipped_frames":0}}'
            ),
        )

    analyzer._rebuildLogicalAudio = RebuildLogicalAudio  # type: ignore[method-assign]
    return analyzer


def CreateRequest(
    tmp_path: Path,
    *,
    service_id: int | None = None,
    logo_paths: tuple[Path, ...] = (),
    hardware_device: str | None = None,
) -> CMAnalyzerRequest:
    recorded = tmp_path / 'recording.mkv'
    recorded.write_bytes(b'media')
    (tmp_path / 'work').mkdir(exist_ok=True)
    return CMAnalyzerRequest(
        recorded_file_path=recorded,
        work_directory=tmp_path / 'work',
        service_id=service_id,
        logo_paths=logo_paths,
        hardware_device=hardware_device,
        duration_seconds=60.0,
    )


def test_mmt_tlv_is_stably_unsupported_before_cm_probe(tmp_path: Path, monkeypatch) -> None:
    """MMT/TLV は CM 用 FFmpeg を起動せず、再試行不要の固定 error code を返す。"""

    analyzer = CreateRuntime(tmp_path)
    request = replace(CreateRequest(tmp_path), recorded_file_path=tmp_path / 'recording.tlv')
    request.recorded_file_path.write_bytes(b'tlv')

    async def RunProcess(*_args, **_kwargs):
        pytest.fail('CM probe must not run for MMT/TLV input')

    monkeypatch.setattr(analyzer, '_runProcess', RunProcess)
    with pytest.raises(CMInputUnsupportedError) as ex:
        asyncio.run(analyzer.resolveInputDescriptor(request))

    assert ex.value.code == 'ContainerUnsupportedMMTTLV'


def ProbePayload(*, format_name: str = 'matroska,webm', codec: str = 'av1') -> dict[str, object]:
    return {
        'format': {'format_name': format_name, 'duration': '60.06'},
        'streams': [
            {
                'index': 0,
                'codec_type': 'video',
                'codec_name': codec,
                'pix_fmt': 'yuv420p10le',
                'width': 3840,
                'height': 2160,
                'field_order': 'progressive',
                'time_base': '1/1000',
                'start_time': '1.500',
                'avg_frame_rate': '30000/1001',
                'disposition': {'default': 1},
            },
            {
                'index': 1,
                'codec_type': 'audio',
                'codec_name': 'opus',
                'time_base': '1/48000',
                'start_time': '1.600',
                'disposition': {'default': 1},
            },
        ],
        'programs': [],
    }


def LogicalAudioDescriptor(*, format_name: str = 'mpegts') -> CMInputDescriptor:
    """論理音声前処理単体テスト用のprobe確定値を返す。"""

    return CMInputDescriptor(
        format_name=format_name,
        video_stream_index=1,
        audio_stream_index=2,
        audio_stream_id=0x112,
        video_codec_name='hevc',
        pixel_format='yuv420p10le',
        bit_depth=10,
        width=1920,
        height=1080,
        field_order='progressive',
        time_base=Fraction(1, 90_000),
        source_frame_rate=Fraction(60_000, 1001),
        duration_seconds=60.0,
        video_start_time_seconds=9434.464589,
        video_duration_seconds=59.95,
        program_id=101,
        service_id=101,
    )


def WriteFFmpegOutputs(command: tuple[str, ...]) -> None:
    """テスト用FFmpeg commandの明示された各outputを生成する。"""

    for index, argument in enumerate(command[:-2]):
        if argument == '-f' and command[index + 1] in ('matroska', 'wav'):
            output_path = Path(command[index + 2])
            if command[index + 1] == 'wav':
                WriteValidWAV(output_path)
            else:
                output_path.write_bytes(b'prepared-output')


def PreparedVideoPayload(payload: dict[str, object]) -> dict[str, object]:
    return {'streams': [cast(list[dict[str, object]], payload['streams'])[0]]}


def PreparedAudioPayload(payload: dict[str, object]) -> dict[str, object]:
    source_audio = cast(list[dict[str, object]], payload['streams'])[1]
    return {'streams': [{**source_audio, 'index': 0}]}


def InstallSuccessfulProcesses(
    analyzer: GenericCMAnalyzer,
    payload: dict[str, object],
    commands: list[tuple[str, ...]],
    *,
    trim_text: str = 'Trim(0,899) ++ Trim(1200,1799)\n',
) -> None:
    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        del environment
        commands.append(command)
        if command[0] == str(analyzer.ffprobe_path):
            if command[-1].endswith('prepared-media.cmwork'):
                return _ProcessResult(0, json.dumps(PreparedVideoPayload(payload)))
            if command[-1].endswith('prepared-audio.wav'):
                return _ProcessResult(0, json.dumps(PreparedAudioPayload(payload)))
            return _ProcessResult(0, json.dumps(payload))
        if command[0] == str(analyzer.ffmpeg_path):
            WriteFFmpegOutputs(command)
            return _ProcessResult(0, '')
        if command[0] == str(analyzer.ffmsindex_path):
            Path(command[-1]).write_bytes(b'ffindex')
            return _ProcessResult(0, '')
        if command[0] == str(analyzer.chapter_executable_path):
            Path(command[command.index('-o') + 1]).write_text('# SCPos: 1799 0\n', encoding='utf-8')
            return _ProcessResult(0, 'Video Frames: 1800')
        if command[0] == str(analyzer.logoframe_path):
            analysis_path = Path(command[command.index('-oa') + 1])
            result_path = analysis_path.with_name('selected-logo.txt')
            result_path.write_text('0 S 0 ALL\n900 E 0 ALL\n', encoding='utf-8')
            analysis_path.with_name(f'{analysis_path.stem}_list.ini').write_text(
                '\n'.join((
                    '[logodata]',
                    'LogoTotalN=1',
                    'FrameTotal=1800',
                    'FrameSum_N1=900',
                    'LogoName_N1=logo.lgd',
                    f'oaFileName_N1={result_path}',
                )),
                encoding='utf-8',
            )
            return _ProcessResult(0, '')
        trim_path = Path(command[command.index('-o') + 1])
        trim_path.write_text(trim_text, encoding='utf-8')
        return _ProcessResult(0, '')

    analyzer._runProcess = RunProcess  # type: ignore[method-assign]


def test_descriptor_selects_sid_program_and_ignores_attached_picture(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)
    request = CreateRequest(tmp_path, service_id=101)
    payload = {
        'format': {'format_name': 'mpegts', 'duration': '120'},
        'streams': [
            {
                'index': 0,
                'codec_type': 'video',
                'codec_name': 'mjpeg',
                'width': 4000,
                'height': 3000,
                'disposition': {'attached_pic': 1, 'default': 1},
            },
            {'index': 1, 'codec_type': 'video', 'codec_name': 'h264', 'width': 1920, 'height': 1080},
            {'index': 2, 'codec_type': 'audio', 'codec_name': 'aac'},
            {'index': 3, 'codec_type': 'video', 'codec_name': 'hevc', 'width': 3840, 'height': 2160},
            {'index': 4, 'codec_type': 'audio', 'codec_name': 'aac'},
        ],
        'programs': [
            {'program_id': 101, 'streams': [{'index': 1}, {'index': 2}]},
            {'program_id': 202, 'streams': [{'index': 3}, {'index': 4}]},
        ],
    }

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        del command, environment
        return _ProcessResult(0, json.dumps(payload))

    analyzer._runProcess = RunProcess  # type: ignore[method-assign]
    descriptor = asyncio.run(analyzer.resolveInputDescriptor(request))

    assert descriptor.video_stream_index == 1
    assert descriptor.audio_stream_index == 2
    assert descriptor.video_codec_name == 'h264'
    assert descriptor.program_id == 101
    assert descriptor.service_id == 101


def test_descriptor_selects_container_audio_zero_before_default_or_longest_track(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)
    payload = ProbePayload(format_name='matroska,webm')
    streams = cast(list[dict[str, object]], payload['streams'])
    streams[1].update({
        'index': 1,
        'id': '0x101',
        'duration': '20',
        'disposition': {'default': 0},
    })
    streams.append({
        'index': 2,
        'id': '0x102',
        'codec_type': 'audio',
        'codec_name': 'opus',
        'duration': '60',
        'disposition': {'default': 1},
    })

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        del command, environment
        return _ProcessResult(0, json.dumps(payload))

    analyzer._runProcess = RunProcess  # type: ignore[method-assign]
    descriptor = asyncio.run(analyzer.resolveInputDescriptor(CreateRequest(tmp_path)))

    assert descriptor.audio_stream_index == 1
    assert descriptor.audio_stream_id == 0x101


def test_descriptor_rejects_requested_program_without_video(tmp_path: Path) -> None:
    """指定SIDに映像がなければ別番組を誤解析しない。"""

    analyzer = CreateRuntime(tmp_path)
    payload = {
        'format': {'format_name': 'mpegts', 'duration': '60'},
        'streams': [
            {'index': 2, 'codec_type': 'audio', 'codec_name': 'aac'},
            {'index': 3, 'codec_type': 'video', 'codec_name': 'av1', 'width': 1920, 'height': 1080},
            {'index': 4, 'codec_type': 'audio', 'codec_name': 'opus'},
        ],
        'programs': [
            {'program_id': 101, 'streams': [{'index': 2}]},
            {'program_id': 202, 'streams': [{'index': 3}, {'index': 4}]},
        ],
    }

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        del command, environment
        return _ProcessResult(0, json.dumps(payload))

    analyzer._runProcess = RunProcess  # type: ignore[method-assign]
    with pytest.raises(CMInputUnsupportedError) as error:
        asyncio.run(analyzer.resolveInputDescriptor(CreateRequest(tmp_path, service_id=101)))
    assert error.value.code == 'ProgramVideoStreamUnavailable'


@pytest.mark.parametrize(
    ('format_name', 'codec'),
    [
        ('mpegts', 'h264'),
        ('mpegts', 'hevc'),
        ('mpegts', 'av1'),
        ('mov,mp4,m4a,3gp,3g2,mj2', 'hevc'),
        ('matroska,webm', 'vp9'),
        ('ogg', 'theora'),
    ],
)
def test_registered_container_and_codec_names_do_not_gate_analysis(
    tmp_path: Path,
    format_name: str,
    codec: str,
) -> None:
    analyzer = CreateRuntime(tmp_path)
    commands: list[tuple[str, ...]] = []
    InstallSuccessfulProcesses(analyzer, ProbePayload(format_name=format_name, codec=codec), commands)

    result = asyncio.run(analyzer.analyze(CreateRequest(tmp_path, service_id=None)))

    assert result.status == 'completed'
    assert result.descriptor is not None
    assert result.descriptor.format_name == format_name
    assert result.descriptor.video_codec_name == codec
    assert result.descriptor.service_id is None
    assert result.sections == ({'start_time': 30.03, 'end_time': 40.04},)
    assert all('Amatsukaze' not in argument for command in commands for argument in command)


def test_audio_only_input_is_stably_unsupported(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        del command, environment
        return _ProcessResult(0, json.dumps({
            'format': {'format_name': 'ogg', 'duration': '60'},
            'streams': [{'index': 0, 'codec_type': 'audio', 'codec_name': 'opus'}],
        }))

    analyzer._runProcess = RunProcess  # type: ignore[method-assign]
    result = asyncio.run(analyzer.analyze(CreateRequest(tmp_path)))

    assert result.status == 'unsupported'
    assert result.error_code == 'VideoStreamUnavailable'


def test_video_without_audio_is_stably_unsupported_without_synthesized_silence(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)
    payload = ProbePayload()
    payload['streams'] = cast(list[dict[str, object]], payload['streams'])[:1]

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        del command, environment
        return _ProcessResult(0, json.dumps(payload))

    analyzer._runProcess = RunProcess  # type: ignore[method-assign]
    result = asyncio.run(analyzer.analyze(CreateRequest(tmp_path)))

    assert result.status == 'unsupported'
    assert result.error_code == 'AudioStreamUnavailable'


def test_variable_video_format_is_rejected_before_frames_can_be_silently_dropped(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)
    commands: list[tuple[str, ...]] = []
    InstallSuccessfulProcesses(analyzer, ProbePayload(), commands)
    request = replace(CreateRequest(tmp_path), has_variable_video_format=True)

    result = asyncio.run(analyzer.analyze(request))

    assert result.status == 'unsupported'
    assert result.error_code == 'VariableVideoFormat'
    assert all(command[0] != str(analyzer.chapter_executable_path) for command in commands)


def test_variable_audio_stream_uses_pts_logical_audio_rebuild(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)
    commands: list[tuple[str, ...]] = []
    InstallSuccessfulProcesses(analyzer, ProbePayload(), commands)
    descriptor = asyncio.run(analyzer.resolveInputDescriptor(CreateRequest(tmp_path)))
    request = replace(
        CreateRequest(tmp_path),
        input_descriptor=replace(descriptor, has_variable_audio_stream=True),
    )

    result = asyncio.run(analyzer.analyze(request))

    assert result.status == 'completed'
    assert result.error_code is None
    assert any(command[0] == str(analyzer.ffmpeg_path) for command in commands)


def test_preparation_separates_shared_cfr_video_and_chapter_pcm_without_codec_or_bs4k_branch(
    tmp_path: Path,
) -> None:
    analyzer = CreateRuntime(tmp_path)
    descriptor_payload = ProbePayload(format_name='mpegts', codec='hevc')
    descriptor_payload['programs'] = [{
        'program_id': 101,
        'streams': [
            cast(list[dict[str, object]], descriptor_payload['streams'])[0],
            cast(list[dict[str, object]], descriptor_payload['streams'])[1],
        ],
    }]
    commands: list[tuple[str, ...]] = []
    InstallSuccessfulProcesses(analyzer, descriptor_payload, commands)
    result = asyncio.run(analyzer.analyze(CreateRequest(tmp_path, service_id=101)))

    assert result.status == 'completed'
    chapter_script = (tmp_path / 'work/cpu/chapter.avs').read_text(encoding='utf-8')
    logo_free_script = chapter_script
    assert 'FFVideoSource(' in chapter_script
    assert 'track=0' in chapter_script
    assert 'prepared-media.cmwork' in chapter_script
    assert 'prepared.ffindex' in chapter_script
    assert 'FFAudioSource(' in chapter_script
    assert 'prepared-audio.wav' in chapter_script
    assert 'prepared-audio.ffindex' in chapter_script
    assert 'clip = AudioDubEx(video, audio)' in chapter_script
    assert 'DelayAudio(' not in chapter_script
    assert 'fpsnum=30000, fpsden=1001' in chapter_script
    assert 'ConvertBits(clip, 8)' in chapter_script
    assert 'ConvertToYV12(clip)' in chapter_script
    assert 'Spline36Resize(clip, 1920, 1080)' in chapter_script
    prepare_command = next(
        command for command in commands
        if command[0] == str(analyzer.ffmpeg_path) and str(tmp_path / 'recording.mkv') in command
    )
    mapped_streams = [
        prepare_command[index + 1]
        for index, argument in enumerate(prepare_command[:-1])
        if argument == '-map'
    ]
    assert mapped_streams == ['0:0']
    assert prepare_command[prepare_command.index('-c:v') + 1] == 'copy'
    assert [
        prepare_command[index + 1]
        for index, argument in enumerate(prepare_command[:-1])
        if argument == '-f'
    ] == ['matroska']
    video_output_index = prepare_command.index(str(tmp_path / 'work/prepared-media.cmwork.partial'))
    assert '-an' in prepare_command[:video_output_index]
    assert prepare_command[prepare_command.index('-merge_pmt_versions') + 1] == '1'
    assert '-ac' not in prepare_command
    assert '-c:a' not in prepare_command
    index_commands = [command for command in commands if command[0] == str(analyzer.ffmsindex_path)]
    assert len(index_commands) == 2
    assert {command[-2] for command in index_commands} == {
        str(tmp_path / 'work/prepared-media.cmwork'),
        str(tmp_path / 'work/prepared-audio.wav'),
    }
    chapter_command = next(
        command for command in commands if command[0] == str(analyzer.chapter_executable_path)
    )
    assert '-a' not in chapter_command
    assert len([command for command in commands if command[0] == str(analyzer.ffmpeg_path)]) == 1
    assert 'BlankClip' not in chapter_script
    assert 'Amatsukaze' not in logo_free_script


def test_logical_audio_rebuilder_command_carries_container_track_and_video_pts(
    tmp_path: Path,
) -> None:
    analyzer = CreateRuntime(tmp_path)
    descriptor = LogicalAudioDescriptor()
    commands: list[tuple[str, ...]] = []

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        del environment
        commands.append(command)
        return _ProcessResult(0, '')

    analyzer._runProcess = RunProcess  # type: ignore[method-assign]
    result = asyncio.run(GenericCMAnalyzer._rebuildLogicalAudio(
        analyzer,
        CreateRequest(tmp_path, service_id=101),
        descriptor,
        tmp_path / 'logical.wav',
        {},
    ))

    assert result.return_code == 0
    command = commands[0]
    assert command[1:3] == ('-m', 'app.metadata.CMLogicalAudioRebuilder')
    assert command[command.index('--format-name') + 1] == 'mpegts'
    assert command[command.index('--stream-index') + 1] == '2'
    assert command[command.index('--stream-id') + 1] == str(0x112)
    assert command[command.index('--video-start-time') + 1] == '9434.464589'
    assert command[command.index('--video-duration') + 1] == '59.95'


def test_logical_audio_rebuilder_signal_is_normalized_by_the_parent(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        del command, environment
        return _ProcessResult(-11, '', 'native decoder crashed')

    analyzer._runProcess = RunProcess  # type: ignore[method-assign]
    result = asyncio.run(GenericCMAnalyzer._rebuildLogicalAudio(
        analyzer,
        CreateRequest(tmp_path),
        LogicalAudioDescriptor(),
        tmp_path / 'logical.wav',
        {},
    ))

    assert result.return_code == -11
    assert result.error_output == (
        'Logical audio rebuild killed by signal 11. native decoder crashed'
    )


@pytest.mark.parametrize(
    'primary_process',
    [
        _ProcessResult(-11, '', 'Logical audio rebuild killed by signal 11.'),
        _ProcessResult(
            2,
            (
                '{"error":"No decodable frame was found in logical audio stream 0.",'
                '"stage":"Decode","statistics":{"decode_errors":4,"decoded_frames":0}}'
            ),
            'Logical audio rebuild failed at Decode (exit code 2).',
        ),
        _ProcessResult(
            0,
            (
                '{"error":null,"stage":"Completed","statistics":'
                '{"decode_errors":0,"decoded_frames":1}}'
            ),
        ),
    ],
)
def test_signal_exit_2_and_empty_wav_use_strict_ffmpeg_fallback(
    tmp_path: Path,
    primary_process: _ProcessResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyzer = CreateRuntime(tmp_path)
    request = CreateRequest(tmp_path)
    descriptor = LogicalAudioDescriptor()
    commands: list[tuple[str, ...]] = []
    warning_messages: list[str] = []
    monkeypatch.setattr('app.metadata.CMAnalyzer.logging.warning', warning_messages.append)

    async def RebuildLogicalAudio(
        request: CMAnalyzerRequest,
        descriptor: CMInputDescriptor,
        output_path: Path,
        environment: Mapping[str, str],
    ) -> _ProcessResult:
        del request, descriptor, output_path, environment
        return primary_process

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        del environment
        commands.append(command)
        WriteFFmpegOutputs(command)
        return _ProcessResult(0, '')

    analyzer._rebuildLogicalAudio = RebuildLogicalAudio  # type: ignore[method-assign]
    analyzer._runProcess = RunProcess  # type: ignore[method-assign]
    result = asyncio.run(GenericCMAnalyzer._prepareMedia(
        analyzer,
        request,
        descriptor,
        tmp_path / 'work/prepared-media.cmwork',
        tmp_path / 'work/prepared-audio.wav',
        {},
    ))

    assert result.return_code == 0
    fallback_command = next(command for command in commands if '-f' in command and 'wav' in command)
    assert [
        fallback_command[index + 1]
        for index, argument in enumerate(fallback_command[:-1])
        if argument == '-map'
    ] == ['0:2']
    assert fallback_command[fallback_command.index('-merge_pmt_versions') + 1] == '1'
    assert '-af' in fallback_command
    assert any(
        'AudioRebuildFallback=FFmpeg; StreamMap=0:2; StreamID=274;'
        in message
        for message in warning_messages
    )
    assert (tmp_path / 'work/prepared-media.cmwork').is_file()
    assert GenericCMAnalyzer._validatePreparedWAV(
        tmp_path / 'work/prepared-audio.wav',
    ) is None
    assert result.konomitv_bs4k_logical_audio_rebuild_strategy == (
        'FFmpegFallbackAfterSignal'
        if primary_process.return_code < 0
        else 'FFmpegFallbackAfterReportedFailure'
    )
    assert list((tmp_path / 'work').glob('*.partial*')) == []


def test_previous_failure_uses_ffmpeg_as_primary_without_retrying_pyav(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一入力の失敗実績があれば、捕捉不能なPyAV native crashを再試行しない。"""

    analyzer = CreateRuntime(tmp_path)
    request = replace(
        CreateRequest(tmp_path),
        konomitv_bs4k_logical_audio_rebuild_preference='FFmpegAfterPreviousFailure',
    )
    commands: list[tuple[str, ...]] = []
    warning_messages: list[str] = []
    monkeypatch.setattr('app.metadata.CMAnalyzer.logging.warning', warning_messages.append)

    async def RebuildLogicalAudio(
        request: CMAnalyzerRequest,
        descriptor: CMInputDescriptor,
        output_path: Path,
        environment: Mapping[str, str],
    ) -> _ProcessResult:
        del request, descriptor, output_path, environment
        raise AssertionError('PyAV logical audio rebuild must not run after the same input failed.')

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        del environment
        commands.append(command)
        WriteFFmpegOutputs(command)
        return _ProcessResult(0, '')

    analyzer._rebuildLogicalAudio = RebuildLogicalAudio  # type: ignore[method-assign]
    analyzer._runProcess = RunProcess  # type: ignore[method-assign]
    result = asyncio.run(GenericCMAnalyzer._prepareMedia(
        analyzer,
        request,
        LogicalAudioDescriptor(),
        tmp_path / 'work/prepared-media.cmwork',
        tmp_path / 'work/prepared-audio.wav',
        {},
    ))

    assert result.return_code == 0
    assert result.konomitv_bs4k_logical_audio_rebuild_strategy == 'FFmpegPrimaryAfterPreviousFailure'
    audio_command = next(command for command in commands if '-f' in command and 'wav' in command)
    assert audio_command[audio_command.index('-map') + 1] == '0:2'
    assert any('AudioRebuildPrimary=FFmpeg; Reason=PreviousMediaPreparationFailure;' in message
               for message in warning_messages)
    assert all('app.metadata.CMLogicalAudioRebuilder' not in command for command in commands)


def test_ffmpeg_fallback_failure_preserves_both_layers_and_cleans_partials(
    tmp_path: Path,
) -> None:
    analyzer = CreateRuntime(tmp_path)
    request = CreateRequest(tmp_path)
    descriptor = LogicalAudioDescriptor()

    async def RebuildLogicalAudio(
        request: CMAnalyzerRequest,
        descriptor: CMInputDescriptor,
        output_path: Path,
        environment: Mapping[str, str],
    ) -> _ProcessResult:
        del request, descriptor, environment
        output_path.write_bytes(b'partial primary output')
        return _ProcessResult(
            2,
            (
                '{"error":"No decodable frame was found in logical audio stream 0.",'
                '"stage":"Decode","statistics":{"decode_errors":7,"decoded_frames":0}}'
            ),
        )

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        del environment
        assert command[command.index('-map') + 1] == '0:2'
        Path(command[-1]).write_bytes(b'partial fallback output')
        return _ProcessResult(1, '', 'AAC decoder rejected the selected stream.')

    analyzer._rebuildLogicalAudio = RebuildLogicalAudio  # type: ignore[method-assign]
    analyzer._runProcess = RunProcess  # type: ignore[method-assign]
    result = asyncio.run(GenericCMAnalyzer._prepareMedia(
        analyzer,
        request,
        descriptor,
        tmp_path / 'work/prepared-media.cmwork',
        tmp_path / 'work/prepared-audio.wav',
        {},
    ))

    assert result.return_code == 1
    assert (
        'Logical audio rebuild failed at Decode (exit code 2): '
        'No decodable frame was found in logical audio stream 0.'
    ) in result.error_output
    assert (
        'AudioRebuildFallback=FFmpeg failed: AAC decoder rejected the selected stream.'
    ) in result.error_output
    assert (tmp_path / 'work/prepared-media.cmwork').exists() is False
    assert (tmp_path / 'work/prepared-audio.wav').exists() is False
    assert list((tmp_path / 'work').glob('*.partial*')) == []


def test_stream_selection_failure_does_not_use_ffmpeg_fallback(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)

    async def RebuildLogicalAudio(
        request: CMAnalyzerRequest,
        descriptor: CMInputDescriptor,
        output_path: Path,
        environment: Mapping[str, str],
    ) -> _ProcessResult:
        del request, descriptor, output_path, environment
        return _ProcessResult(
            1,
            (
                '{"error":"The selected logical audio stream is unavailable.",'
                '"stage":"StreamSelection","statistics":{"decoded_frames":0}}'
            ),
        )

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        del command, environment
        raise AssertionError('FFmpeg fallback must not run for stream selection failures.')

    analyzer._rebuildLogicalAudio = RebuildLogicalAudio  # type: ignore[method-assign]
    analyzer._runProcess = RunProcess  # type: ignore[method-assign]
    result = asyncio.run(GenericCMAnalyzer._prepareMedia(
        analyzer,
        CreateRequest(tmp_path),
        LogicalAudioDescriptor(),
        tmp_path / 'work/prepared-media.cmwork',
        tmp_path / 'work/prepared-audio.wav',
        {},
    ))

    assert result.return_code == 1
    assert result.error_output == (
        'Logical audio rebuild failed at StreamSelection (exit code 1): '
        'The selected logical audio stream is unavailable.'
    )


def test_exit_1_without_report_is_diagnosed_without_ffmpeg_fallback(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)

    async def RebuildLogicalAudio(
        request: CMAnalyzerRequest,
        descriptor: CMInputDescriptor,
        output_path: Path,
        environment: Mapping[str, str],
    ) -> _ProcessResult:
        del request, descriptor, output_path, environment
        return _ProcessResult(1, '', '')

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        del command, environment
        raise AssertionError('FFmpeg fallback must not hide an unclassified exit 1.')

    analyzer._rebuildLogicalAudio = RebuildLogicalAudio  # type: ignore[method-assign]
    analyzer._runProcess = RunProcess  # type: ignore[method-assign]
    result = asyncio.run(GenericCMAnalyzer._prepareMedia(
        analyzer,
        CreateRequest(tmp_path),
        LogicalAudioDescriptor(),
        tmp_path / 'work/prepared-media.cmwork',
        tmp_path / 'work/prepared-audio.wav',
        {},
    ))

    assert result.return_code == 1
    assert result.error_output == 'Logical audio rebuild exited with code 1.'


def test_negative_audio_start_offset_is_restored_after_timestamp_reset(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)
    payload = ProbePayload()
    streams = cast(list[dict[str, object]], payload['streams'])
    streams[1]['start_time'] = '1.400'
    commands: list[tuple[str, ...]] = []
    InstallSuccessfulProcesses(analyzer, payload, commands)

    result = asyncio.run(analyzer.analyze(CreateRequest(tmp_path)))

    assert result.status == 'completed'
    chapter_script = (tmp_path / 'work/cpu/chapter.avs').read_text(encoding='utf-8')
    assert 'DelayAudio(' not in chapter_script
    assert len([command for command in commands if command[0] == str(analyzer.ffmpeg_path)]) == 1


def test_equal_audio_and_video_start_omits_delay_without_extra_ffmpeg_pass(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)
    payload = ProbePayload()
    streams = cast(list[dict[str, object]], payload['streams'])
    streams[1]['start_time'] = streams[0]['start_time']
    commands: list[tuple[str, ...]] = []
    InstallSuccessfulProcesses(analyzer, payload, commands)

    result = asyncio.run(analyzer.analyze(CreateRequest(tmp_path)))

    assert result.status == 'completed'
    ffmpeg_commands = [command for command in commands if command[0] == str(analyzer.ffmpeg_path)]
    assert len(ffmpeg_commands) == 1
    assert (tmp_path / 'work/prepared-audio.wav').is_file()
    chapter_script = (tmp_path / 'work/cpu/chapter.avs').read_text(encoding='utf-8')
    assert 'FFAudioSource(' in chapter_script
    assert 'DelayAudio(' not in chapter_script


def test_missing_audio_output_publishes_neither_prepared_output(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)
    payload = ProbePayload()

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        del environment
        if command[0] == str(analyzer.ffprobe_path):
            return _ProcessResult(0, json.dumps(payload))
        if command[0] == str(analyzer.ffmpeg_path):
            video_output_index = command.index('-f') + 2
            Path(command[video_output_index]).write_bytes(b'prepared-video')
            return _ProcessResult(0, '')
        raise AssertionError(f'Unexpected command after failed preparation: {command}')

    analyzer._runProcess = RunProcess  # type: ignore[method-assign]

    async def RebuildLogicalAudio(
        request: CMAnalyzerRequest,
        descriptor: CMInputDescriptor,
        output_path: Path,
        environment: Mapping[str, str],
    ) -> _ProcessResult:
        del request, descriptor, output_path, environment
        return _ProcessResult(0, '')

    analyzer._rebuildLogicalAudio = RebuildLogicalAudio  # type: ignore[method-assign]

    result = asyncio.run(analyzer.analyze(CreateRequest(tmp_path)))

    assert result.status == 'analysis_failed'
    assert result.error_code == 'MediaPreparationFailed'
    assert result.error_message is not None
    assert result.error_message.startswith('Logical audio rebuild produced an invalid WAV:')
    assert 'AudioRebuildFallback=FFmpeg failed: InvalidWAV:' in result.error_message
    assert (tmp_path / 'work/prepared-media.cmwork').exists() is False
    assert (tmp_path / 'work/prepared-audio.wav').exists() is False
    assert list((tmp_path / 'work').glob('*.partial*')) == []


def test_logo_frame_uses_shared_index_and_parallel_ranges_before_passing_match_to_jls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyzer = CreateRuntime(tmp_path)
    monkeypatch.setattr(GenericCMAnalyzer, '_effectiveCPUCount', staticmethod(lambda: 12))
    logo = tmp_path / 'logo.lgd'
    logo.write_bytes(b'logo')
    commands: list[tuple[str, ...]] = []
    InstallSuccessfulProcesses(analyzer, ProbePayload(), commands)

    result = asyncio.run(analyzer.analyze(CreateRequest(tmp_path, logo_paths=(logo,))))

    assert result.status == 'completed'
    assert result.matched_logo == 'logo.lgd'
    logo_commands = [command for command in commands if command[0] == str(analyzer.logoframe_path)]
    assert len(logo_commands) == 1
    logo_command = logo_commands[0]
    assert '-oa' in logo_command
    assert '-o' not in logo_command
    assert logo_command[logo_command.index('-parallel') + 1] == '3'
    assert logo_command[logo_command.index('-logo1') + 1] == str(logo)
    chapter_script = (tmp_path / 'work/cpu/chapter.avs').read_text(encoding='utf-8')
    logo_script = (tmp_path / 'work/cpu/logo.avs').read_text(encoding='utf-8')
    shared_media = str(tmp_path / 'work/prepared-media.cmwork')
    shared_index = str(tmp_path / 'work/prepared.ffindex')
    assert shared_media in chapter_script and shared_media in logo_script
    assert f'cachefile="{shared_index}"' in chapter_script
    assert f'cachefile="{shared_index}"' in logo_script
    assert 'prepared-audio.wav' in chapter_script
    assert 'prepared-audio.ffindex' in chapter_script
    assert 'prepared-audio' not in logo_script
    assert 'threads=12' in chapter_script
    assert 'threads=4' in logo_script
    assert 'colorspace=' not in logo_script
    assert 'ConvertBits' not in logo_script and 'ConvertToYV12' not in logo_script
    chapter_index = next(
        index for index, command in enumerate(commands)
        if command[0] == str(analyzer.chapter_executable_path)
    )
    logo_index = next(
        index for index, command in enumerate(commands)
        if command[0] == str(analyzer.logoframe_path)
    )
    assert chapter_index < logo_index
    jls_command = next(command for command in commands if command[0] == str(analyzer.join_logo_scp_path))
    assert '-inlogo' in jls_command
    assert jls_command[jls_command.index('-inlogo') + 1].endswith('selected-logo.txt')


@pytest.mark.parametrize('pixel_format', ['yuv420p', 'yuv420p10le', 'yuv420p12le', 'yuv420p14le', 'yuv420p16le'])
def test_logo_script_preserves_ffms2_native_luma_format_and_range(
    tmp_path: Path,
    pixel_format: str,
) -> None:
    analyzer = CreateRuntime(tmp_path)
    logo = tmp_path / 'logo.lgd'
    logo.write_bytes(b'logo')
    payload = ProbePayload()
    streams = cast(list[dict[str, object]], payload['streams'])
    streams[0]['pix_fmt'] = pixel_format
    commands: list[tuple[str, ...]] = []
    InstallSuccessfulProcesses(analyzer, payload, commands)

    result = asyncio.run(analyzer.analyze(CreateRequest(tmp_path, logo_paths=(logo,))))

    assert result.status == 'completed'
    logo_script = (tmp_path / 'work/cpu/logo.avs').read_text(encoding='utf-8')
    assert 'colorspace=' not in logo_script
    assert 'ConvertBits' not in logo_script
    assert 'ConvertToYV12' not in logo_script


def test_parallel_logo_decode_stays_on_cpu_when_chapter_uses_hardware(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)
    logo = tmp_path / 'logo.lgd'
    logo.write_bytes(b'logo')
    commands: list[tuple[str, ...]] = []
    InstallSuccessfulProcesses(analyzer, ProbePayload(), commands)

    result = asyncio.run(analyzer.analyze(CreateRequest(
        tmp_path,
        logo_paths=(logo,),
        hardware_device='vaapi:/dev/dri/renderD128',
    )))

    assert result.status == 'completed'
    assert result.decode_mode == 'Hardware'
    chapter_script = (tmp_path / 'work/hardware/chapter.avs').read_text(encoding='utf-8')
    logo_script = (tmp_path / 'work/hardware/logo.avs').read_text(encoding='utf-8')
    assert 'hwdevice="vaapi:/dev/dri/renderD128"' in chapter_script
    assert 'hwdevice=' not in logo_script


@pytest.mark.parametrize(
    ('processor_count', 'total_frames', 'expected_workers'),
    [
        (12, 299, 1),
        (12, 300, 1),
        (12, 899, 1),
        (12, 900, 2),
        (12, 1800, 3),
        (12, 108_000, 6),
        (16, 108_000, 8),
        (48, 108_000, 12),
        (1, 108_000, 1),
    ],
)
def test_logo_frame_worker_count_matches_amatsukaze_balancing(
    monkeypatch: pytest.MonkeyPatch,
    processor_count: int,
    total_frames: int,
    expected_workers: int,
) -> None:
    monkeypatch.setattr(GenericCMAnalyzer, '_effectiveCPUCount', staticmethod(lambda: processor_count))

    assert GenericCMAnalyzer._logoFrameWorkerCount(total_frames) == expected_workers


@pytest.mark.parametrize(
    ('processor_count', 'worker_count', 'expected_chapter_threads', 'expected_logo_threads'),
    [
        (1, 1, 1, 1),
        (4, 1, 4, 4),
        (12, 3, 12, 4),
        (16, 8, 16, 2),
        (48, 12, 16, 4),
        (64, 2, 16, 16),
    ],
)
def test_decoder_threads_follow_actual_process_parallelism(
    monkeypatch: pytest.MonkeyPatch,
    processor_count: int,
    worker_count: int,
    expected_chapter_threads: int,
    expected_logo_threads: int,
) -> None:
    monkeypatch.setattr(GenericCMAnalyzer, '_effectiveCPUCount', staticmethod(lambda: processor_count))

    assert GenericCMAnalyzer._chapterDecoderThreadCount() == expected_chapter_threads
    assert GenericCMAnalyzer._logoDecoderThreadCount(worker_count) == expected_logo_threads


def test_logo_and_chapter_frame_count_must_match(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)
    logo = tmp_path / 'logo.lgd'
    logo.write_bytes(b'logo')
    commands: list[tuple[str, ...]] = []
    InstallSuccessfulProcesses(analyzer, ProbePayload(), commands)
    original_run = analyzer._runProcess

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        result = await original_run(command, environment)
        if command[0] == str(analyzer.logoframe_path):
            analysis_path = Path(command[command.index('-oa') + 1])
            list_path = analysis_path.with_name(f'{analysis_path.stem}_list.ini')
            list_path.write_text(list_path.read_text(encoding='utf-8').replace('1800', '1799'), encoding='utf-8')
        return result

    analyzer._runProcess = RunProcess  # type: ignore[method-assign]
    result = asyncio.run(analyzer.analyze(CreateRequest(tmp_path, logo_paths=(logo,))))

    assert result.status == 'analysis_failed'
    assert result.error_code == 'AnalyzerFrameCountMismatch'


def test_low_logo_ratio_is_not_passed_to_join_logo_scp(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)
    logo = tmp_path / 'logo.lgd'
    logo.write_bytes(b'logo')
    commands: list[tuple[str, ...]] = []
    InstallSuccessfulProcesses(analyzer, ProbePayload(), commands)
    original_run = analyzer._runProcess

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        result = await original_run(command, environment)
        if command[0] == str(analyzer.logoframe_path):
            analysis_path = Path(command[command.index('-oa') + 1])
            list_path = analysis_path.with_name(f'{analysis_path.stem}_list.ini')
            list_path.write_text(
                list_path.read_text(encoding='utf-8').replace('FrameSum_N1=900', 'FrameSum_N1=10'),
                encoding='utf-8',
            )
        return result

    analyzer._runProcess = RunProcess  # type: ignore[method-assign]
    result = asyncio.run(analyzer.analyze(CreateRequest(tmp_path, logo_paths=(logo,))))

    assert result.status == 'completed'
    assert 'LogoNotMatched' in result.warnings
    join_command = next(command for command in commands if command[0] == str(analyzer.join_logo_scp_path))
    assert '-inlogo' not in join_command


@pytest.mark.parametrize(
    ('frame_rate', 'expected_analysis_fps', 'expected_sampling'),
    [
        ('60000/1001', '60000/1001', '20'),
        ('50/1', '50', '10'),
    ],
)
def test_source_frame_rate_uses_amatsukaze_chapter_sampling_boundary(
    tmp_path: Path,
    frame_rate: str,
    expected_analysis_fps: str,
    expected_sampling: str,
) -> None:
    analyzer = CreateRuntime(tmp_path)
    payload = ProbePayload()
    cast(list[dict[str, object]], payload['streams'])[0]['avg_frame_rate'] = frame_rate
    commands: list[tuple[str, ...]] = []
    InstallSuccessfulProcesses(analyzer, payload, commands)

    result = asyncio.run(analyzer.analyze(CreateRequest(tmp_path)))

    assert result.status == 'completed'
    assert result.analysis_fps == expected_analysis_fps
    chapter_script = (tmp_path / 'work/cpu/chapter.avs').read_text(encoding='utf-8')
    source_rate = Fraction(frame_rate)
    assert f'fpsnum={source_rate.numerator}, fpsden={source_rate.denominator}' in chapter_script
    chapter_command = next(
        command for command in commands if command[0] == str(analyzer.chapter_executable_path)
    )
    assert chapter_command[chapter_command.index('-s') + 1] == expected_sampling


def test_hardware_initialization_failure_falls_back_to_cpu_once(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)
    payload = ProbePayload()
    chapter_attempts = 0
    prepare_attempts = 0
    index_attempts = 0

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        nonlocal chapter_attempts, prepare_attempts, index_attempts
        del environment
        if command[0] == str(analyzer.ffprobe_path):
            if command[-1].endswith('prepared-media.cmwork'):
                return _ProcessResult(0, json.dumps(PreparedVideoPayload(payload)))
            if command[-1].endswith('prepared-audio.wav'):
                return _ProcessResult(0, json.dumps(PreparedAudioPayload(payload)))
            return _ProcessResult(0, json.dumps(payload))
        if command[0] == str(analyzer.ffmpeg_path):
            if str(tmp_path / 'recording.mkv') in command:
                prepare_attempts += 1
            WriteFFmpegOutputs(command)
            return _ProcessResult(0, '')
        if command[0] == str(analyzer.ffmsindex_path):
            index_attempts += 1
            Path(command[-1]).write_bytes(b'ffindex')
            return _ProcessResult(0, '')
        if command[0] == str(analyzer.chapter_executable_path):
            chapter_attempts += 1
            script = Path(command[command.index('-v') + 1]).read_text(encoding='utf-8')
            if 'hwdevice=' in script:
                Path(command[command.index('-o') + 1]).write_text('0 0\n', encoding='utf-8')
                return _ProcessResult(
                    0,
                    'Avisynth ERROR: FFVideoSource: FFMS2-HW: failed to create device /dev/dri/renderD128',
                    'Movie data\nVideo Frames: 0',
                )
            Path(command[command.index('-o') + 1]).write_text('# SCPos: 1799 0\n', encoding='utf-8')
            return _ProcessResult(0, 'Video Frames: 1800')
        Path(command[command.index('-o') + 1]).write_text('Trim(0,1799)\n', encoding='utf-8')
        return _ProcessResult(0, '')

    analyzer._runProcess = RunProcess  # type: ignore[method-assign]
    result = asyncio.run(analyzer.analyze(CreateRequest(tmp_path, hardware_device='vaapi:/dev/dri/renderD128')))

    assert result.status == 'completed'
    assert result.decode_mode == 'CPU'
    assert result.warnings[0] == 'HardwareDecodeFallback'
    assert chapter_attempts == 2
    assert prepare_attempts == 1
    assert index_attempts == 2
    assert 'hwdevice=' in (tmp_path / 'work/hardware/chapter.avs').read_text(encoding='utf-8')
    assert 'hwdevice=' not in (tmp_path / 'work/cpu/chapter.avs').read_text(encoding='utf-8')


def test_stage_callback_reports_shared_pipeline_and_hardware_fallback(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)
    payload = ProbePayload()
    logo = tmp_path / 'logo.lgd'
    logo.write_bytes(b'logo')
    commands: list[tuple[str, ...]] = []
    InstallSuccessfulProcesses(analyzer, payload, commands)
    original_run = analyzer._runProcess

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        if command[0] == str(analyzer.chapter_executable_path):
            script = Path(command[command.index('-v') + 1]).read_text(encoding='utf-8')
            if 'hwdevice=' in script:
                return _ProcessResult(1, '', 'FFMS2-HW: test device initialization failed')
        return await original_run(command, environment)

    stages: list[tuple[str, float | None]] = []

    async def StageCallback(stage: str, progress: float | None) -> None:
        stages.append((stage, progress))

    analyzer._runProcess = RunProcess  # type: ignore[method-assign]
    request = replace(
        CreateRequest(tmp_path, logo_paths=(logo,), hardware_device='cuda:0'),
        stage_callback=StageCallback,  # type: ignore[arg-type]
    )

    result = asyncio.run(analyzer.analyze(request))

    assert result.status == 'completed'
    assert [stage for stage, _ in stages] == [
        'PreparingMedia', 'IndexingMedia', 'ChapterAnalyzing', 'HardwareFallback',
        'ChapterAnalyzing', 'LogoAnalyzing', 'CombiningCM',
    ]
    numeric_progress = [progress for _, progress in stages if progress is not None]
    assert numeric_progress == sorted(numeric_progress)


def test_non_hardware_analysis_failure_is_not_retried_on_cpu(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)
    chapter_attempts = 0

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        nonlocal chapter_attempts
        del environment
        if command[0] == str(analyzer.ffprobe_path):
            payload = ProbePayload()
            if command[-1].endswith('prepared-media.cmwork'):
                return _ProcessResult(0, json.dumps(PreparedVideoPayload(payload)))
            if command[-1].endswith('prepared-audio.wav'):
                return _ProcessResult(0, json.dumps(PreparedAudioPayload(payload)))
            return _ProcessResult(0, json.dumps(payload))
        if command[0] == str(analyzer.ffmpeg_path):
            WriteFFmpegOutputs(command)
            return _ProcessResult(0, '')
        if command[0] == str(analyzer.ffmsindex_path):
            Path(command[-1]).write_bytes(b'ffindex')
            return _ProcessResult(0, '')
        chapter_attempts += 1
        return _ProcessResult(1, '', 'chapter algorithm rejected the input')

    analyzer._runProcess = RunProcess  # type: ignore[method-assign]
    result = asyncio.run(analyzer.analyze(CreateRequest(tmp_path, hardware_device='cuda:0')))

    assert result.status == 'analysis_failed'
    assert result.error_code == 'ChapterExeFailed'
    assert chapter_attempts == 1


def test_cpu_decoder_unavailable_is_stably_unsupported(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        del environment
        if command[0] == str(analyzer.ffprobe_path):
            return _ProcessResult(0, json.dumps(ProbePayload(codec='unknown_codec')))
        return _ProcessResult(1, '', 'decoder not found for unknown_codec')

    analyzer._runProcess = RunProcess  # type: ignore[method-assign]
    result = asyncio.run(analyzer.analyze(CreateRequest(tmp_path)))

    assert result.status == 'unsupported'
    assert result.error_code == 'DecoderUnavailable'


def test_runtime_enospc_is_normalized_to_temporary_storage_error(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        del environment
        if command[0] == str(analyzer.ffprobe_path):
            return _ProcessResult(0, json.dumps(ProbePayload()))
        return _ProcessResult(1, '', 'av_interleaved_write_frame(): No space left on device')

    analyzer._runProcess = RunProcess  # type: ignore[method-assign]

    result = asyncio.run(analyzer.analyze(CreateRequest(tmp_path)))

    assert result.status == 'analysis_failed'
    assert result.error_code == 'TemporaryStorageInsufficient'








def test_completed_analysis_returns_sections_without_writing_public_chapter(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)
    commands: list[tuple[str, ...]] = []
    InstallSuccessfulProcesses(analyzer, ProbePayload(), commands)

    result = asyncio.run(analyzer.analyze(CreateRequest(tmp_path)))

    assert result.status == 'completed'
    assert result.chapter_file is None
    assert result.sections == ({'start_time': 30.03, 'end_time': 40.04},)
    assert list((tmp_path / 'work').rglob('generated.cmchapter')) == []


def test_completed_analysis_uses_recording_duration_for_trailing_cm(tmp_path: Path) -> None:
    """解析clipの末尾が長くても、公開CM区間はDBと同じ60秒で終端する。"""

    analyzer = CreateRuntime(tmp_path)
    commands: list[tuple[str, ...]] = []
    InstallSuccessfulProcesses(
        analyzer,
        ProbePayload(),
        commands,
        trim_text='Trim(0,1796)\n',
    )

    result = asyncio.run(analyzer.analyze(CreateRequest(tmp_path)))

    assert result.status == 'completed'
    assert result.sections == ({'start_time': 59.9599, 'end_time': 60.0},)
    assert result.analyzer_version == 'cm-9'


def test_analyzer_requires_precreated_private_workspace(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)
    request = CreateRequest(tmp_path)
    request.work_directory.rmdir()

    async def RunProcess(command: tuple[str, ...], environment: Mapping[str, str]) -> _ProcessResult:
        del command, environment
        return _ProcessResult(0, json.dumps(ProbePayload()))

    analyzer._runProcess = RunProcess  # type: ignore[method-assign]

    result = asyncio.run(analyzer.analyze(request))

    assert result.status == 'analysis_failed'
    assert result.error_code == 'TemporaryStorageUnavailable'


def test_jls_output_requires_ordered_determinate_trim_ranges() -> None:
    assert GenericCMAnalyzer._parseCMSections(
        'Trim(0,899) ++ Trim(1200,1799)',
        1800,
        Fraction(30_000, 1001),
    ) == [{'start_time': 30.03, 'end_time': 40.04}]
    with pytest.raises(ValueError, match='determinate'):
        GenericCMAnalyzer._parseCMSections('', 1800, Fraction(30_000, 1001))
    with pytest.raises(ValueError, match='overlap'):
        GenericCMAnalyzer._parseCMSections(
            'Trim(100,500) ++ Trim(400,900)',
            1800,
            Fraction(30_000, 1001),
        )


def test_jls_trailing_cm_is_clamped_to_canonical_timeline_duration() -> None:
    assert GenericCMAnalyzer._parseCMSections(
        'Trim(0,1796)',
        1810,
        Fraction(30, 1),
        timeline_duration_seconds=60.0,
    ) == [{'start_time': 59.9, 'end_time': 60.0}]








@pytest.mark.parametrize('timeline_duration_seconds', [0.0, -1.0, float('nan'), float('inf')])
def test_jls_rejects_invalid_canonical_timeline_duration(timeline_duration_seconds: float) -> None:
    with pytest.raises(ValueError, match='finite positive'):
        GenericCMAnalyzer._parseCMSections(
            'Trim(0,1796)',
            1810,
            Fraction(30, 1),
            timeline_duration_seconds=timeline_duration_seconds,
        )


def test_missing_runtime_is_reported_as_stable_unavailable(tmp_path: Path) -> None:
    result = asyncio.run(GenericCMAnalyzer(runtime_directory=tmp_path / 'missing').analyze(CreateRequest(tmp_path)))

    assert result.status == 'unsupported'
    assert result.error_code == 'AnalyzerUnavailable'


def test_runtime_manifest_content_invalidates_attempt_fingerprint(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)
    before = analyzer.runtimeFingerprint
    analyzer.runtime_manifest_path.write_text(
        '{"schema_version":1,"components":{"chapter_exe":{"commit":"new"}},"patches":{}}\n',
        encoding='utf-8',
    )
    after = analyzer.runtimeFingerprint

    assert before['native_runtime_manifest_sha256'] != after['native_runtime_manifest_sha256']
    assert after['native_runtime_manifest_error'] is None


def test_playback_ffmpeg_environment_never_loads_private_cm_ffmpeg_libraries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyzer = CreateRuntime(tmp_path)
    monkeypatch.setenv('LD_LIBRARY_PATH', '/host/libraries')
    request = replace(CreateRequest(tmp_path), hardware_environment={'LIBVA_DRIVER_NAME': 'test'})

    media_environment = analyzer._buildMediaEnvironment(request)  # pyright: ignore[reportPrivateUsage]
    native_environment = analyzer._buildEnvironment(request)  # pyright: ignore[reportPrivateUsage]

    assert media_environment['LD_LIBRARY_PATH'] == '/host/libraries'
    assert str(analyzer.runtime_directory) not in media_environment['LD_LIBRARY_PATH']
    assert native_environment['LD_LIBRARY_PATH'] == f'{analyzer.runtime_directory}:/host/libraries'
    assert media_environment['LIBVA_DRIVER_NAME'] == 'test'


def test_invalid_runtime_manifest_is_stably_unavailable(tmp_path: Path) -> None:
    analyzer = CreateRuntime(tmp_path)
    analyzer.runtime_manifest_path.write_text('{}\n', encoding='utf-8')

    result = asyncio.run(analyzer.analyze(CreateRequest(tmp_path)))

    assert result.status == 'unsupported'
    assert result.error_code == 'RuntimeManifestInvalid'
