import asyncio
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import Response

from app.routers import VideoStreamsRouter
from app.streams.KonomiTVBS4KPlaybackCapabilities import (
    KonomiTVBS4KPlaybackCapabilityProbe,
)
from app.streams.KonomiTVBS4KPlaybackEncoding import (
    KonomiTVBS4KAudioCodec,
    KonomiTVBS4KPlaybackAudioCapability,
    KonomiTVBS4KPlaybackCapabilities,
    KonomiTVBS4KPlaybackEncoder,
    KonomiTVBS4KPlaybackLiveCombinationCapability,
    KonomiTVBS4KPlaybackMode,
    KonomiTVBS4KPlaybackVideoCapability,
    KonomiTVBS4KVideoBitDepth,
    KonomiTVBS4KVideoCodec,
)
from app.streams.RecordedPlaybackCapabilities import (
    RecordedPlaybackCapability,
    RecordedPlaybackCapabilityProbe,
)
from app.streams.StreamEncodingOptions import (
    StreamEncodingOptions,
    StreamQualityWithOptions,
)


@pytest.fixture(autouse = True)
def RunThreadWorkInlineOnHost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """終了不能なhost default executorを避け、to_threadへ渡す同期処理本体を検証する。"""

    async def RunInline(
        function: Callable[..., object],
        /,
        *args: object,
        **kwargs: object,
    ) -> object:
        """能力検査がworkerへ委譲する同期処理を、この単体試験内だけ直接実行する。"""

        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, 'to_thread', RunInline)


def test_targeted_capabilities_probe_only_requested_exact_and_avc_aac_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """優先depth成功後は低優先depthを止め、fallbackはencoder実能力だけ確認する。"""

    recorded_calls: list[
        tuple[
            KonomiTVBS4KPlaybackEncoder,
            KonomiTVBS4KVideoCodec,
            KonomiTVBS4KVideoBitDepth,
        ]
    ] = []
    live_calls: list[
        tuple[
            KonomiTVBS4KPlaybackEncoder,
            KonomiTVBS4KVideoCodec,
            KonomiTVBS4KVideoBitDepth,
            KonomiTVBS4KAudioCodec,
        ]
    ] = []

    async def GetCapability(
        _cls: type[RecordedPlaybackCapabilityProbe],
        encoder: KonomiTVBS4KPlaybackEncoder,
        codec: KonomiTVBS4KVideoCodec,
        bit_depth: KonomiTVBS4KVideoBitDepth,
    ) -> RecordedPlaybackCapability:
        """要求された録画映像能力を利用可能として返す。"""

        recorded_calls.append((encoder, codec, bit_depth))
        return RecordedPlaybackCapability(
            encoder = encoder,
            codec = codec,
            bit_depth = bit_depth,
            available = True,
            profile = 'High' if codec == 'avc' else 'Main',
            reason_code = None,
        )

    async def IsLiveTransportAvailable(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
        encoder: KonomiTVBS4KPlaybackEncoder,
        codec: KonomiTVBS4KVideoCodec,
        bit_depth: KonomiTVBS4KVideoBitDepth,
        audio_codec: KonomiTVBS4KAudioCodec,
    ) -> bool:
        """映像付き高度codecの実pipe probe呼び出しを記録する。"""

        live_calls.append((encoder, codec, bit_depth, audio_codec))
        return True

    async def UnexpectedGetCapabilities(
        _cls: type[RecordedPlaybackCapabilityProbe],
    ) -> list[RecordedPlaybackCapability]:
        """全32行の録画能力検査を呼んだ場合にテストを失敗させる。"""

        raise AssertionError('targeted API must not request the full recorded matrix')

    async def UnexpectedRadioProbe(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
    ) -> bool:
        """映像付きOpusでradio probeを重複起動した場合にテストを失敗させる。"""

        raise AssertionError('video playback must not start the radio Opus probe')

    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        'getCapability',
        classmethod(GetCapability),
    )
    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        'getCapabilities',
        classmethod(UnexpectedGetCapabilities),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isFFmpegAvailable',
        classmethod(lambda _cls: True),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isBridgeAvailable',
        classmethod(lambda _cls: True),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isLiveTransportAvailable',
        classmethod(IsLiveTransportAvailable),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isRadioOpusTransportAvailable',
        classmethod(UnexpectedRadioProbe),
    )

    capabilities = asyncio.run(
        KonomiTVBS4KPlaybackCapabilityProbe.getTargetedCapabilities(
            'QSV',
            'Live',
            'av1',
            (10, 8),
            'opus',
            True,
        )
    )

    assert recorded_calls == [('QSV', 'av1', 10), ('QSV', 'avc', 8)]
    assert live_calls == [('QSV', 'av1', 10, 'opus')]
    assert [
        (item.codec, item.bit_depth)
        for item in capabilities.video
    ] == [('av1', 10), ('avc', 8)]
    assert [item.codec for item in capabilities.audio] == ['opus', 'aac']
    assert [
        (
            item.video_codec,
            item.video_bit_depth,
            item.audio_codec,
            item.available,
        )
        for item in capabilities.live_combinations
    ] == [
        ('av1', 10, 'opus', True),
        ('avc', 8, 'aac', True),
    ]


