from types import SimpleNamespace
from typing import Literal, cast

import pytest

from app.config import ServerSettings
from app.constants import QUALITY_TYPES
from app.streams.LiveEncodingTask import LiveEncodingTask
from app.streams.LiveStream import LiveStream
from app.streams.RecordedPlaybackCapabilities import (
    RecordedPlaybackBackend,
    RecordedPlaybackCapabilityProbe,
    RecordedPlaybackEncoder,
)


LiveChannelType = Literal['GR', 'BS', 'CS', 'CATV', 'SKY', 'BS4K']


def _mapTargets(options: list[str]) -> list[str]:
    """
    FFmpeg 8 オプション列からストリームの map 対象を取り出す

    Args:
        options (list[str]): 空白分割済みの FFmpeg 8 オプション列。

    Returns:
        list[str]: -map に渡された対象の一覧。
    """

    return [options[index + 1] for index, option in enumerate(options) if option == '-map']


def _buildEncodingTask(
    monkeypatch: pytest.MonkeyPatch,
    settings: ServerSettings | None = None,
    is_24fps_mode_enabled: bool = False,
    is_hevc_10bit_enabled: bool = False,
    video_codec: str | None = None,
    video_bit_depth: int | None = None,
    audio_codec: str | None = None,
) -> LiveEncodingTask:
    """
    FFmpeg 8 ライブオプション生成に必要な最小構成のタスクを作成する

    Args:
        monkeypatch (pytest.MonkeyPatch): Config() をテスト設定へ差し替える fixture。
        settings (ServerSettings | None): 使用するサーバー設定。None の場合は既定値。
        is_24fps_mode_enabled (bool): 24fps モードを有効にするかどうか。
        is_hevc_10bit_enabled (bool): HEVC 10bit 出力を要求するかどうか。

    Returns:
        LiveEncodingTask: オプション生成に必要な属性を設定したタスク。
    """

    if settings is None:
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
    encoding_options: dict[str, object] = {
        'is_24fps_mode_enabled': is_24fps_mode_enabled,
        'is_hevc_10bit_enabled': is_hevc_10bit_enabled,
        'video_codec': video_codec or ('hevc' if is_hevc_10bit_enabled is True else 'avc'),
        'video_bit_depth': video_bit_depth or (10 if is_hevc_10bit_enabled is True else 8),
        'audio_codec': audio_codec or 'aac',
    }
    task.live_stream = SimpleNamespace(
        encoding_options=SimpleNamespace(**encoding_options),
        log_prefix='[Live: test]',
    )
    return task


