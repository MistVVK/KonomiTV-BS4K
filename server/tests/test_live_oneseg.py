import asyncio
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import ServerSettings
from app.models.Channel import Channel
from app.routers.LiveStreamsRouter import ValidateChannelID
from app.streams.LiveEncodingTask import LiveEncodingTask
from app.streams.RecordedPlaybackCapabilities import RecordedPlaybackCapabilityProbe


def _map_targets(options: list[str]) -> list[str]:
    """
    FFmpeg オプション列から -map の直後にある対象指定だけを取り出す。

    Args:
        options (list[str]): 空白分割済みの FFmpeg オプション列。

    Returns:
        list[str]: -map に渡された対象指定の一覧。
    """

    return [options[index + 1] for index, option in enumerate(options) if option == '-map']


@pytest.mark.parametrize(
    ('metadata_backend', 'always_receive_from_mirakurun', 'expected_live_backend'),
    [
        ('EDCB', False, 'EDCB'),
        ('EDCB', True, 'Mirakurun'),
        ('Mirakurun', False, 'Mirakurun'),
        ('Mirakurun', True, 'Mirakurun'),
    ],
)
def test_live_stream_backend_truth_table(
    metadata_backend: str,
    always_receive_from_mirakurun: bool,
    expected_live_backend: str,
) -> None:
    """
    設定上の4通りを、サポートする3つのバックエンド構成へ正規化する。

    Args:
        metadata_backend (str): メタデータ取得元のバックエンド。
        always_receive_from_mirakurun (bool): 映像を常に Mirakurun から受信する設定値。
        expected_live_backend (str): 期待するライブ受信元。

    Returns:
        None
    """

    settings = ServerSettings.model_validate(
        {
            'general': {
                'backend': metadata_backend,
                'always_receive_tv_from_mirakurun': always_receive_from_mirakurun,
            },
        },
        context={'bypass_validation': True},
    )

    assert settings.general.live_stream_backend == expected_live_backend
    assert 'live_stream_backend' not in settings.general.model_dump()


def _build_encoding_task(
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_24fps_mode_enabled: bool,
    video_codec: str = 'avc',
) -> LiveEncodingTask:
    """
    エンコードオプションの単体テストに必要な最小構成のタスクを作成する。

    Args:
        monkeypatch (pytest.MonkeyPatch): Config() をテスト設定へ差し替える fixture。
        is_24fps_mode_enabled (bool): 24fps モード設定値。

    Returns:
        LiveEncodingTask: オプション生成に必要な属性を設定したタスク。
    """

    settings = ServerSettings.model_validate({}, context={'bypass_validation': True})
    monkeypatch.setattr('app.streams.LiveEncodingTask.Config', lambda: settings)
    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        'getSelectedDevice',
        lambda _encoder, _codec=None, _bit_depth=None: '/dev/dri/renderD128',
    )

    task = object.__new__(LiveEncodingTask)
    task._retry_count = 0
    task._live_source_geometry = None
    task.live_stream = SimpleNamespace(
        log_prefix='[Live: test]',
        encoding_options=SimpleNamespace(
            is_24fps_mode_enabled=is_24fps_mode_enabled,
            is_hevc_10bit_enabled=False,
            video_codec=video_codec,
            video_bit_depth=8,
            audio_codec='aac',
        ),
    )
    return task


@pytest.mark.parametrize(
    ('quality', 'expected_gop'),
    [
        ('240p', '8'),
        ('240p-hevc', '30'),
    ],
)
def test_ffmpeg_oneseg_options_preserve_progressive_vfr(
    monkeypatch: pytest.MonkeyPatch,
    quality: str,
    expected_gop: str,
) -> None:
    """
    FFmpegのワンセグ入力へ固定fps・インターレース解除・24fps化を適用しない。

    Args:
        monkeypatch (pytest.MonkeyPatch): Config() をテスト設定へ差し替える fixture。
        quality (str): テストする画質。
        expected_gop (str): 期待する GOP 長。

    Returns:
        None
    """

    task = _build_encoding_task(
        monkeypatch,
        is_24fps_mode_enabled=True,
        video_codec='hevc' if quality.endswith('-hevc') else 'avc',
    )
    options = task.buildFFmpeg8Options(quality, 'FFmpeg', 'GR', False, True)  # type: ignore[arg-type]

    assert options[options.index('-analyzeduration') + 1] == '2500000'
    assert options[options.index('-fps_mode') + 1] == 'vfr'
    assert options[options.index('-g') + 1] == expected_gop
    assert '-r' not in options
    assert _map_targets(options) == ['0:v:0', '0:a?', '0:d?']
    assert '0:a:0' not in options
    assert '0:a:1' not in options
    assert options[options.index('-acodec') + 1] == 'aac'
    assert options[options.index('-ac') + 1] == '2'
    assert options[options.index('-ab') + 1] == '96K'
    assert options[options.index('-ar') + 1] == '48000'
    assert all('yadif' not in option for option in options)
    assert all('pullup' not in option for option in options)
    assert all('dejudder' not in option for option in options)


