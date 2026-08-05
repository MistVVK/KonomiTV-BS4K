import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import ServerSettings
from app.models.Channel import Channel
from app.routers.LiveStreamsRouter import ValidateChannelID
from app.streams.LiveEncodingTask import LiveEncodingTask


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
    video_codec: str,
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

    task = object.__new__(LiveEncodingTask)
    task._retry_count = 0
    task.live_stream = SimpleNamespace(
        stream_anchor_enabled=True,
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
    options = task.buildFFmpegOptions(quality, 'GR', False, True)  # type: ignore[arg-type]

    assert options[options.index('-analyzeduration') + 1] == '2500000'
    assert options[options.index('-fps_mode') + 1] == 'vfr'
    assert options[options.index('-g') + 1] == expected_gop
    assert '-r' not in options
    assert '0:a:1' not in options
    assert '0:a?' in options
    assert options[options.index('-acodec') + 1] == 'aac'
    assert options[options.index('-ac') + 1] == '2'
    assert options[options.index('-ab') + 1] == '96K'
    assert options[options.index('-ar') + 1] == '48000'
    assert all('yadif' not in option for option in options)
    assert all('pullup' not in option for option in options)
    assert all('dejudder' not in option for option in options)


@pytest.mark.parametrize('encoder_type', ['QSVEncC', 'NVEncC', 'VCEEncC'])
@pytest.mark.parametrize(
    ('quality', 'expected_gop'),
    [
        ('240p', '8'),
        ('240p-hevc', '30'),
    ],
)
def test_hwenc_oneseg_options_preserve_progressive_vfr(
    monkeypatch: pytest.MonkeyPatch,
    encoder_type: str,
    quality: str,
    expected_gop: str,
) -> None:
    """
    各HWEncCのワンセグ入力へ固定fps・インターレース解除・24fps化を適用しない。

    Args:
        monkeypatch (pytest.MonkeyPatch): Config() をテスト設定へ差し替える fixture。
        encoder_type (str): テストする HWEncC の種類。
        quality (str): テストする画質。
        expected_gop (str): 期待する GOP 長。

    Returns:
        None
    """

    task = _build_encoding_task(monkeypatch, is_24fps_mode_enabled=True, video_codec='avc')
    options = task.buildHWEncCOptions(  # type: ignore[arg-type]
        quality,
        encoder_type,
        'GR',
        False,
        True,
    )

    assert options[options.index('--input-probesize') + 1] == '3000K'
    assert float(options[options.index('--input-analyze') + 1]) == pytest.approx(2.5)
    assert options[options.index('--avsync') + 1] == 'vfr'
    assert options[options.index('--gop-len') + 1] == expected_gop
    assert '--fps' not in options
    assert '--interlace' not in options
    assert '--audio-copy' not in options
    assert options[options.index('--audio-codec') + 1] == 'aac'
    assert options[options.index('--audio-bitrate') + 1] == '96'
    assert options[options.index('--audio-samplerate') + 1] == '48000'
    assert options[options.index('--audio-stream') + 1] == ':stereo'
    assert options[options.index('--audio-ignore-decode-error') + 1] == '100'
    assert all(not option.startswith('--vpp-deinterlace') for option in options)
    assert all(not option.startswith('--vpp-yadif') for option in options)
    assert all(not option.startswith('--vpp-afs') for option in options)


def test_non_oneseg_encoding_options_keep_existing_interlaced_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    通常地デジは従来の29.97fps指定とインターレース解除を維持する。

    Args:
        monkeypatch (pytest.MonkeyPatch): Config() をテスト設定へ差し替える fixture。

    Returns:
        None
    """

    task = _build_encoding_task(monkeypatch, is_24fps_mode_enabled=False, video_codec='avc')

    ffmpeg_options = task.buildFFmpegOptions('240p', 'GR', False)
    assert ffmpeg_options[ffmpeg_options.index('-analyzeduration') + 1] == '500000'
    assert ffmpeg_options[ffmpeg_options.index('-r') + 1] == '30000/1001'
    assert any('yadif=mode=0' in option for option in ffmpeg_options)
    assert ffmpeg_options[ffmpeg_options.index('-acodec') + 1] == 'copy'
    assert '-ac' not in ffmpeg_options

    hwenc_options = task.buildHWEncCOptions('240p', 'QSVEncC', 'GR', False)
    assert hwenc_options[hwenc_options.index('--input-probesize') + 1] == '1000K'
    assert float(hwenc_options[hwenc_options.index('--input-analyze') + 1]) == pytest.approx(0.7)
    assert hwenc_options[hwenc_options.index('--fps') + 1] == '30000/1001'
    assert hwenc_options[hwenc_options.index('--interlace') + 1] == 'tff'
    assert hwenc_options[hwenc_options.index('--vpp-deinterlace') + 1] == 'normal'
    assert '--audio-copy' in hwenc_options
    assert '--audio-codec' not in hwenc_options


def test_oneseg_input_analysis_keeps_retry_increments(monkeypatch: pytest.MonkeyPatch) -> None:
    """ワンセグ専用の初期解析値にも既存のリトライ増分を適用する。"""

    task = _build_encoding_task(monkeypatch, is_24fps_mode_enabled=False, video_codec='avc')
    task._retry_count = 1

    ffmpeg_options = task.buildFFmpegOptions('240p', 'GR', False, True)
    assert ffmpeg_options[ffmpeg_options.index('-analyzeduration') + 1] == '2700000'

    hwenc_options = task.buildHWEncCOptions('240p', 'QSVEncC', 'GR', False, True)
    assert hwenc_options[hwenc_options.index('--input-probesize') + 1] == '3500K'
    assert float(hwenc_options[hwenc_options.index('--input-analyze') + 1]) == pytest.approx(2.7)


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