def test_targeted_capabilities_try_next_depth_after_exact_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """優先depthの実搬送だけが失敗した場合は、次のdepthまで順番に検査する。"""

    recorded_calls: list[tuple[str, str, int]] = []
    live_calls: list[tuple[str, str, int, str]] = []

    async def GetCapability(
        _cls: type[RecordedPlaybackCapabilityProbe],
        encoder: KonomiTVBS4KPlaybackEncoder,
        codec: KonomiTVBS4KVideoCodec,
        bit_depth: KonomiTVBS4KVideoBitDepth,
    ) -> RecordedPlaybackCapability:
        recorded_calls.append((encoder, codec, bit_depth))
        return RecordedPlaybackCapability(
            encoder,
            codec,
            bit_depth,
            True,
            'Main',
            None,
        )

    async def IsLiveTransportAvailable(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
        encoder: KonomiTVBS4KPlaybackEncoder,
        codec: KonomiTVBS4KVideoCodec,
        bit_depth: KonomiTVBS4KVideoBitDepth,
        audio_codec: KonomiTVBS4KAudioCodec,
    ) -> bool:
        live_calls.append((encoder, codec, bit_depth, audio_codec))
        return bit_depth == 8

    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        'getCapability',
        classmethod(GetCapability),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isFFmpegAvailable',
        classmethod(lambda _cls: True),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isBridgeAvailable',
        classmethod(lambda _cls: True),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isLiveTransportAvailable',
        classmethod(IsLiveTransportAvailable),
    )

    capabilities = asyncio.run(
        KonomiTVBS4KPlaybackCapabilityProbe.getTargetedCapabilities(
            'QSV',
            'Live',
            'av1',
            (10, 8),
            'opus',
            True,
        )
    )

    assert recorded_calls == [('QSV', 'av1', 10), ('QSV', 'av1', 8), ('QSV', 'avc', 8)]
    assert live_calls == [
        ('QSV', 'av1', 10, 'opus'),
        ('QSV', 'av1', 8, 'opus'),
    ]
    assert [
        (
            item.video_codec,
            item.video_bit_depth,
            item.audio_codec,
            item.available,
        )
        for item in capabilities.live_combinations
    ] == [
        ('av1', 10, 'opus', False),
        ('av1', 8, 'opus', True),
        ('avc', 8, 'aac', True),
    ]