@pytest.mark.parametrize('encoder_type', ['QSV', 'NVENC', 'AMF'])
@pytest.mark.parametrize(
    ('quality', 'expected_gop'),
    [
        ('240p', '8'),
        ('240p-hevc', '30'),
    ],
)
def test_ffmpeg8_hardware_oneseg_options_preserve_progressive_vfr(
    monkeypatch: pytest.MonkeyPatch,
    encoder_type: str,
    quality: str,
    expected_gop: str,
) -> None:
    """
    FFmpeg 8 の各ハードウェアバックエンドでワンセグ入力へ固定fps・インターレース解除・24fps化を適用しない。

    Args:
        monkeypatch (pytest.MonkeyPatch): Config() をテスト設定へ差し替える fixture。
        encoder_type (str): 公開設定上のハードウェアエンコーダー名。
        quality (str): テストする画質。
        expected_gop (str): 期待する GOP 長。

    Returns:
        None
    """

    task = _build_encoding_task(
        monkeypatch,
        is_24fps_mode_enabled=True,
        video_codec='hevc' if quality.endswith('-hevc') else 'avc',
    )
    options = task.buildFFmpeg8Options(  # type: ignore[arg-type]
        quality,
        encoder_type,
        'GR',
        False,
        True,
    )

    assert options[options.index('-probesize') + 1] == '3000K'
    assert options[options.index('-analyzeduration') + 1] == '2500000'
    assert options[options.index('-fps_mode') + 1] == 'vfr'
    assert options[options.index('-g') + 1] == expected_gop
    assert '-r' not in options
    assert _map_targets(options) == ['0:v:0', '0:a?', '0:d?']
    assert options[options.index('-c:a') + 1] == 'aac'
    assert options[options.index('-b:a') + 1] == '96K'
    assert options[options.index('-ac') + 1] == '2'
    assert options[options.index('-ar') + 1] == '48000'
    video_filter = options[options.index('-vf') + 1]
    assert 'deinterlace' not in video_filter
    assert 'bwdif' not in video_filter
    assert 'pullup' not in video_filter
    assert 'dejudder' not in video_filter
    assert all(not option.startswith('--') for option in options)


def test_non_oneseg_encoding_options_keep_existing_interlaced_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    通常地デジは従来の29.97fps指定とインターレース解除を維持する。

    Args:
        monkeypatch (pytest.MonkeyPatch): Config() をテスト設定へ差し替える fixture。

    Returns:
        None
    """

    task = _build_encoding_task(monkeypatch, is_24fps_mode_enabled=False)

    ffmpeg_options = task.buildFFmpeg8Options('240p', 'FFmpeg', 'GR', False)
    assert ffmpeg_options[ffmpeg_options.index('-analyzeduration') + 1] == '500000'
    assert ffmpeg_options[ffmpeg_options.index('-r') + 1] == '30000/1001'
    assert any('yadif=mode=0' in option for option in ffmpeg_options)
    # 通常ライブ AAC もステレオ AAC-LC へ再エンコードする（放送波 copy は奇妙な音の原因）
    assert ffmpeg_options[ffmpeg_options.index('-acodec') + 1] == 'aac'
    assert ffmpeg_options[ffmpeg_options.index('-ac') + 1] == '2'
    assert _map_targets(ffmpeg_options) == ['0:v:0', '0:a?', '0:d?']
    assert '0:a:0' not in ffmpeg_options
    assert '0:a:1' not in ffmpeg_options

    hardware_options = task.buildFFmpeg8Options('240p', 'QSV', 'GR', False)
    assert hardware_options[hardware_options.index('-probesize') + 1] == '1000K'
    assert hardware_options[hardware_options.index('-analyzeduration') + 1] == '700000'
    assert hardware_options[hardware_options.index('-r') + 1] == '30000/1001'
    assert 'deinterlace=advanced:rate=frame' in hardware_options[hardware_options.index('-vf') + 1]
    assert _map_targets(hardware_options) == ['0:v:0', '0:a?', '0:d?']
    assert hardware_options[hardware_options.index('-c:a') + 1] == 'aac'
    assert hardware_options[hardware_options.index('-ac') + 1] == '2'
    assert all(not option.startswith('--') for option in hardware_options)


def test_radio_ffmpeg_options_map_all_existing_audio_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    ラジオは実在する全音声とデータを一度だけマッピングする。

    Args:
        monkeypatch (pytest.MonkeyPatch): Config() をテスト設定へ差し替える fixture。

    Returns:
        None
    """

    task = _build_encoding_task(monkeypatch, is_24fps_mode_enabled=False)
    options = task.buildFFmpeg8RadioOptions()

    assert _map_targets(options) == ['0:a?', '0:d?']
    assert '0:a:0' not in options
    assert '0:a:1' not in options