def testSoftwareEncoderUsesFFmpeg8CompatibleOptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    公開設定 FFmpeg が FFmpeg 8 の libx264 で MPEG-TS を生成できるオプションを返す

    Args:
        monkeypatch (pytest.MonkeyPatch): Config() をテスト設定へ差し替える fixture。

    Returns:
        None
    """

    task = _buildEncodingTask(monkeypatch)
    options = task.buildFFmpeg8Options('1080p', 'FFmpeg', 'GR', False)

    assert options[options.index('-vcodec') + 1] == 'libx264'
    assert options[options.index('-pix_fmt') + 1] == 'yuv420p'
    assert options[-3:] == ['-f', 'mpegts', 'pipe:1']
    assert all('EncC' not in option for option in options)


@pytest.mark.parametrize(
    ('encoder_type', 'expected_encoder', 'expected_hwaccel', 'expected_filter'),
    [
        ('QSV', 'h264_qsv', 'qsv', 'vpp_qsv='),
        ('NVENC', 'h264_nvenc', 'cuda', 'bwdif_cuda='),
        ('AMF', 'h264_amf', 'vaapi', 'deinterlace_vaapi='),
    ],
)
def testHardwareEncoderNamesDispatchToFFmpeg8(
    monkeypatch: pytest.MonkeyPatch,
    encoder_type: RecordedPlaybackEncoder,
    expected_encoder: str,
    expected_hwaccel: str,
    expected_filter: str,
) -> None:
    """
    既存の HW エンコーダー設定名を FFmpeg 8 の対応 API とフィルターへ変換する

    Args:
        monkeypatch (pytest.MonkeyPatch): Config() をテスト設定へ差し替える fixture。
        encoder_type (RecordedPlaybackEncoder): 公開設定上のエンコーダー名。
        expected_encoder (str): FFmpeg 8 で期待する映像エンコーダー名。
        expected_hwaccel (str): FFmpeg 8 で期待する HW デコード API。
        expected_filter (str): FFmpeg 8 で期待するフィルター名。

    Returns:
        None
    """

    task = _buildEncodingTask(monkeypatch)
    options = task.buildFFmpeg8Options(
        '1080p',
        encoder_type,
        'GR',
        False,
    )

    assert options[options.index('-c:v') + 1] == expected_encoder
    assert options[options.index('-hwaccel') + 1] == expected_hwaccel
    assert expected_filter in options[options.index('-vf') + 1]
    # ライブ AAC はブラウザ互換のためステレオ AAC-LC へ再エンコードする（copy しない）
    assert options[options.index('-c:a') + 1] == 'aac'
    assert options[options.index('-ac') + 1] == '2'
    assert options[options.index('-c:d') + 1] == 'copy'
    assert options[-3:] == ['-f', 'mpegts', 'pipe:1']
    assert all(not option.startswith('--') for option in options)


@pytest.mark.parametrize(
    ('encoder_type', 'expected_encoder', 'expected_profile', 'expected_filter_format'),
    [
        ('FFmpeg', 'libx265', 'main10', None),
        ('QSV', 'hevc_qsv', 'main10', 'format=p010le'),
        ('NVENC', 'hevc_nvenc', 'main10', 'format=p010le'),
        ('AMF', 'hevc_amf', 'main10', 'format=p010le'),
    ],
)
def testExplicitHEVC10BitUsesExactFFmpeg8BackendContract(
    monkeypatch: pytest.MonkeyPatch,
    encoder_type: RecordedPlaybackEncoder,
    expected_encoder: str,
    expected_profile: str,
    expected_filter_format: str | None,
) -> None:
    """
    明示 HEVC 10bit は能力APIと同じ全 backend の exact bit depth で生成する

    Args:
        monkeypatch (pytest.MonkeyPatch): Config() をテスト設定へ差し替える fixture。
        encoder_type (RecordedPlaybackEncoder): 公開設定上のエンコーダー名。
        expected_encoder (str): FFmpeg 8 で期待する映像エンコーダー。
        expected_profile (str): 期待する HEVC profile。
        expected_filter_format (str | None): HW filter に期待する pixel format。SW では None。

    Returns:
        None
    """

    task = _buildEncodingTask(monkeypatch, is_hevc_10bit_enabled=True)
    options = task.buildFFmpeg8Options('1080p-hevc', encoder_type, 'GR', False)

    encoder_option = '-vcodec' if encoder_type == 'FFmpeg' else '-c:v'
    assert options[options.index(encoder_option) + 1] == expected_encoder
    assert options[options.index('-profile:v') + 1] == expected_profile
    if expected_filter_format is None:
        assert options[options.index('-pix_fmt') + 1] == 'yuv420p10le'
    else:
        assert expected_filter_format in options[options.index('-vf') + 1]


@pytest.mark.parametrize('encoder_type', ['QSV', 'NVENC', 'AMF'])
def testBS4KStableModeKeepsInputAnalysisWithoutLowLatencyFlags(
    monkeypatch: pytest.MonkeyPatch,
    encoder_type: RecordedPlaybackEncoder,
) -> None:
    """
    BS4K の既存安定設定では 8MiB・3秒解析を維持し、低遅延フラグを付与しない

    Args:
        monkeypatch (pytest.MonkeyPatch): Config() をテスト設定へ差し替える fixture。
        encoder_type (RecordedPlaybackEncoder): テストするハードウェアバックエンドの互換設定名。

    Returns:
        None
    """

    settings = ServerSettings.model_validate(
        {
            'general': {
                'encoder_bs4k_input_analysis_enabled': True,
                'encoder_bs4k_low_latency': False,
            },
        },
        context={'bypass_validation': True},
    )
    task = _buildEncodingTask(monkeypatch, settings)
    options = task.buildFFmpeg8Options('1080p', encoder_type, 'BS4K', False)

    assert options[options.index('-probesize') + 1] == '8M'
    assert options[options.index('-analyzeduration') + 1] == '3000000'
    assert '-fflags' not in options
    assert '-flags' not in options
    assert '-flush_packets' not in options


@pytest.mark.parametrize('channel_type', ['GR', 'BS', 'CS', 'CATV', 'SKY'])
@pytest.mark.parametrize('encoder_type', ['FFmpeg', 'QSV', 'NVENC', 'AMF'])
def testNonBS4KBroadcastsUseOneSharedLowLatencyMPEGTSContract(
    monkeypatch: pytest.MonkeyPatch,
    channel_type: LiveChannelType,
    encoder_type: RecordedPlaybackEncoder,
) -> None:
    """
    非 BS4K の全放送種別・全バックエンドでクライアントモードに依存しない即時出力 TS を生成する

    Args:
        monkeypatch (pytest.MonkeyPatch): Config() をテスト設定へ差し替える fixture。
        channel_type (LiveChannelType): テストする放送種別。
        encoder_type (RecordedPlaybackEncoder): 公開設定上のエンコーダー名。

    Returns:
        None
    """

    task = _buildEncodingTask(monkeypatch)
    options = task.buildFFmpeg8Options('1080p', encoder_type, channel_type, False)

    assert options[options.index('-fflags') + 1] == 'nobuffer'
    assert options[options.index('-flags') + 1] == 'low_delay'
    assert options[options.index('-flush_packets') + 1] == '1'
    assert options[options.index('-max_delay') + 1] == '250000'
    assert options[options.index('-max_interleave_delta') + 1] == '500K'
    assert _mapTargets(options) == ['0:v:0', '0:a?', '0:d?']
    assert options[-3:] == ['-f', 'mpegts', 'pipe:1']
    assert all(not option.startswith('--') for option in options)


@pytest.mark.parametrize('encoder_type', ['FFmpeg', 'QSV', 'NVENC', 'AMF'])
def testBS4KLowLatencyModeUsesFFmpeg8ImmediateOutputContract(
    monkeypatch: pytest.MonkeyPatch,
    encoder_type: RecordedPlaybackEncoder,
) -> None:
    """
    BS4K の即時出力優先 ON でも全バックエンドを FFmpeg 8 の低遅延 TS 契約へ揃える

    Args:
        monkeypatch (pytest.MonkeyPatch): Config() をテスト設定へ差し替える fixture。
        encoder_type (RecordedPlaybackEncoder): 公開設定上のエンコーダー名。

    Returns:
        None
    """

    settings = ServerSettings.model_validate(
        {
            'general': {
                'encoder_bs4k_input_analysis_enabled': True,
                'encoder_bs4k_low_latency': True,
            },
        },
        context={'bypass_validation': True},
    )
    task = _buildEncodingTask(monkeypatch, settings)
    options = task.buildFFmpeg8Options('1080p', encoder_type, 'BS4K', True)

    assert options[options.index('-fflags') + 1] == 'nobuffer'
    assert options[options.index('-flags') + 1] == 'low_delay'
    assert options[options.index('-flush_packets') + 1] == '1'
    assert _mapTargets(options) == ['0:v:0', '0:a?', '0:d?']
    assert options[-3:] == ['-f', 'mpegts', 'pipe:1']


def testRadioUsesSharedFFmpeg8LowLatencyMPEGTSContract(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    ラジオは映像バックエンドを起動せず FFmpeg 8 で全音声・データを即時出力する

    Args:
        monkeypatch (pytest.MonkeyPatch): Config() をテスト設定へ差し替える fixture。

    Returns:
        None
    """

    task = _buildEncodingTask(monkeypatch)
    options = task.buildFFmpeg8RadioOptions()

    assert _mapTargets(options) == ['0:a?', '0:d?']
    assert '-vcodec' not in options
    assert '-c:v' not in options
    assert options[options.index('-fflags') + 1] == 'nobuffer'
    assert options[options.index('-flags') + 1] == 'low_delay'
    assert options[options.index('-flush_packets') + 1] == '1'
    assert options[-3:] == ['-f', 'mpegts', 'pipe:1']