def test_targeted_recorded_capabilities_do_not_require_live_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """録画再生は録画エンコーダー能力だけを使い、Bridge 不在でも成立する。"""

    recorded_calls: list[tuple[str, str, int]] = []

    async def GetCapability(
        _cls: type[RecordedPlaybackCapabilityProbe],
        encoder: KonomiTVBS4KPlaybackEncoder,
        codec: KonomiTVBS4KVideoCodec,
        bit_depth: KonomiTVBS4KVideoBitDepth,
    ) -> RecordedPlaybackCapability:
        """要求された録画映像能力を利用可能として返す。"""

        recorded_calls.append((encoder, codec, bit_depth))
        return RecordedPlaybackCapability(
            encoder = encoder,
            codec = codec,
            bit_depth = bit_depth,
            available = True,
            profile = 'Main',
            reason_code = None,
        )

    async def UnexpectedLiveProbe(*_args: object, **_kwargs: object) -> bool:
        """録画 targeted 検査がライブ搬送を調べた場合に失敗させる。"""

        raise AssertionError('recorded playback must not start a live transport probe')

    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        'getCapability',
        classmethod(GetCapability),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isFFmpegAvailable',
        classmethod(lambda _cls: True),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'getLiveCombinationCapability',
        classmethod(UnexpectedLiveProbe),
    )

    capabilities = asyncio.run(
        KonomiTVBS4KPlaybackCapabilityProbe.getTargetedCapabilities(
            'QSV',
            'Video',
            'av1',
            (10, 8),
            'opus',
            True,
        )
    )

    assert recorded_calls == [('QSV', 'av1', 10), ('QSV', 'avc', 8)]
    assert [
        (item.codec, item.bit_depth, item.recorded_available)
        for item in capabilities.video
    ] == [('av1', 10, True), ('avc', 8, True)]
    assert [
        (item.codec, item.recorded_available)
        for item in capabilities.audio
    ] == [('opus', True), ('aac', True)]
    assert capabilities.live_combinations == ()

    audio_only_capabilities = asyncio.run(
        KonomiTVBS4KPlaybackCapabilityProbe.getTargetedCapabilities(
            'QSV',
            'Video',
            'av1',
            (10, 8),
            'opus',
            False,
        )
    )
    assert recorded_calls == [('QSV', 'av1', 10), ('QSV', 'avc', 8)]
    assert audio_only_capabilities.video == ()
    assert [
        (item.codec, item.recorded_available)
        for item in audio_only_capabilities.audio
    ] == [('opus', True), ('aac', True)]
    assert audio_only_capabilities.live_combinations == ()