@pytest.mark.skipif(shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None, reason='ffmpeg/ffprobe is required')
@pytest.mark.parametrize('audio_stream_count', [1, 2])
def test_ffmpeg_optional_audio_map_starts_with_one_or_two_audio_streams(
    tmp_path: Path,
    audio_stream_count: int,
) -> None:
    """
    旧固定 0:a:0/0:a:1 ではなく 0:a? を1回だけ使い、1/2 音声 TS で FFmpeg が起動できる。

    Args:
        tmp_path (Path): 合成 TS と出力を置く一時ディレクトリ。
        audio_stream_count (int): 入力 TS に含める音声ストリーム数。

    Returns:
        None
    """

    input_path = tmp_path / f'input_{audio_stream_count}a.ts'
    output_path = tmp_path / f'output_{audio_stream_count}a.ts'

    # 映像 1 + 音声 N の最小 MPEG-TS を合成する
    lavfi_inputs = ['-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=30']
    for index in range(audio_stream_count):
        lavfi_inputs += ['-f', 'lavfi', '-i', f'sine=f={440 + index * 100}:r=48000']
    map_args: list[str] = ['-map', '0:v:0']
    for index in range(audio_stream_count):
        map_args += ['-map', f'{index + 1}:a:0']
    subprocess.run(
        [
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
            *lavfi_inputs,
            *map_args,
            '-t', '0.3',
            '-c:v', 'mpeg2video',
            '-c:a', 'mp2',
            '-f', 'mpegts',
            str(input_path),
        ],
        check=True,
    )

    # 本番の通常ライブと同じ map 契約で再エンコードし、起動失敗しないことを確認する
    ## MPEG-TS は program 重複で probe がぶれるため、出力は matroska にして stream 数を数える
    subprocess.run(
        [
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
            '-f', 'mpegts', '-i', str(input_path),
            '-ignore_unknown',
            '-map', '0:v:0',
            '-map', '0:a?',
            '-map', '0:d?',
            '-c:v', 'mpeg2video',
            '-c:a', 'copy',
            '-f', 'matroska',
            str(output_path),
        ],
        check=True,
    )

    probe = subprocess.run(
        [
            'ffprobe', '-hide_banner', '-loglevel', 'error',
            '-select_streams', 'a',
            '-show_entries', 'stream=index',
            '-of', 'csv=p=0',
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    audio_indexes = [line for line in probe.stdout.splitlines() if line.strip() != '']
    assert len(audio_indexes) == audio_stream_count

    # 旧固定 2 音声 map は、音声 1 本の入力で起動失敗することを対照として固定する
    if audio_stream_count == 1:
        old_map_result = subprocess.run(
            [
                'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                '-f', 'mpegts', '-i', str(input_path),
                '-map', '0:v:0',
                '-map', '0:a:0',
                '-map', '0:a:1',
                '-map', '0:d?',
                '-c', 'copy',
                '-f', 'matroska',
                str(tmp_path / 'old_map_should_fail.mkv'),
            ],
            capture_output=True,
            text=True,
        )
        assert old_map_result.returncode != 0
        assert '0:a:1' in old_map_result.stderr or 'matches no streams' in old_map_result.stderr


def test_oneseg_input_analysis_keeps_retry_increments(monkeypatch: pytest.MonkeyPatch) -> None:
    """ワンセグ専用の初期解析値にも既存のリトライ増分を適用する。"""

    task = _build_encoding_task(monkeypatch, is_24fps_mode_enabled=False)
    task._retry_count = 1

    ffmpeg_options = task.buildFFmpeg8Options('240p', 'FFmpeg', 'GR', False, True)
    assert ffmpeg_options[ffmpeg_options.index('-analyzeduration') + 1] == '2700000'

    hardware_options = task.buildFFmpeg8Options('240p', 'QSV', 'GR', False, True)
    assert hardware_options[hardware_options.index('-probesize') + 1] == '3500K'
    assert hardware_options[hardware_options.index('-analyzeduration') + 1] == '2700000'


def test_live_channel_validation_rejects_recording_only_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    録画専用チャンネルIDをライブAPIへ直接指定しても選局を開始しない。

    Args:
        monkeypatch (pytest.MonkeyPatch): Channel.filter() をテスト結果へ差し替える fixture。

    Returns:
        None
    """

    class Query:
        async def get_or_none(self) -> SimpleNamespace:
            return SimpleNamespace(is_watchable=False)

    monkeypatch.setattr(Channel, 'filter', lambda **_kwargs: Query())

    with pytest.raises(HTTPException) as ex_info:
        asyncio.run(ValidateChannelID('gr094'))

    assert ex_info.value.status_code == 422
    assert ex_info.value.detail == 'Specified display_channel_id is not watchable'