def testClientLowLatencyModesShareOneServerStreamIdentity() -> None:
    """
    クライアントの低遅延 ON / OFF をサーバーの stream ID やエンコーダータスクへ混入させない

    Returns:
        None
    """

    # ON/OFF はクライアント側の再生ポリシーであり、サーバーへは同じチャンネル・画質として接続される。
    low_latency_on_stream = LiveStream('gr-low-latency-contract', '1080p')
    low_latency_off_stream = LiveStream('gr-low-latency-contract', '1080p')

    assert low_latency_on_stream is low_latency_off_stream
    assert low_latency_on_stream.live_stream_id == 'gr-low-latency-contract-1080p'


def testRemovedEncoderBackendFailsClosed(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    撤去済みの独立エンコーダー実行体を示す不正値を別バックエンドへ誤配送しない

    Args:
        monkeypatch (pytest.MonkeyPatch): Config() をテスト設定へ差し替える fixture。

    Returns:
        None
    """

    task = _buildEncodingTask(monkeypatch)
    removed_backend = cast(RecordedPlaybackEncoder, 'QSVEncCExecutable')

    with pytest.raises(ValueError, match='Unsupported FFmpeg 8 live backend'):
        task.buildFFmpeg8Options('1080p', removed_backend, 'GR', False)


@pytest.mark.parametrize('encoder_type', ['QSV', 'AMF'])
def testMissingRenderDeviceFailsClosed(
    monkeypatch: pytest.MonkeyPatch,
    encoder_type: RecordedPlaybackEncoder,
) -> None:
    """
    QSV / AMF の対応 render node がない場合に別 GPU やソフトウェアへ暗黙フォールバックしない

    Args:
        monkeypatch (pytest.MonkeyPatch): GPU 能力検査結果を差し替える fixture。
        encoder_type (RecordedPlaybackEncoder): render node を必要とするバックエンド名。

    Returns:
        None
    """

    task = _buildEncodingTask(monkeypatch)
    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        'getSelectedDevice',
        lambda _encoder, _codec=None, _bit_depth=None: None,
    )
    monkeypatch.setattr(RecordedPlaybackBackend, 'discoverRenderDevices', lambda _encoder: [])

    with pytest.raises(RuntimeError, match='No compatible render device'):
        task.buildFFmpeg8Options('1080p', encoder_type, 'GR', False)


@pytest.mark.parametrize(
    ('quality', 'is_24fps_mode_enabled', 'expected_timing_option', 'expected_timing_value'),
    [
        ('1080p', False, '-r', '30000/1001'),
        ('1080p-60fps', False, '-r', '60000/1001'),
        ('1080p', True, '-fps_mode', 'vfr'),
    ],
)
@pytest.mark.parametrize('encoder_type', ['FFmpeg', 'QSV', 'NVENC', 'AMF'])
def testFFmpeg8FrameRateMatrix(
    monkeypatch: pytest.MonkeyPatch,
    quality: QUALITY_TYPES,
    is_24fps_mode_enabled: bool,
    expected_timing_option: str,
    expected_timing_value: str,
    encoder_type: RecordedPlaybackEncoder,
) -> None:
    """
    FFmpeg 8 の全バックエンドで 30p・60p・24/30p VFR の時間契約を維持する

    Args:
        monkeypatch (pytest.MonkeyPatch): Config() をテスト設定へ差し替える fixture。
        quality (QUALITY_TYPES): テストする画質。
        is_24fps_mode_enabled (bool): 24fps モードを有効にするかどうか。
        expected_timing_option (str): 期待するフレーム時間指定オプション。
        expected_timing_value (str): 期待するフレーム時間指定値。
        encoder_type (RecordedPlaybackEncoder): 公開設定上のエンコーダー名。

    Returns:
        None
    """

    task = _buildEncodingTask(monkeypatch, is_24fps_mode_enabled=is_24fps_mode_enabled)
    options = task.buildFFmpeg8Options(quality, encoder_type, 'GR', False)

    assert options[options.index(expected_timing_option) + 1] == expected_timing_value
    if is_24fps_mode_enabled is True:
        assert 'pullup' in options[options.index('-vf') + 1]
        assert 'dejudder' in options[options.index('-vf') + 1]


@pytest.mark.parametrize('encoder_type', ['FFmpeg', 'QSV', 'NVENC', 'AMF'])
def testAllLiveBackendsResolveToFFmpeg8Executable(
    encoder_type: RecordedPlaybackEncoder,
) -> None:
    """
    公開設定名を維持しても独立 EncC 実行ファイルへ戻らず FFmpeg 8 実行体だけを選ぶ

    Args:
        encoder_type (RecordedPlaybackEncoder): 公開設定上のエンコーダー名。

    Returns:
        None
    """

    executable = RecordedPlaybackBackend.getExecutable(encoder_type)

    assert 'FFmpeg8' in executable
    assert not executable.endswith(('QSVEncC.elf', 'NVEncC.elf', 'VCEEncC.elf'))


@pytest.mark.parametrize(
    (
        'encoder_type',
        'video_codec',
        'bit_depth',
        'expected_encoder',
        'expected_profile',
        'expected_muxrate',
    ),
    [
        ('FFmpeg', 'vp9', 8, 'libvpx-vp9', '0', '5450K'),
        ('FFmpeg', 'av1', 10, 'libaom-av1', '0', '12150K'),
        ('QSV', 'vp9', 10, 'vp9_qsv', 'profile2', '5450K'),
        ('QSV', 'av1', 10, 'av1_qsv', 'main', '12150K'),
        ('NVENC', 'av1', 8, 'av1_nvenc', None, '12150K'),
        ('AMF', 'av1', 8, 'av1_amf', 'main', '12150K'),
    ],
)
def testAdvancedLiveVideoCodecOptionsUseRealtimeNoLookaheadContract(
    monkeypatch: pytest.MonkeyPatch,
    encoder_type: RecordedPlaybackEncoder,
    video_codec: str,
    bit_depth: int,
    expected_encoder: str,
    expected_profile: str | None,
    expected_muxrate: str,
) -> None:
    """高度映像 codec は実時間・先読みなしで Bridge 前の MPEG-TS を生成する。"""

    task = _buildEncodingTask(
        monkeypatch,
        video_codec=video_codec,
        video_bit_depth=bit_depth,
        audio_codec='aac',
    )
    options = task.buildFFmpeg8Options('1080p', encoder_type, 'GR', False)

    assert options[options.index('-c:v') + 1] == expected_encoder
    assert options[-3:] == ['-f', 'mpegts', 'pipe:1']
    assert options[options.index('-muxrate') + 1] == expected_muxrate
    assert options[options.index('-pcr_period') + 1] == '20'
    # SW / HW とも VBV 相当 bufsize で I フレーム突発を抑える。
    assert options[options.index('-bufsize') + 1] == options[options.index('-maxrate') + 1]
    assert task.isTSCodecBridgeRequired() is True
    if encoder_type == 'FFmpeg':
        assert options[options.index('-lag-in-frames') + 1] == '0'
        if video_codec == 'vp9':
            assert options[options.index('-auto-alt-ref') + 1] == '0'
    elif encoder_type == 'QSV':
        assert options[options.index('-look_ahead') + 1] == '0'
    elif encoder_type == 'NVENC':
        assert options[options.index('-rc-lookahead') + 1] == '0'
    else:
        assert options[options.index('-async_depth') + 1] == '1'
    if expected_profile is None:
        assert '-profile:v' not in options
    else:
        assert options[options.index('-profile:v') + 1] == expected_profile


@pytest.mark.parametrize(
    ('quality', 'expected_muxrate'),
    [
        ('240p', '1650K'),
        ('360p', '3300K'),
        ('540p', '6600K'),
        ('720p', '11000K'),
    ],
)
def testAV1QualitiesUseRequiredMinimumLevelBoundedTransportMuxrate(
    monkeypatch: pytest.MonkeyPatch,
    quality: QUALITY_TYPES,
    expected_muxrate: str,
) -> None:
    """
    AV1 の FFmpeg 8 TS muxrate を各画質の必要最小 Level の T-STD Rx 以下に抑える。

    Args:
        monkeypatch (pytest.MonkeyPatch): テスト対象の依存を差し替える fixture。
        quality (QUALITY_TYPES): 処理または検証対象の画質。
        expected_muxrate (str): 期待する固定 TS muxrate。

    Returns:
        None
    """

    task = _buildEncodingTask(
        monkeypatch,
        video_codec='av1',
        video_bit_depth=8,
        audio_codec='aac',
    )

    options = task.buildFFmpeg8Options(quality, 'FFmpeg', 'GR', False)

    assert options[options.index('-muxrate') + 1] == expected_muxrate
    assert options[options.index('-pcr_period') + 1] == '20'


@pytest.mark.parametrize('encoder_type', ['FFmpeg', 'QSV', 'NVENC', 'AMF'])
def testOpusLiveAudioPreservesAllMappedTracksAndRequiresBridge(
    monkeypatch: pytest.MonkeyPatch,
    encoder_type: RecordedPlaybackEncoder,
) -> None:
    """Opus 指定では全音声 map を維持し、映像 codec に関係なく Bridge を要求する。"""

    task = _buildEncodingTask(
        monkeypatch,
        video_codec='avc',
        video_bit_depth=8,
        audio_codec='opus',
    )
    options = task.buildFFmpeg8Options('1080p', encoder_type, 'GR', False)
    audio_option = '-acodec' if encoder_type == 'FFmpeg' else '-c:a'

    assert _mapTargets(options) == ['0:v:0', '0:a?', '0:d?']
    assert [
        options[index + 1]
        for index, option in enumerate(options)
        if option == audio_option
    ] == ['libopus']
    assert options[options.index('-r') + 1] == '30000/1001'
    # Bridge の SELECTED_PCR_GAP を避けるため、Opus 付き AVC/HEVC も固定 muxrate を使う。
    assert '-muxrate' in options
    assert options[options.index('-pcr_period') + 1] == '20'
    assert task.isTSCodecBridgeRequired() is True


def testLegacyLiveCodecCombinationBypassesBridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """従来 AVC / AAC の明示指定でも Bridge を起動対象にしない。"""

    task = _buildEncodingTask(
        monkeypatch,
        video_codec='avc',
        video_bit_depth=8,
        audio_codec='aac',
    )

    options = task.buildFFmpeg8Options('1080p', 'FFmpeg', 'GR', False)

    assert task.isTSCodecBridgeRequired() is False
    assert '-muxrate' not in options
    assert '-pcr_period' not in options


def testRadioIgnoresVideoCodecButStillBridgesOpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """映像を持たないラジオでは高度映像codecを無視し、Opus音声だけをBridge対象にする。"""

    advanced_video_task = _buildEncodingTask(
        monkeypatch,
        video_codec='vp9',
        video_bit_depth=10,
        audio_codec='aac',
    )
    opus_task = _buildEncodingTask(
        monkeypatch,
        video_codec='vp9',
        video_bit_depth=10,
        audio_codec='opus',
    )

    assert advanced_video_task.isTSCodecBridgeRequired(is_radiochannel=True) is False
    assert opus_task.isTSCodecBridgeRequired(is_radiochannel=True) is True


@pytest.mark.parametrize(
    (
        'pipeline_output_started_at',
        'bridge_returncode',
        'bridge_required',
        'now',
        'expected',
    ),
    [
        (None, None, False, 100.0, False),
        (95.0, None, False, 100.0, False),
        (90.0, None, False, 100.0, True),
        (90.0, None, True, 100.0, True),
        (90.0, 1, True, 100.0, False),
    ],
)
def testRetryCountResetsOnlyAfterStableFinalTransport(
    pipeline_output_started_at: float | None,
    bridge_returncode: int | None,
    bridge_required: bool,
    now: float,
    expected: bool,
) -> None:
    """FFmpeg進捗だけでなく、Bridge後の最終TSが安定して初めてretry系列をリセットする。"""

    assert (
        LiveEncodingTask.canResetRetryCount(
            pipeline_output_started_at,
            bridge_returncode,
            bridge_required = bridge_required,
            now = now,
        )
        is expected
    )


def testRetryCountResetsOnceAfterStandbyTransitionsToONAir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ONAir遷移直後は保持し、最終TSが10秒安定した後の進捗行で一度だけresetする。"""

    task = _buildEncodingTask(monkeypatch)
    task._retry_count = 2
    status = SimpleNamespace(status = 'Standby')
    transitions: list[tuple[str, str]] = []

    def GetStatus() -> SimpleNamespace:
        """現在のテスト用ライブ状態を返す。"""

        return status

    def SetStatus(next_status: str, detail: str) -> bool:
        """状態遷移を記録し、テスト用ライブ状態へ反映する。"""

        status.status = next_status
        transitions.append((next_status, detail))
        return True

    task.live_stream.getStatus = GetStatus
    task.live_stream.setStatus = SetStatus

    # 最終TS開始から5秒の最初の有効進捗でONAirへ遷移しても、retry系列はまだ保持する。
    assert task.handleEncoderProgressLine(
        'frame=   30 fps=30.0 bitrate=1200.0kbits/s',
        95.0,
        None,
        bridge_required = True,
        now = 100.0,
    ) is False
    assert status.status == 'ONAir'
    assert task._retry_count == 2

    # ONAir後の進捗行も共通経路で判定し、最終TS開始から10秒で初めてresetする。
    assert task.handleEncoderProgressLine(
        'frame=  330 fps=30.0 bitrate=1200.0kbits/s',
        95.0,
        None,
        bridge_required = True,
        now = 105.0,
    ) is True
    assert task._retry_count == 0

    # 以後の進捗行ではreset済み系列を重複処理せず、ONAir状態も再設定しない。
    assert task.handleEncoderProgressLine(
        'frame=  360 fps=30.0 bitrate=1200.0kbits/s',
        95.0,
        None,
        bridge_required = True,
        now = 106.0,
    ) is False
    assert task._retry_count == 0
    assert transitions == [('ONAir', 'ライブストリームは ONAir です。')]

    # Restart決定後に遅れて届いた進捗行では、新しい障害系列の回数を消さない。
    status.status = 'Restart'
    task._retry_count = 1
    assert task.handleEncoderProgressLine(
        'frame=  390 fps=30.0 bitrate=1200.0kbits/s',
        95.0,
        None,
        bridge_required = True,
        now = 107.0,
    ) is False
    assert task._retry_count == 1


def testEncoderLogHistoryKeepsOnlyLatestOneHundredProgressLines() -> None:
    """100件を超えるFFmpeg進捗でも固定長を保ち、最新行を診断用に残す。"""

    lines: list[str] = []
    for index in range(150):
        progress_name = 'frame' if index % 2 == 0 else 'bitrate'
        LiveEncodingTask.appendBoundedEncoderLogLine(
            lines,
            f'{progress_name}={index}',
        )

    assert len(lines) == LiveEncodingTask.ENCODER_LOG_HISTORY_LIMIT == 100
    assert lines[0] == 'frame=50'
    assert lines[-1] == 'bitrate=149'