def test_full_capabilities_probe_advanced_rows_with_bounded_parallelism(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全行列のlive実probeを二件ずつ進め、従来の完全直列へ戻さない。"""

    active = 0
    maximum_active = 0
    probe_calls: list[tuple[str, str, int, str]] = []
    two_probes_started = asyncio.Event()

    async def GetCapabilities(
        _cls: type[RecordedPlaybackCapabilityProbe],
    ) -> list[RecordedPlaybackCapability]:
        return [
            RecordedPlaybackCapability('FFmpeg', 'av1', 8, True, 'Main', None),
            RecordedPlaybackCapability('QSV', 'vp9', 10, True, 'Profile 2', None),
        ]

    async def RunLiveTransportProbe(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
        encoder: KonomiTVBS4KPlaybackEncoder,
        codec: KonomiTVBS4KVideoCodec,
        bit_depth: KonomiTVBS4KVideoBitDepth,
        audio_codec: KonomiTVBS4KAudioCodec,
    ) -> bool:
        nonlocal active, maximum_active
        probe_calls.append((encoder, codec, bit_depth, audio_codec))
        active += 1
        maximum_active = max(maximum_active, active)
        if active == 2:
            two_probes_started.set()
        try:
            await asyncio.wait_for(two_probes_started.wait(), timeout = 1)
            await asyncio.sleep(0)
            return True
        finally:
            active -= 1

    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        'getCapabilities',
        classmethod(GetCapabilities),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isFFmpegAvailable',
        classmethod(lambda _cls: True),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isBridgeAvailable',
        classmethod(lambda _cls: True),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isRadioOpusTransportAvailable',
        classmethod(lambda _cls: asyncio.sleep(0, result = True)),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        '_KonomiTVBS4KPlaybackCapabilityProbe__getLiveProbeSignature',
        classmethod(lambda _cls: 'bounded-full-matrix'),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        '_KonomiTVBS4KPlaybackCapabilityProbe__runLiveTransportProbe',
        classmethod(RunLiveTransportProbe),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe, '_live_probe_signature', None
    )
    monkeypatch.setattr(KonomiTVBS4KPlaybackCapabilityProbe, '_live_probe_results', {})
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        '_live_probe_failure_timestamps',
        {},
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe, '_live_probe_event_loop', None
    )

    capabilities = asyncio.run(
        KonomiTVBS4KPlaybackCapabilityProbe.getCapabilities()
    )

    assert len(capabilities.live_combinations) == 4
    assert len(probe_calls) == 4
    assert maximum_active == 2


def test_targeted_legacy_avc_aac_counter_read_never_starts_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AVC 8bit/AAC APIもencoder/Bridgeをfail closed検査し、実encode probeは起動しない。"""

    recorded_calls: list[tuple[str, str, int]] = []

    async def GetCapability(
        _cls: type[RecordedPlaybackCapabilityProbe],
        encoder: KonomiTVBS4KPlaybackEncoder,
        codec: KonomiTVBS4KVideoCodec,
        bit_depth: KonomiTVBS4KVideoBitDepth,
    ) -> RecordedPlaybackCapability:
        recorded_calls.append((encoder, codec, bit_depth))
        return RecordedPlaybackCapability(encoder, codec, bit_depth, True, 'High', None)

    async def UnexpectedTransportProbe(*_args: object, **_kwargs: object) -> None:
        raise AssertionError('baseline AVC/AAC must not start an advanced transport probe')

    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        'getCapability',
        classmethod(GetCapability),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isFFmpegAvailable',
        classmethod(lambda _cls: True),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isBridgeAvailable',
        classmethod(lambda _cls: True),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'isLiveTransportAvailable',
        classmethod(UnexpectedTransportProbe),
    )
    monkeypatch.setattr(
        VideoStreamsRouter.TSCodecBridgeRuntimeVerifier,
        '_process_generation',
        'legacy-counter-generation',
    )
    monkeypatch.setattr(
        VideoStreamsRouter.TSCodecBridgeRuntimeVerifier,
        '_process_start_count',
        23,
    )
    monkeypatch.setattr(
        VideoStreamsRouter.TSCodecBridgeRuntimeVerifier,
        '_live_process_start_count',
        11,
    )
    monkeypatch.setattr(
        VideoStreamsRouter.TSCodecBridgeRuntimeVerifier,
        '_probe_process_start_count',
        8,
    )
    monkeypatch.setattr(
        VideoStreamsRouter.TSCodecBridgeRuntimeVerifier,
        '_verification_process_start_count',
        4,
    )
    monkeypatch.setattr(
        VideoStreamsRouter,
        'Config',
        lambda: SimpleNamespace(
            general=SimpleNamespace(
                konomitv_bs4k_acceptance_diagnostics_enabled=True,
            ),
        ),
    )

    api_response = Response()
    capabilities = asyncio.run(
        VideoStreamsRouter.KonomiTVBS4KTargetedPlaybackCapabilitiesAPI(
            api_response,
            'NVENC',
            'Live',
            'avc',
            '8',
            'aac',
            True,
        )
    )

    assert (
        api_response.headers['X-KonomiTV-BS4K-TSCodecBridge-Process-Generation']
        == 'legacy-counter-generation'
    )
    assert (
        api_response.headers['X-KonomiTV-BS4K-TSCodecBridge-Process-Start-Count']
        == '23'
    )
    assert (
        api_response.headers['X-KonomiTV-BS4K-TSCodecBridge-Live-Process-Start-Count']
        == '11'
    )
    assert (
        api_response.headers['X-KonomiTV-BS4K-TSCodecBridge-Probe-Process-Start-Count']
        == '8'
    )
    assert (
        api_response.headers[
            'X-KonomiTV-BS4K-TSCodecBridge-Verification-Process-Start-Count'
        ]
        == '4'
    )
    assert (
        VideoStreamsRouter.TSCodecBridgeRuntimeVerifier.getProcessStartSnapshot()
        == ('legacy-counter-generation', 23, 11, 8, 4)
    )
    assert [
        (
            item.video_codec,
            item.video_bit_depth,
            item.audio_codec,
            item.available,
        )
        for item in capabilities.live_combinations
    ] == [('avc', 8, 'aac', True)]
    assert recorded_calls == [('NVENC', 'avc', 8)]


def test_targeted_audio_only_capabilities_skip_every_video_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ラジオと音声のみ録画は要求音声とAACだけを調べ、映像backendを起動しない。"""

    audio_calls: list[KonomiTVBS4KAudioCodec] = []

    async def GetAudioCapability(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
        codec: KonomiTVBS4KAudioCodec,
    ) -> KonomiTVBS4KPlaybackAudioCapability:
        """要求音声のaudio-only能力を返す。"""

        audio_calls.append(codec)
        return KonomiTVBS4KPlaybackAudioCapability(
            codec = codec,
            live_available = True,
            recorded_available = True,
            live_reason_code = None,
            recorded_reason_code = None,
        )

    async def UnexpectedRecordedProbe(*_args: object, **_kwargs: object) -> None:
        """映像能力を参照した場合にテストを失敗させる。"""

        raise AssertionError('audio-only playback must not probe a video encoder')

    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'getAudioCapability',
        classmethod(GetAudioCapability),
    )
    monkeypatch.setattr(
        RecordedPlaybackCapabilityProbe,
        'getCapability',
        classmethod(UnexpectedRecordedProbe),
    )

    capabilities = asyncio.run(
        KonomiTVBS4KPlaybackCapabilityProbe.getTargetedCapabilities(
            'NVENC',
            'Live',
            'av1',
            (10, 8),
            'opus',
            False,
        )
    )

    assert audio_calls == ['opus', 'aac']
    assert capabilities.video == ()
    assert [item.codec for item in capabilities.audio] == ['opus', 'aac']
    assert capabilities.live_combinations == ()




def test_pending_recorded_index_revalidates_explicit_codec_before_stream_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending録画はindex生成後の最新メタデータで非対応codecを422拒否する。"""

    recorded_video_calls: list[tuple[str, str, int, str | None]] = []
    recorded_program = SimpleNamespace(
        network_id = 0x000B,
        recorded_video = SimpleNamespace(
            playback_index_status = 'Pending',
            playback_index_version = None,
            has_video = False,
        ),
    )
    stream_quality = StreamQualityWithOptions(
        quality = '1080p',
        encoding_options = StreamEncodingOptions(
            video_codec = 'av1',
            video_bit_depth = 10,
            audio_codec = 'aac',
        ),
        is_video_encoding_explicitly_requested = True,
    )

    async def EnsurePlaybackIndexReady(
        target_recorded_program: SimpleNamespace,
        session_id: str,
    ) -> None:
        """Pending録画をReadyへ更新し、index解析後に判明した映像ありを反映する。"""

        assert target_recorded_program is recorded_program
        assert session_id == 'pending-session'
        target_recorded_program.recorded_video.playback_index_status = 'Ready'
        target_recorded_program.recorded_video.playback_index_version = 7
        target_recorded_program.recorded_video.has_video = True

    async def GetRecordedVideoCapability(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
        encoder: KonomiTVBS4KPlaybackEncoder,
        codec: KonomiTVBS4KVideoCodec,
        bit_depth: KonomiTVBS4KVideoBitDepth,
        *,
        quality: str | None = None,
    ) -> KonomiTVBS4KPlaybackVideoCapability:
        """index生成後のexact映像能力を非対応として返す。"""

        recorded_video_calls.append((encoder, codec, bit_depth, quality))
        return KonomiTVBS4KPlaybackVideoCapability(
            encoder = encoder,
            codec = codec,
            bit_depth = bit_depth,
            profile = 'Main',
            live_available = False,
            recorded_available = False,
            live_reason_code = 'UnsupportedCombination',
            recorded_reason_code = 'UnsupportedCombination',
        )

    def UnexpectedGetRecordedStream(*_args: object, **_kwargs: object) -> None:
        """422拒否後に録画セッション生成へ進んだ場合は失敗させる。"""

        raise AssertionError('unsupported explicit codec must not create a recorded stream')

    monkeypatch.setattr(
        VideoStreamsRouter,
        'EnsurePlaybackIndexReady',
        EnsurePlaybackIndexReady,
    )
    monkeypatch.setattr(
        VideoStreamsRouter,
        'Config',
        lambda: SimpleNamespace(
            general = SimpleNamespace(encoder = 'FFmpeg', encoder_bs4k = 'QSV')
        ),
    )
    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'getRecordedVideoCapability',
        classmethod(GetRecordedVideoCapability),
    )
    monkeypatch.setattr(
        VideoStreamsRouter,
        'GetRecordedStream',
        UnexpectedGetRecordedStream,
    )

    with pytest.raises(HTTPException) as ex_info:
        asyncio.run(
            VideoStreamsRouter.VideoHLSPlaylistAPI(
                SimpleNamespace(),
                recorded_program,
                stream_quality,
                'pending-session',
                None,
            )
        )

    assert ex_info.value.status_code == 422
    assert ex_info.value.detail == {
        'code': 'UnsupportedCombination',
        'message': 'The requested recorded encoding is unavailable.',
    }
    assert recorded_video_calls == [('QSV', 'av1', 10, '1080p')]


def test_targeted_capability_api_serializes_partial_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """targeted APIはqueryを内部型へ変換し、既存能力schemaで部分行列を返す。"""

    requested: list[
        tuple[
            KonomiTVBS4KPlaybackEncoder,
            KonomiTVBS4KPlaybackMode,
            KonomiTVBS4KVideoCodec,
            tuple[KonomiTVBS4KVideoBitDepth, ...],
            KonomiTVBS4KAudioCodec,
            bool,
        ]
    ] = []

    async def GetTargetedCapabilities(
        _cls: type[KonomiTVBS4KPlaybackCapabilityProbe],
        encoder: KonomiTVBS4KPlaybackEncoder,
        playback_mode: KonomiTVBS4KPlaybackMode,
        video_codec: KonomiTVBS4KVideoCodec,
        video_bit_depths: tuple[KonomiTVBS4KVideoBitDepth, ...],
        audio_codec: KonomiTVBS4KAudioCodec,
        has_video: bool,
    ) -> KonomiTVBS4KPlaybackCapabilities:
        """APIから受け取ったtargeted条件を記録して固定応答を返す。"""

        requested.append(
            (encoder, playback_mode, video_codec, video_bit_depths, audio_codec, has_video)
        )
        return KonomiTVBS4KPlaybackCapabilities(
            video = (
                KonomiTVBS4KPlaybackVideoCapability(
                    encoder = 'QSV',
                    codec = 'av1',
                    bit_depth = 10,
                    profile = 'Main',
                    live_available = False,
                    recorded_available = True,
                    live_reason_code = 'ProbeFailed',
                    recorded_reason_code = None,
                ),
            ),
            audio = (
                KonomiTVBS4KPlaybackAudioCapability(
                    codec = 'opus',
                    live_available = False,
                    recorded_available = True,
                    live_reason_code = 'ProbeFailed',
                    recorded_reason_code = None,
                ),
            ),
            live_combinations = (
                KonomiTVBS4KPlaybackLiveCombinationCapability(
                    encoder = 'QSV',
                    video_codec = 'av1',
                    video_bit_depth = 10,
                    audio_codec = 'opus',
                    available = False,
                    reason_code = 'ProbeFailed',
                ),
            ),
        )

    monkeypatch.setattr(
        KonomiTVBS4KPlaybackCapabilityProbe,
        'getTargetedCapabilities',
        classmethod(GetTargetedCapabilities),
    )
    monkeypatch.setattr(
        VideoStreamsRouter.TSCodecBridgeRuntimeVerifier,
        'getProcessStartSnapshot',
        classmethod(lambda _cls: ('targeted-api-generation', 73, 31, 22, 20)),
    )
    monkeypatch.setattr(
        VideoStreamsRouter,
        'Config',
        lambda: SimpleNamespace(
            general=SimpleNamespace(
                konomitv_bs4k_acceptance_diagnostics_enabled=True,
            ),
        ),
    )

    api_response = Response()
    response = asyncio.run(
        VideoStreamsRouter.KonomiTVBS4KTargetedPlaybackCapabilitiesAPI(
            api_response,
            'QSV',
            'Live',
            'av1',
            '10,8',
            'opus',
            True,
        )
    )
    assert (
        api_response.headers['X-KonomiTV-BS4K-TSCodecBridge-Process-Generation']
        == 'targeted-api-generation'
    )
    assert (
        api_response.headers['X-KonomiTV-BS4K-TSCodecBridge-Process-Start-Count']
        == '73'
    )
    assert (
        api_response.headers['X-KonomiTV-BS4K-TSCodecBridge-Live-Process-Start-Count']
        == '31'
    )
    assert (
        api_response.headers['X-KonomiTV-BS4K-TSCodecBridge-Probe-Process-Start-Count']
        == '22'
    )
    assert (
        api_response.headers[
            'X-KonomiTV-BS4K-TSCodecBridge-Verification-Process-Start-Count'
        ]
        == '20'
    )

    assert requested == [('QSV', 'Live', 'av1', (10, 8), 'opus', True)]
    assert response.model_dump() == {
        'video': [{
            'encoder': 'QSV',
            'codec': 'av1',
            'bit_depth': 10,
            'profile': 'Main',
            'live_available': False,
            'recorded_available': True,
            'live_reason_code': 'ProbeFailed',
            'recorded_reason_code': None,
        }],
        'audio': [{
            'codec': 'opus',
            'live_available': False,
            'recorded_available': True,
            'live_reason_code': 'ProbeFailed',
            'recorded_reason_code': None,
        }],
        'live_combinations': [{
            'encoder': 'QSV',
            'video_codec': 'av1',
            'video_bit_depth': 10,
            'audio_codec': 'opus',
            'available': False,
            'reason_code': 'ProbeFailed',
        }],
    }

    route_paths = {route.path for route in VideoStreamsRouter.router.routes}
    assert (
        '/api/streams/video/konomitv-bs4k-playback-capabilities/targeted'
        in route_paths
    )
