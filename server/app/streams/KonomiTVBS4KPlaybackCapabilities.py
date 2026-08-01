from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path
from typing import ClassVar, cast

from biim.mpeg2ts import ts
from biim.mpeg2ts.parser import SectionParser
from biim.mpeg2ts.pat import PATSection
from biim.mpeg2ts.pmt import PMTSection

from app import logging
from app.constants import LIBRARY_PATH
from app.streams.KonomiTVBS4KPlaybackEncoding import (
    KONOMITV_BS4K_AUDIO_CODECS,
    IsKonomiTVBS4KVideoCodecBitDepthSupported,
    KonomiTVBS4KAudioCodec,
    KonomiTVBS4KPlaybackAudioCapability,
    KonomiTVBS4KPlaybackCapabilities,
    KonomiTVBS4KPlaybackCapabilityReason,
    KonomiTVBS4KPlaybackEncoder,
    KonomiTVBS4KPlaybackLiveCombinationCapability,
    KonomiTVBS4KPlaybackVideoCapability,
    KonomiTVBS4KVideoBitDepth,
    KonomiTVBS4KVideoCodec,
    ParseKonomiTVBS4KAdvancedLiveMuxrateKbps,
    ResolveKonomiTVBS4KAdvancedLiveMuxrate,
)
from app.streams.RecordedPlaybackCapabilities import (
    RecordedPlaybackBackend,
    RecordedPlaybackCapability,
    RecordedPlaybackCapabilityProbe,
)
from app.streams.TSCodecBridgeRuntime import TSCodecBridgeRuntimeVerifier


class KonomiTVBS4KPlaybackCapabilityProbe:
    """録画の実probeとライブTS経路の依存を分離して共通能力契約を作る。"""

    BRIDGE_LIBRARY_PATH_KEY = 'KonomiTVBS4KTSCodecBridge'
    _live_probe_version: ClassVar[int] = 7
    _live_probe_signature: ClassVar[str | None] = None
    _live_probe_results: ClassVar[
        dict[
            tuple[
                KonomiTVBS4KPlaybackEncoder,
                KonomiTVBS4KVideoCodec,
                KonomiTVBS4KVideoBitDepth,
                KonomiTVBS4KAudioCodec,
            ],
            bool,
        ]
    ] = {}
    _live_probe_failure_timestamps: ClassVar[
        dict[
            tuple[
                KonomiTVBS4KPlaybackEncoder,
                KonomiTVBS4KVideoCodec,
                KonomiTVBS4KVideoBitDepth,
                KonomiTVBS4KAudioCodec,
            ],
            float,
        ]
    ] = {}
    _radio_opus_probe_result: ClassVar[bool | None] = None
    _radio_opus_probe_failure_timestamp: ClassVar[float | None] = None
    _live_probe_tasks: ClassVar[
        dict[
            tuple[
                str,
                KonomiTVBS4KPlaybackEncoder,
                KonomiTVBS4KVideoCodec,
                KonomiTVBS4KVideoBitDepth,
                KonomiTVBS4KAudioCodec,
            ],
            asyncio.Task[bool],
        ]
    ] = {}
    _radio_opus_probe_tasks: ClassVar[dict[str, asyncio.Task[bool]]] = {}
    _live_probe_event_loop: ClassVar[asyncio.AbstractEventLoop | None] = None
    _live_probe_lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    _live_probe_semaphore: ClassVar[asyncio.Semaphore] = asyncio.Semaphore(2)
    _negative_probe_ttl_seconds: ClassVar[float] = 5.0

    @classmethod
    async def getCapabilities(cls) -> KonomiTVBS4KPlaybackCapabilities:
        """映像・音声codecのライブ・録画能力を返す。"""

        is_ffmpeg_available = cls.isFFmpegAvailable()
        is_bridge_available = (
            await cls.isBridgeAvailableAsync()
            if is_ffmpeg_available is True
            else False
        )
        recorded_capabilities = await RecordedPlaybackCapabilityProbe.getCapabilities()
        # 全行列 API でも実 probe を一件ずつ直列実行しない。isLiveTransportAvailable() 側で
        # 同一キーを共有しつつ実プロセス数を2件へ制限するため、ここでは応答順を保ったまま並行化する。
        combination_targets: list[
            tuple[RecordedPlaybackCapability, KonomiTVBS4KAudioCodec]
        ] = [
            (capability, audio_codec)
            for capability in recorded_capabilities
            for audio_codec in KONOMITV_BS4K_AUDIO_CODECS
        ]
        live_combinations = list(await asyncio.gather(*(
            cls.__buildLiveCombinationCapability(
                capability,
                audio_codec,
                is_ffmpeg_available = is_ffmpeg_available,
                is_bridge_available = is_bridge_available,
            )
            for capability, audio_codec in combination_targets
        )))

        video_capabilities: list[KonomiTVBS4KPlaybackVideoCapability] = []
        for capability in recorded_capabilities:
            matching_live_combinations = [
                combination
                for combination in live_combinations
                if combination.encoder == capability.encoder
                and combination.video_codec == capability.codec
                and combination.video_bit_depth == capability.bit_depth
            ]
            live_available = any(
                combination.available is True
                for combination in matching_live_combinations
            )
            live_reason_code: KonomiTVBS4KPlaybackCapabilityReason | None = None
            if live_available is False:
                live_reason_code = capability.reason_code
                for combination in matching_live_combinations:
                    if combination.reason_code is not None:
                        live_reason_code = combination.reason_code
                        break
            video_capabilities.append(
                KonomiTVBS4KPlaybackVideoCapability(
                    encoder = capability.encoder,
                    codec = capability.codec,
                    bit_depth = capability.bit_depth,
                    profile = capability.profile,
                    live_available = live_available,
                    recorded_available = capability.available,
                    live_reason_code = live_reason_code,
                    recorded_reason_code = capability.reason_code,
                )
            )

        binary_reason: KonomiTVBS4KPlaybackCapabilityReason | None = (
            None if is_ffmpeg_available else 'BinaryUnavailable'
        )
        # 音声能力は映像付き組み合わせ行列から推測せず、ラジオと同じ audio-only topology を正本にする。
        ## 依存バイナリ不在では、実 pipe probe を起動する前に fail closed とする。
        is_opus_live_transport_available = False
        opus_live_reason: KonomiTVBS4KPlaybackCapabilityReason | None
        if is_ffmpeg_available is False:
            opus_live_reason = 'BinaryUnavailable'
        elif is_bridge_available is False:
            opus_live_reason = 'BridgeUnavailable'
        else:
            is_opus_live_transport_available = await cls.isRadioOpusTransportAvailable()
            opus_live_reason = (
                None if is_opus_live_transport_available is True else 'ProbeFailed'
            )
        audio_capabilities = (
            KonomiTVBS4KPlaybackAudioCapability(
                codec = 'aac',
                live_available = is_ffmpeg_available,
                recorded_available = is_ffmpeg_available,
                live_reason_code = binary_reason,
                recorded_reason_code = binary_reason,
            ),
            KonomiTVBS4KPlaybackAudioCapability(
                codec = 'opus',
                live_available = is_opus_live_transport_available,
                recorded_available = is_ffmpeg_available,
                live_reason_code = opus_live_reason,
                recorded_reason_code = binary_reason,
            ),
        )
        return KonomiTVBS4KPlaybackCapabilities(
            video = tuple(video_capabilities),
            audio = audio_capabilities,
            live_combinations = tuple(live_combinations),
        )

    @classmethod
    async def getTargetedCapabilities(
        cls,
        encoder: KonomiTVBS4KPlaybackEncoder,
        video_codec: KonomiTVBS4KVideoCodec,
        video_bit_depths: tuple[KonomiTVBS4KVideoBitDepth, ...],
        audio_codec: KonomiTVBS4KAudioCodec,
        has_video: bool,
    ) -> KonomiTVBS4KPlaybackCapabilities:
        """
        再生開始に必要なexact行とAVC/AAC互換fallback行だけを検査する。

        Args:
            encoder: 現在のチャンネルまたは録画で実際に使うエンコーダー。
            video_codec: 保存設定から選ばれた映像コーデック。
            video_bit_depths: 現在の画質とブラウザで候補になるbit depthの優先順。
            audio_codec: 保存設定から選ばれた音声コーデック。
            has_video: 映像SourceBufferを使う再生対象ならTrue。

        Returns:
            現在の再生候補と互換fallbackだけを含む部分能力行列。
        """

        # ラジオと音声のみ録画では映像backendを一切起動せず、要求音声とAAC fallbackだけを検査する。
        if has_video is False:
            audio_only_codecs: tuple[KonomiTVBS4KAudioCodec, ...] = tuple(
                dict.fromkeys((audio_codec, 'aac'))
            )
            audio_only_capabilities = await asyncio.gather(*(
                cls.getAudioCapability(candidate_audio_codec)
                for candidate_audio_codec in audio_only_codecs
            ))
            return KonomiTVBS4KPlaybackCapabilities(
                video = (),
                audio = tuple(
                    capability
                    for capability in audio_only_capabilities
                    if capability is not None
                ),
                live_combinations = (),
            )

        # 要求codecのdepth候補は優先順に一つずつ調べ、成功後の低優先depthを起動しない。
        ## AVC 8bit/AAC fallbackは旧再生経路の既知構成なので、実encode probeなしで最後に付加する。
        requested_combinations: list[
            tuple[
                KonomiTVBS4KPlaybackEncoder,
                KonomiTVBS4KVideoCodec,
                KonomiTVBS4KVideoBitDepth,
                KonomiTVBS4KAudioCodec,
            ]
        ] = []
        for video_bit_depth in video_bit_depths:
            if IsKonomiTVBS4KVideoCodecBitDepthSupported(video_codec, video_bit_depth):
                requested_combinations.append(
                    (encoder, video_codec, video_bit_depth, audio_codec)
                )
        requested_combinations = list(dict.fromkeys(requested_combinations))
        fallback_target: tuple[
            KonomiTVBS4KPlaybackEncoder,
            KonomiTVBS4KVideoCodec,
            KonomiTVBS4KVideoBitDepth,
            KonomiTVBS4KAudioCodec,
        ] = (encoder, 'avc', 8, 'aac')
        probe_results: list[
            tuple[
                RecordedPlaybackCapability,
                KonomiTVBS4KPlaybackLiveCombinationCapability,
            ]
        ] = []
        for (
            target_encoder,
            target_video_codec,
            target_bit_depth,
            target_audio_codec,
        ) in requested_combinations:
            if (
                target_encoder,
                target_video_codec,
                target_bit_depth,
                target_audio_codec,
            ) == fallback_target:
                break
            recorded = await RecordedPlaybackCapabilityProbe.getCapability(
                target_encoder,
                target_video_codec,
                target_bit_depth,
            )
            combination = await cls.getLiveCombinationCapability(
                recorded.encoder,
                recorded.codec,
                recorded.bit_depth,
                target_audio_codec,
                recorded_capability = recorded,
            )
            probe_results.append((recorded, combination))
            if (
                combination.available is True
                or (
                    recorded.available is True
                    and combination.reason_code in (
                        'BinaryUnavailable',
                        'BridgeUnavailable',
                    )
                )
            ):
                break

        if not any(
            (
                recorded.encoder,
                recorded.codec,
                recorded.bit_depth,
                combination.audio_codec,
            ) == fallback_target
            for recorded, combination in probe_results
        ):
            is_fallback_recorded_available = cls.isFFmpegAvailable()
            fallback_recorded = RecordedPlaybackCapability(
                encoder = encoder,
                codec = 'avc',
                bit_depth = 8,
                available = is_fallback_recorded_available,
                profile = 'High',
                reason_code = (
                    None
                    if is_fallback_recorded_available is True
                    else 'BinaryUnavailable'
                ),
            )
            fallback_combination = KonomiTVBS4KPlaybackLiveCombinationCapability(
                encoder = encoder,
                video_codec = 'avc',
                video_bit_depth = 8,
                audio_codec = 'aac',
                available = True,
                reason_code = None,
            )
            probe_results.append((fallback_recorded, fallback_combination))

        # 映像能力は同じcodec/depthを音声ごとに重複させず、返したexact組の集約値として表現する。
        video_capabilities: list[KonomiTVBS4KPlaybackVideoCapability] = []
        video_keys: list[
            tuple[
                KonomiTVBS4KPlaybackEncoder,
                KonomiTVBS4KVideoCodec,
                KonomiTVBS4KVideoBitDepth,
            ]
        ] = []
        for recorded, _ in probe_results:
            key = (recorded.encoder, recorded.codec, recorded.bit_depth)
            if key not in video_keys:
                video_keys.append(key)
        for target_encoder, target_video_codec, target_bit_depth in video_keys:
            matching_results = [
                (recorded, combination)
                for recorded, combination in probe_results
                if (
                    recorded.encoder,
                    recorded.codec,
                    recorded.bit_depth,
                ) == (target_encoder, target_video_codec, target_bit_depth)
            ]
            recorded = matching_results[0][0]
            live_available = any(
                combination.available is True
                for _, combination in matching_results
            )
            live_reason_code: KonomiTVBS4KPlaybackCapabilityReason | None = (
                None if live_available else
                cast(
                    KonomiTVBS4KPlaybackCapabilityReason | None,
                    next(
                        (
                            combination.reason_code
                            for _, combination in matching_results
                            if combination.reason_code is not None
                        ),
                        None,
                    ),
                )
            )
            video_capabilities.append(
                KonomiTVBS4KPlaybackVideoCapability(
                    encoder = recorded.encoder,
                    codec = recorded.codec,
                    bit_depth = recorded.bit_depth,
                    profile = recorded.profile,
                    live_available = live_available,
                    recorded_available = recorded.available,
                    live_reason_code = live_reason_code,
                    recorded_reason_code = recorded.reason_code,
                )
            )

        # 映像付き再生の音声行は同じexact組から導出し、Opus radio probeを重複して起動しない。
        ## recorded音声は固定FFmpegの有無、live音声は返した組み合わせ内の成功を表す。
        audio_capabilities: list[KonomiTVBS4KPlaybackAudioCapability] = []
        target_audio_codecs: tuple[KonomiTVBS4KAudioCodec, ...] = tuple(
            dict.fromkeys(
                combination.audio_codec
                for _, combination in probe_results
            )
        )
        is_ffmpeg_available = cls.isFFmpegAvailable()
        for target_audio_codec in target_audio_codecs:
            matching_combinations = [
                combination
                for _, combination in probe_results
                if combination.audio_codec == target_audio_codec
            ]
            live_available = any(
                combination.available is True
                for combination in matching_combinations
            )
            live_reason_code = (
                None if live_available else
                cast(
                    KonomiTVBS4KPlaybackCapabilityReason | None,
                    next(
                        (
                            combination.reason_code
                            for combination in matching_combinations
                            if combination.reason_code is not None
                        ),
                        None,
                    ),
                )
            )
            binary_reason: KonomiTVBS4KPlaybackCapabilityReason | None = (
                None if is_ffmpeg_available else 'BinaryUnavailable'
            )
            audio_capabilities.append(
                KonomiTVBS4KPlaybackAudioCapability(
                    codec = target_audio_codec,
                    live_available = live_available,
                    recorded_available = is_ffmpeg_available,
                    live_reason_code = live_reason_code,
                    recorded_reason_code = binary_reason,
                )
            )

        return KonomiTVBS4KPlaybackCapabilities(
            video = tuple(video_capabilities),
            audio = tuple(audio_capabilities),
            live_combinations = tuple(
                combination
                for _, combination in probe_results
            ),
        )

    @classmethod
    def isBridgeAvailable(cls) -> bool:
        """固定取得したBridge実行形式とruntime manifestが一致するか返す。"""

        bridge_path = LIBRARY_PATH.get(cls.BRIDGE_LIBRARY_PATH_KEY)
        return (
            bridge_path is not None
            and TSCodecBridgeRuntimeVerifier.isAvailable(bridge_path)
        )

    @classmethod
    async def isBridgeAvailableAsync(cls) -> bool:
        """SHA計算と版確認をworker threadへ移し、能力APIのevent loopを塞がない。"""

        return await asyncio.to_thread(cls.isBridgeAvailable)

    @classmethod
    def isFFmpegAvailable(cls) -> bool:
        """共通codecを生成する固定FFmpeg 8がruntimeに存在するか返す。"""

        return Path(LIBRARY_PATH['FFmpeg8']).is_file()

    @classmethod
    async def isLiveTransportAvailable(
        cls,
        encoder: KonomiTVBS4KPlaybackEncoder,
        video_codec: KonomiTVBS4KVideoCodec,
        bit_depth: KonomiTVBS4KVideoBitDepth,
        audio_codec: KonomiTVBS4KAudioCodec,
    ) -> bool:
        """
        FFmpeg 8 → TS Codec Bridge の実 pipe で短い合成 MPEG-TS を生成し、搬送識別を検証する。

        同一バイナリの組み合わせはプロセス内でキャッシュし、API 呼び出しごとの再エンコードを避ける。
        """

        lock, semaphore = cls.__getLiveProbeSynchronization()
        key = (encoder, video_codec, bit_depth, audio_codec)
        while True:
            async with lock:
                signature = cls.__resetLiveProbeCacheForCurrentSignature()
                cached = cls._live_probe_results.get(key)
                if cached is True:
                    return True
                if cached is False:
                    failure_timestamp = cls._live_probe_failure_timestamps.get(key)
                    if (
                        failure_timestamp is not None
                        and time.monotonic() - failure_timestamp
                        < cls._negative_probe_ttl_seconds
                    ):
                        return False
                    cls._live_probe_results.pop(key, None)
                    cls._live_probe_failure_timestamps.pop(key, None)

                task_key = (signature, encoder, video_codec, bit_depth, audio_codec)
                task = cls._live_probe_tasks.get(task_key)
                if task is None:
                    task = asyncio.create_task(
                        cls.__runLiveTransportProbeAndCache(
                            signature,
                            key,
                            lock,
                            semaphore,
                        ),
                        name = (
                            'KonomiTVBS4KPlaybackCapabilityProbe-live-'
                            f'{encoder}-{video_codec}-{bit_depth}-{audio_codec}'
                        ),
                    )
                    cls._live_probe_tasks[task_key] = task

            result = await asyncio.shield(task)
            async with lock:
                current_signature = cls.__resetLiveProbeCacheForCurrentSignature()
                if current_signature == signature:
                    return result

    @classmethod
    async def isRadioOpusTransportAvailable(cls) -> bool:
        """
        映像なし・二音声の FFmpeg 8 → TS Codec Bridge 実 pipe が Opus TS として成立するか返す。

        映像付きの exact combination probe と同じバイナリ署名・lock・cache 世代を共有する。

        Args:
            None

        Returns:
            bool: 映像なし・二音声の Opus TS が実 pipe で成立した場合は True。
        """

        lock, semaphore = cls.__getLiveProbeSynchronization()
        while True:
            async with lock:
                signature = cls.__resetLiveProbeCacheForCurrentSignature()
                if cls._radio_opus_probe_result is True:
                    return True
                if cls._radio_opus_probe_result is False:
                    if (
                        cls._radio_opus_probe_failure_timestamp is not None
                        and time.monotonic() - cls._radio_opus_probe_failure_timestamp
                        < cls._negative_probe_ttl_seconds
                    ):
                        return False
                    cls._radio_opus_probe_result = None
                    cls._radio_opus_probe_failure_timestamp = None

                task = cls._radio_opus_probe_tasks.get(signature)
                if task is None:
                    task = asyncio.create_task(
                        cls.__runRadioOpusTransportProbeAndCache(
                            signature,
                            lock,
                            semaphore,
                        ),
                        name = 'KonomiTVBS4KPlaybackCapabilityProbe-radio-opus',
                    )
                    cls._radio_opus_probe_tasks[signature] = task

            result = await asyncio.shield(task)
            async with lock:
                current_signature = cls.__resetLiveProbeCacheForCurrentSignature()
                if current_signature == signature:
                    return result

    @classmethod
    def __getLiveProbeSynchronization(
        cls,
    ) -> tuple[asyncio.Lock, asyncio.Semaphore]:
        """実行中 event loop ごとに probe の共有 lock と同時実行上限を返す。"""

        event_loop = asyncio.get_running_loop()
        if cls._live_probe_event_loop is not event_loop:
            cls._live_probe_event_loop = event_loop
            cls._live_probe_lock = asyncio.Lock()
            cls._live_probe_semaphore = asyncio.Semaphore(2)
            cls._live_probe_tasks.clear()
            cls._radio_opus_probe_tasks.clear()
        return cls._live_probe_lock, cls._live_probe_semaphore

    @classmethod
    def __resetLiveProbeCacheForCurrentSignature(cls) -> str:
        """固定バイナリ署名が変わった場合に成功・失敗 cache を同時に破棄する。"""

        signature = cls.__getLiveProbeSignature()
        if cls._live_probe_signature == signature:
            return signature
        cls._live_probe_results.clear()
        cls._live_probe_failure_timestamps.clear()
        cls._radio_opus_probe_result = None
        cls._radio_opus_probe_failure_timestamp = None
        cls._live_probe_signature = signature
        return signature

    @classmethod
    async def __runLiveTransportProbeAndCache(
        cls,
        signature: str,
        key: tuple[
            KonomiTVBS4KPlaybackEncoder,
            KonomiTVBS4KVideoCodec,
            KonomiTVBS4KVideoBitDepth,
            KonomiTVBS4KAudioCodec,
        ],
        lock: asyncio.Lock,
        semaphore: asyncio.Semaphore,
    ) -> bool:
        """同一exact probeを共有し、安定した署名世代の結果だけをcacheする。"""

        task_key = (signature, *key)
        current_task = asyncio.current_task()
        try:
            async with semaphore:
                result = await cls.__runLiveTransportProbe(*key)
            async with lock:
                if cls.__getLiveProbeSignature() == signature:
                    cls._live_probe_results[key] = result
                    if result is False:
                        cls._live_probe_failure_timestamps[key] = time.monotonic()
                    else:
                        cls._live_probe_failure_timestamps.pop(key, None)
            return result
        finally:
            async with lock:
                if cls._live_probe_tasks.get(task_key) is current_task:
                    cls._live_probe_tasks.pop(task_key, None)

    @classmethod
    async def __runRadioOpusTransportProbeAndCache(
        cls,
        signature: str,
        lock: asyncio.Lock,
        semaphore: asyncio.Semaphore,
    ) -> bool:
        """radio Opus probeを共有し、失敗だけ短時間cacheする。"""

        current_task = asyncio.current_task()
        try:
            async with semaphore:
                result = await cls.__runRadioOpusTransportProbe()
            async with lock:
                if cls.__getLiveProbeSignature() == signature:
                    cls._radio_opus_probe_result = result
                    cls._radio_opus_probe_failure_timestamp = (
                        time.monotonic() if result is False else None
                    )
            return result
        finally:
            async with lock:
                if cls._radio_opus_probe_tasks.get(signature) is current_task:
                    cls._radio_opus_probe_tasks.pop(signature, None)

    @classmethod
    def __getLiveProbeSignature(cls) -> str:
        """FFmpeg と Bridge の更新時にだけ probe cache を破棄する署名を返す。"""

        values = [str(cls._live_probe_version)]
        for key in ('FFmpeg8', 'FFmpeg8AMD', cls.BRIDGE_LIBRARY_PATH_KEY):
            path_value = LIBRARY_PATH.get(key)
            if path_value is None:
                values.append(f'{key}:missing')
                continue
            path = Path(path_value)
            try:
                stat = path.stat()
                values.append(f'{key}:{stat.st_size}:{stat.st_mtime_ns}')
            except OSError:
                values.append(f'{key}:missing')
        return hashlib.sha256(':'.join(values).encode()).hexdigest()

    @staticmethod
    def __collectLiveProbePMTStreams(
        output: bytes,
    ) -> dict[
        int,
        tuple[
            int,
            dict[int, tuple[int, list[tuple[int, bytes]]]],
        ],
    ]:
        """current PAT / PMTをprogram単位で保持し、PCR PIDとES mappingを返す。"""

        pat_parser = SectionParser(PATSection)
        pmt_parsers: dict[int, SectionParser[PMTSection]] = {}
        pmt_program_numbers: dict[int, int] = {}
        programs: dict[
            int,
            tuple[
                int,
                dict[int, tuple[int, list[tuple[int, bytes]]]],
            ],
        ] = {}
        pat_signature: tuple[int, tuple[tuple[int, int], ...]] | None = None
        try:
            for offset in range(0, len(output), 188):
                packet = output[offset:offset + 188]
                packet_pid = ts.pid(packet)
                if packet_pid == 0x00:
                    pat_parser.push(packet)
                    for pat in pat_parser:
                        if (
                            pat.CRC32() != 0
                            or pat.current_next_indicator() is False
                            or pat.section_number() != 0
                            or pat.last_section_number() != 0
                        ):
                            continue
                        entries = tuple(
                            (program_number, pmt_pid)
                            for program_number, pmt_pid in pat
                            if program_number != 0
                        )
                        current_signature = (pat.version_number(), entries)
                        if pat_signature != current_signature:
                            pmt_parsers.clear()
                            pmt_program_numbers.clear()
                            programs.clear()
                            pat_signature = current_signature
                        for program_number, pmt_pid in entries:
                            pmt_parsers.setdefault(
                                pmt_pid,
                                SectionParser(PMTSection),
                            )
                            pmt_program_numbers[pmt_pid] = program_number
                    continue

                pmt_parser = pmt_parsers.get(packet_pid)
                if pmt_parser is None:
                    continue
                pmt_parser.push(packet)
                for pmt in pmt_parser:
                    expected_program_number = pmt_program_numbers[packet_pid]
                    if (
                        pmt.CRC32() != 0
                        or pmt.current_next_indicator() is False
                        or pmt.section_number() != 0
                        or pmt.last_section_number() != 0
                        or pmt.table_id_extension() != expected_program_number
                    ):
                        continue
                    streams: dict[
                        int,
                        tuple[int, list[tuple[int, bytes]]],
                    ] = {}
                    for stream_type, elementary_pid, descriptors in pmt:
                        streams[elementary_pid] = (
                            stream_type,
                            [(tag, bytes(payload)) for tag, payload in descriptors],
                        )
                    programs[expected_program_number] = (
                        pmt.PCR_PID,
                        streams,
                    )
        except (IndexError, TypeError, ValueError):
            # 188-byte境界とsyncだけが正しい破損PSIを、能力APIの500へ漏らさない。
            ## 実 pipe probeの構造不成立として空集合を返し、呼び出し側でProbeFailedにする。
            return {}
        return programs

    @staticmethod
    def __validateLiveProbePES(pes: bytes, *, is_video: bool) -> bool:
        """完全なPTS付きPES headerと空でないES payloadを持つか検証する。"""

        if (
            len(pes) < 14
            or pes[:3] != b'\x00\x00\x01'
            or pes[6] & 0xC0 != 0x80
        ):
            return False
        stream_id = pes[3]
        if is_video:
            # AVC/HEVC/VP9 は 0xE0..0xEF。AV1 MPEG-2 TS draft は private_stream_1 (0xBD)。
            # AV1 (と Bridge が整形する VP9) は data_alignment=1 を要求する。
            # FFmpeg の AVC/HEVC passthrough は alignment を立てないため、0xE0..0xEF では必須にしない。
            if stream_id == 0xBD:
                if pes[6] & 0x04 == 0:
                    return False
            elif not 0xE0 <= stream_id <= 0xEF:
                return False
        elif stream_id != 0xBD and not 0xC0 <= stream_id <= 0xDF:
            return False
        pes_packet_length = int.from_bytes(pes[4:6], 'big')
        pes_end = 6 + pes_packet_length if pes_packet_length != 0 else len(pes)
        if pes_end > len(pes):
            return False
        pts_dts_flags = pes[7] >> 6
        pes_header_data_length = pes[8]
        payload_start = 9 + pes_header_data_length
        if pts_dts_flags not in (0b10, 0b11) or pes_header_data_length < 5:
            return False
        if payload_start >= pes_end:
            return False

        # PTS/DTS の prefix と各 marker bit を検証し、単なる 00 00 01 擬似payloadを拒否する。
        pts = pes[9:14]
        expected_prefix = 0b0010 if pts_dts_flags == 0b10 else 0b0011
        if (
            pts[0] >> 4 != expected_prefix
            or pts[0] & 0x01 == 0
            or pts[2] & 0x01 == 0
            or pts[4] & 0x01 == 0
        ):
            return False
        if pts_dts_flags == 0b11:
            if pes_header_data_length < 10 or len(pes) < 19:
                return False
            dts = pes[14:19]
            if (
                dts[0] >> 4 != 0b0001
                or dts[0] & 0x01 == 0
                or dts[2] & 0x01 == 0
                or dts[4] & 0x01 == 0
            ):
                return False
        return True

    @classmethod
    def __validateLiveProbeProgramTransport(
        cls,
        output: bytes,
        pcr_pid: int,
        required_pids: set[int],
        video_pid: int | None,
    ) -> bool:
        """
        PMTだけでなく、実際のPCR・連続性・PTS付きPES・映像RAIまで成立するか検証する。

        能力probeは固定FFmpegとBridgeが生成する短い単一program TSに限定するため、
        TEI、scramble、壊れたadaptation field、CC飛びを一つでも含む出力はfail closedにする。
        """

        if (
            len(output) == 0
            or len(output) % 188 != 0
            or not required_pids
            or pcr_pid == 0x1FFF
        ):
            return False

        continuity_counters: dict[int, int] = {}
        pes_buffers = {pid: bytearray() for pid in required_pids}
        valid_pes_pids: set[int] = set()
        saw_pcr = False
        saw_video_random_access = video_pid is None

        def finish_pes(pid: int) -> None:
            """一つ前のPESを確定し、完全ならそのPIDを成功集合へ加える。"""

            pes = bytes(pes_buffers[pid])
            if pes and cls.__validateLiveProbePES(pes, is_video = pid == video_pid):
                valid_pes_pids.add(pid)

        for offset in range(0, len(output), 188):
            packet = output[offset:offset + 188]
            if packet[0] != 0x47 or packet[1] & 0x80 != 0 or packet[3] & 0xC0 != 0:
                return False

            pid = ((packet[1] & 0x1F) << 8) | packet[2]
            payload_unit_start = packet[1] & 0x40 != 0
            adaptation_field_control = (packet[3] >> 4) & 0x03
            continuity_counter = packet[3] & 0x0F
            if adaptation_field_control == 0:
                return False

            has_adaptation = adaptation_field_control in (2, 3)
            has_payload = adaptation_field_control in (1, 3)
            payload_offset = 4
            discontinuity = False
            random_access = False
            if has_adaptation:
                adaptation_field_length = packet[4]
                maximum_length = 183 if adaptation_field_control == 2 else 182
                if adaptation_field_length > maximum_length:
                    return False
                payload_offset = 5 + adaptation_field_length
                if adaptation_field_control == 2 and payload_offset != 188:
                    return False
                if adaptation_field_length > 0:
                    adaptation_flags = packet[5]
                    discontinuity = adaptation_flags & 0x80 != 0
                    random_access = adaptation_flags & 0x40 != 0
                    if adaptation_flags & 0x10 != 0:
                        if adaptation_field_length < 7:
                            return False
                        if pid == pcr_pid:
                            saw_pcr = True

            # null packetを除く全PIDでpayload continuityを検査し、PSIや未知ES上の破損も見逃さない。
            if pid != 0x1FFF:
                previous_counter = continuity_counters.get(pid)
                if previous_counter is not None and discontinuity is False:
                    expected_counter = (
                        (previous_counter + 1) & 0x0F
                        if has_payload
                        else previous_counter
                    )
                    if continuity_counter != expected_counter:
                        return False
                continuity_counters[pid] = continuity_counter

            if pid not in required_pids or has_payload is False:
                continue
            if payload_offset >= 188:
                return False
            if payload_unit_start:
                finish_pes(pid)
                pes_buffers[pid].clear()
                if pid == video_pid and random_access:
                    saw_video_random_access = True
            elif not pes_buffers[pid]:
                # probe開始前から継続中だったPES断片は能力の根拠にせず読み飛ばす。
                continue
            pes_buffers[pid].extend(packet[payload_offset:])

        for pid in required_pids:
            finish_pes(pid)
        return (
            saw_pcr
            and saw_video_random_access
            and valid_pes_pids == required_pids
        )

    @staticmethod
    def __isLiveProbeVideoMappingValid(
        video_codec: KonomiTVBS4KVideoCodec,
        bit_depth: KonomiTVBS4KVideoBitDepth,
        stream_type: int,
        descriptors: list[tuple[int, bytes]],
    ) -> bool:
        """probe 出力の映像 ES が要求 codec の厳密な搬送 descriptor と一致するか返す。"""

        if video_codec == 'vp9':
            return (
                stream_type == 0x06 and
                descriptors[:2] == [
                    (0x05, b'VP09'),
                    (0x80, b'KTVB\x09\x01\xF0\x00'),
                ]
            )
        if video_codec == 'av1':
            if (
                stream_type != 0x06 or
                len(descriptors) < 2 or
                descriptors[0] != (0x05, b'AV01')
            ):
                return False
            descriptor_tag, descriptor_payload = descriptors[1]
            if (
                descriptor_tag != 0x80 or
                len(descriptor_payload) != 4 or
                descriptor_payload[0] != 0x81 or
                descriptor_payload[3] & 0x20 != 0
            ):
                return False
            high_bitdepth = descriptor_payload[2] & 0x40 != 0
            twelve_bit = descriptor_payload[2] & 0x20 != 0
            if twelve_bit is True:
                return False
            descriptor_bit_depth = 10 if high_bitdepth is True else 8
            return descriptor_bit_depth == bit_depth

        expected_video_stream_type = 0x1B if video_codec == 'avc' else 0x24
        return stream_type == expected_video_stream_type

    @staticmethod
    def __isLiveProbeOpusMappingValid(
        stream_type: int,
        descriptors: list[tuple[int, bytes]],
    ) -> bool:
        """probe 出力の Opus ES が registration と DVB extension の厳密な組を持つか返す。"""

        if (
            stream_type != 0x06
            or len(descriptors) < 2
            or descriptors[0] != (0x05, b'Opus')
        ):
            return False
        extension_tag, extension_payload = descriptors[1]
        if (
            extension_tag != 0x7F
            or len(extension_payload) != 2
            or extension_payload[0] != 0x80
        ):
            return False
        channel_configuration = extension_payload[1]
        return 1 <= channel_configuration <= 8 or 0x82 <= channel_configuration <= 0x88

    @classmethod
    async def __runProbePipeline(
        cls,
        ffmpeg_command: list[str],
        bridge_command: list[str],
        failure_context: str,
        ffmpeg_environment: dict[str, str] | None = None,
    ) -> bytes | None:
        """
        FFmpeg と Bridge を OS pipe で直結し、両 process を同時に drain して完全な TS を返す。

        Args:
            ffmpeg_command (list[str]): 起動する固定 FFmpeg のコマンド列。
            bridge_command (list[str]): 起動する TS Codec Bridge のコマンド列。
            failure_context (str): process 失敗時のログに添える probe 条件。
            ffmpeg_environment (dict[str, str] | None): GPU backend 固有の FFmpeg 環境。

        Returns:
            bytes | None: 構造が成立した完全な MPEG-TS。失敗時は None。
        """

        read_pipe: int | None = None
        write_pipe: int | None = None
        ffmpeg: asyncio.subprocess.Process | None = None
        bridge: asyncio.subprocess.Process | None = None
        try:
            read_pipe, write_pipe = os.pipe()
            bridge = await asyncio.create_subprocess_exec(
                *bridge_command,
                stdin = read_pipe,
                stdout = asyncio.subprocess.PIPE,
                stderr = asyncio.subprocess.PIPE,
            )
            TSCodecBridgeRuntimeVerifier.recordProcessStart('probe')
            os.close(read_pipe)
            read_pipe = None
            ffmpeg = await asyncio.create_subprocess_exec(
                *ffmpeg_command,
                stdout = write_pipe,
                stderr = asyncio.subprocess.PIPE,
                env = ffmpeg_environment,
            )
            os.close(write_pipe)
            write_pipe = None

            # FFmpeg が pipe を埋め、Bridge が stdout 側で詰まる相互待ちを避けるため、両方を同時に drain する。
            async with asyncio.timeout(20):
                (_, ffmpeg_stderr), (output, bridge_stderr) = await asyncio.gather(
                    ffmpeg.communicate(),
                    bridge.communicate(),
                )
            if ffmpeg.returncode != 0 or bridge.returncode != 0:
                logging.warning(
                    '[KonomiTVBS4KPlaybackCapabilityProbe] Live transport probe failed. '
                    f'{failure_context}; '
                    f'FFmpeg={ffmpeg_stderr.decode(errors="replace").strip()}; '
                    f'Bridge={bridge_stderr.decode(errors="replace").strip()}'
                )
                return None
            if len(output) == 0 or len(output) % 188 != 0:
                return None
            if any(output[offset] != 0x47 for offset in range(0, len(output), 188)):
                return None
            return output
        except (OSError, TimeoutError, IndexError):
            return None
        finally:
            for pipe in (read_pipe, write_pipe):
                if pipe is not None:
                    try:
                        os.close(pipe)
                    except OSError:
                        pass
            for process in (ffmpeg, bridge):
                if process is not None and process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await process.wait()
                    except Exception:
                        pass

    @classmethod
    async def __runLiveTransportProbe(
        cls,
        encoder: KonomiTVBS4KPlaybackEncoder,
        video_codec: KonomiTVBS4KVideoCodec,
        bit_depth: KonomiTVBS4KVideoBitDepth,
        audio_codec: KonomiTVBS4KAudioCodec,
    ) -> bool:
        """1 組を指定 backend で実エンコードし、OS pipe と Bridge の TS 構造を検証する。"""

        ffmpeg_path = RecordedPlaybackBackend.getExecutable(encoder)
        bridge_path = LIBRARY_PATH.get(cls.BRIDGE_LIBRARY_PATH_KEY)
        if Path(ffmpeg_path).is_file() is False or bridge_path is None:
            return False

        ffmpeg_encoder = RecordedPlaybackBackend.getEncoderName(encoder, video_codec)
        if ffmpeg_encoder is None:
            return False
        codec_spec = RecordedPlaybackBackend.getCodecSpec(video_codec, bit_depth)
        selected_device: str | None = None
        if encoder in ('QSV', 'AMF'):
            selected_device = RecordedPlaybackCapabilityProbe.getSelectedDevice(
                encoder,
                video_codec,
                bit_depth,
            )
            if selected_device is None:
                return False

        ffmpeg_command = [
            ffmpeg_path,
            '-nostdin',
            '-hide_banner',
            '-loglevel',
            'error',
        ]
        if encoder == 'QSV':
            ffmpeg_command += [
                '-init_hw_device',
                f'qsv=live_qsv:{selected_device}',
                '-filter_hw_device',
                'live_qsv',
            ]
        elif encoder == 'NVENC':
            ffmpeg_command += [
                '-init_hw_device',
                'cuda=live_cuda:0',
                '-filter_hw_device',
                'live_cuda',
            ]
        elif encoder == 'AMF':
            ffmpeg_command += [
                '-init_hw_device',
                f'vaapi=live_vaapi:{selected_device}',
                '-filter_hw_device',
                'live_vaapi',
            ]

        ffmpeg_command += [
            '-f',
            'lavfi',
            '-i',
            'testsrc2=size=640x360:rate=10:duration=0.8',
            '-f',
            'lavfi',
            '-i',
            'sine=frequency=1000:sample_rate=48000:duration=0.8',
            '-f',
            'lavfi',
            '-i',
            'sine=frequency=1400:sample_rate=48000:duration=0.8',
            '-map',
            '0:v:0',
            '-map',
            '1:a:0',
            '-map',
            '2:a:0',
        ]
        if encoder == 'FFmpeg':
            ffmpeg_command += ['-vf', f'format={codec_spec.pixel_format}']
        elif encoder == 'QSV':
            ffmpeg_command += [
                '-vf',
                f'format={codec_spec.encoder_pixel_format},hwupload=extra_hw_frames=32',
            ]
        elif encoder == 'NVENC':
            ffmpeg_command += [
                '-vf',
                f'format={codec_spec.encoder_pixel_format},hwupload_cuda',
            ]
        else:
            ffmpeg_command += [
                '-vf',
                f'format={codec_spec.encoder_pixel_format},hwupload,'
                f'scale_vaapi=w=640:h=360,hwdownload,format={codec_spec.encoder_pixel_format}',
            ]

        ffmpeg_command += [
            '-c:v',
            ffmpeg_encoder,
            '-g',
            '5',
        ]
        if encoder == 'FFmpeg':
            ffmpeg_command += ['-pix_fmt', codec_spec.pixel_format]
        elif encoder == 'NVENC':
            ffmpeg_command += ['-pix_fmt', 'cuda']
        elif encoder == 'AMF':
            ffmpeg_command += ['-pix_fmt', codec_spec.encoder_pixel_format]

        if video_codec == 'vp9':
            ffmpeg_command += [
                '-profile:v',
                ('profile2' if bit_depth == 10 else 'profile0')
                if encoder == 'QSV'
                else ('2' if bit_depth == 10 else '0'),
            ]
        elif video_codec == 'av1':
            if encoder == 'FFmpeg':
                ffmpeg_command += ['-profile:v', '0']
            elif encoder != 'NVENC':
                ffmpeg_command += ['-profile:v', 'main']
        elif video_codec == 'hevc':
            ffmpeg_command += ['-profile:v', 'main10' if bit_depth == 10 else 'main']
        else:
            ffmpeg_command += ['-profile:v', 'high']

        ffmpeg_command += RecordedPlaybackBackend.getTuningArguments(
            encoder, video_codec
        )
        if encoder == 'FFmpeg':
            if video_codec == 'vp9':
                ffmpeg_command += ['-lag-in-frames', '0', '-auto-alt-ref', '0']
            elif video_codec == 'av1':
                ffmpeg_command += ['-lag-in-frames', '0']
        elif encoder == 'QSV':
            ffmpeg_command += ['-async_depth', '1']
            if video_codec in ('vp9', 'av1'):
                ffmpeg_command += ['-look_ahead', '0', '-bf', '0']
        elif encoder == 'NVENC':
            ffmpeg_command += [
                '-preset',
                'p4',
                '-tune',
                'll',
                '-rc',
                'vbr',
                '-rc-lookahead',
                '0',
                '-spatial-aq',
                '1',
                '-temporal-aq',
                '1',
            ]
            if video_codec in ('vp9', 'av1'):
                ffmpeg_command += ['-zerolatency', '1', '-bf', '0']
        else:
            ffmpeg_command += [
                '-quality',
                'balanced',
                '-rc',
                'vbr_latency',
                '-async_depth',
                '1',
            ]
            if video_codec in ('vp9', 'av1'):
                ffmpeg_command += ['-usage', 'lowlatency', '-latency', '1', '-bf', '0']

        # Bridge が SELECTED_PCR_GAP で fail closed するため、VP9/AV1 だけでなく
        # Opus 付きの passthrough 映像 (AVC/HEVC) でも固定 muxrate と PCR 周期を使う。
        live_probe_muxrate: str | None = None
        needs_fixed_muxrate = (
            video_codec in ('vp9', 'av1')
            or audio_codec == 'opus'
        )
        if needs_fixed_muxrate is True:
            live_probe_video_bitrate_max = '800K'
            live_probe_muxrate = ResolveKonomiTVBS4KAdvancedLiveMuxrate(
                live_probe_video_bitrate_max
            )
            if video_codec in ('vp9', 'av1'):
                ffmpeg_command += [
                    '-b:v',
                    '600K',
                    '-maxrate',
                    live_probe_video_bitrate_max,
                    '-bufsize',
                    live_probe_video_bitrate_max,
                ]
            ffmpeg_command += [
                '-muxrate',
                live_probe_muxrate,
                '-pcr_period',
                '20',
            ]
        if audio_codec == 'opus':
            ffmpeg_command += ['-c:a', 'libopus', '-application', 'audio', '-b:a', '96K', '-ar', '48000']
        else:
            ffmpeg_command += ['-c:a', 'aac', '-b:a', '96K', '-ar', '48000']
        ffmpeg_command += ['-frames:v', '8', '-shortest', '-f', 'mpegts', 'pipe:1']

        bridge_video_codec = video_codec if video_codec in ('vp9', 'av1') else 'passthrough'
        bridge_command = [
            bridge_path,
            '--video-codec', bridge_video_codec,
            '--audio-codec', audio_codec,
        ]
        if video_codec == 'av1':
            assert live_probe_muxrate is not None
            bridge_command += [
                '--transport-rate-kbps',
                ParseKonomiTVBS4KAdvancedLiveMuxrateKbps(live_probe_muxrate),
            ]

        output = await cls.__runProbePipeline(
            ffmpeg_command,
            bridge_command,
            f'Encoder={encoder}; Video={video_codec}/{bit_depth}; Audio={audio_codec}',
            RecordedPlaybackBackend.getEnvironment(encoder),
        )
        if output is None:
            return False
        programs = cls.__collectLiveProbePMTStreams(output)
        if len(programs) != 1:
            return False
        pcr_pid, streams = next(iter(programs.values()))
        video_pids = {
            pid
            for pid, (stream_type, descriptors) in streams.items()
            if cls.__isLiveProbeVideoMappingValid(
                video_codec,
                bit_depth,
                stream_type,
                descriptors,
            )
        }
        if len(video_pids) != 1:
            return False

        if audio_codec == 'opus':
            audio_pids = {
                pid
                for pid, (stream_type, descriptors) in streams.items()
                if cls.__isLiveProbeOpusMappingValid(stream_type, descriptors)
            }
        else:
            audio_pids = {
                pid
                for pid, (stream_type, _descriptors) in streams.items()
                if stream_type in (0x0F, 0x11)
            }
        if len(streams) != 3 or len(audio_pids) != 2:
            return False
        return cls.__validateLiveProbeProgramTransport(
            output,
            pcr_pid,
            video_pids | audio_pids,
            next(iter(video_pids)),
        )

    @classmethod
    async def __runRadioOpusTransportProbe(cls) -> bool:
        """
        映像なし・二音声の固定 FFmpeg MPEG-TS を Bridge の passthrough / Opus 経路で検証する。

        Args:
            None

        Returns:
            bool: 出力 PMT が Opus 二音声だけを含む場合は True。
        """

        ffmpeg_path = LIBRARY_PATH.get('FFmpeg8')
        bridge_path = LIBRARY_PATH.get(cls.BRIDGE_LIBRARY_PATH_KEY)
        if ffmpeg_path is None or bridge_path is None:
            return False

        ffmpeg_command = [
            ffmpeg_path,
            '-nostdin',
            '-hide_banner',
            '-loglevel',
            'error',
            '-f',
            'lavfi',
            '-i',
            'sine=frequency=1000:sample_rate=48000:duration=0.8',
            '-f',
            'lavfi',
            '-i',
            'sine=frequency=1400:sample_rate=48000:duration=0.8',
            '-map',
            '0:a:0',
            '-map',
            '1:a:0',
            '-vn',
            '-c:a',
            'libopus',
            '-application',
            'audio',
            '-ac',
            '2',
            '-b:a',
            '192K',
            '-ar',
            '48000',
            '-af',
            'volume=2.0',
            '-fflags',
            'nobuffer',
            '-flags',
            'low_delay',
            '-max_delay',
            '250000',
            '-max_interleave_delta',
            '500K',
            '-flush_packets',
            '1',
            '-threads',
            'auto',
            '-shortest',
            '-f',
            'mpegts',
            'pipe:1',
        ]
        bridge_command = [
            bridge_path,
            '--video-codec',
            'passthrough',
            '--audio-codec',
            'opus',
        ]
        output = await cls.__runProbePipeline(
            ffmpeg_command,
            bridge_command,
            'RadioAudio=opus',
        )
        if output is None:
            return False

        # ラジオ probe は Opus 二音声だけを許可し、欠落だけでなく意図しない映像や data ES の混入も拒否する。
        programs = cls.__collectLiveProbePMTStreams(output)
        if len(programs) != 1:
            return False
        pcr_pid, streams = next(iter(programs.values()))
        opus_pids = {
            pid
            for pid, (stream_type, descriptors) in streams.items()
            if cls.__isLiveProbeOpusMappingValid(stream_type, descriptors)
        }
        if len(streams) != 2 or len(opus_pids) != 2:
            return False
        return cls.__validateLiveProbeProgramTransport(
            output,
            pcr_pid,
            opus_pids,
            None,
        )

    @classmethod
    async def getVideoCapability(
        cls,
        encoder: KonomiTVBS4KPlaybackEncoder,
        codec: KonomiTVBS4KVideoCodec,
        bit_depth: KonomiTVBS4KVideoBitDepth,
    ) -> KonomiTVBS4KPlaybackVideoCapability | None:
        """指定映像の1組だけを検査し、全ライブ行列の初回probeを起動せず返す。"""

        recorded = await RecordedPlaybackCapabilityProbe.getCapability(
            encoder,
            codec,
            bit_depth,
        )
        live_available = recorded.available
        live_reason_code = recorded.reason_code
        if recorded.available is True and codec in ('vp9', 'av1'):
            if cls.isFFmpegAvailable() is False:
                live_available = False
                live_reason_code = 'BinaryUnavailable'
            elif await cls.isBridgeAvailableAsync() is False:
                live_available = False
                live_reason_code = 'BridgeUnavailable'
            elif (
                await cls.isLiveTransportAvailable(
                    encoder,
                    codec,
                    bit_depth,
                    'aac',
                )
                is False
            ):
                live_available = False
                live_reason_code = 'ProbeFailed'
            else:
                live_reason_code = None
        return KonomiTVBS4KPlaybackVideoCapability(
            encoder = encoder,
            codec = codec,
            bit_depth = bit_depth,
            profile = recorded.profile,
            live_available = live_available,
            recorded_available = recorded.available,
            live_reason_code = live_reason_code,
            recorded_reason_code = recorded.reason_code,
        )

    @classmethod
    async def getAudioCapability(
        cls,
        codec: KonomiTVBS4KAudioCodec,
    ) -> KonomiTVBS4KPlaybackAudioCapability | None:
        """指定音声だけを検査し、映像能力行列を起動せず返す。"""

        is_ffmpeg_available = cls.isFFmpegAvailable()
        binary_reason: KonomiTVBS4KPlaybackCapabilityReason | None = (
            None if is_ffmpeg_available is True else 'BinaryUnavailable'
        )
        if codec == 'aac':
            return KonomiTVBS4KPlaybackAudioCapability(
                codec = codec,
                live_available = is_ffmpeg_available,
                recorded_available = is_ffmpeg_available,
                live_reason_code = binary_reason,
                recorded_reason_code = binary_reason,
            )

        live_available = False
        live_reason_code: KonomiTVBS4KPlaybackCapabilityReason | None
        if is_ffmpeg_available is False:
            live_reason_code = 'BinaryUnavailable'
        elif await cls.isBridgeAvailableAsync() is False:
            live_reason_code = 'BridgeUnavailable'
        elif await cls.isRadioOpusTransportAvailable() is False:
            live_reason_code = 'ProbeFailed'
        else:
            live_available = True
            live_reason_code = None
        return KonomiTVBS4KPlaybackAudioCapability(
            codec = codec,
            live_available = live_available,
            recorded_available = is_ffmpeg_available,
            live_reason_code = live_reason_code,
            recorded_reason_code = binary_reason,
        )

    @classmethod
    async def getRecordedVideoCapability(
        cls,
        encoder: KonomiTVBS4KPlaybackEncoder,
        codec: KonomiTVBS4KVideoCodec,
        bit_depth: KonomiTVBS4KVideoBitDepth,
    ) -> KonomiTVBS4KPlaybackVideoCapability:
        """録画映像の実encode可否だけを返し、live TS probeを起動しない。"""

        recorded = await RecordedPlaybackCapabilityProbe.getCapability(
            encoder,
            codec,
            bit_depth,
        )
        return KonomiTVBS4KPlaybackVideoCapability(
            encoder = recorded.encoder,
            codec = recorded.codec,
            bit_depth = recorded.bit_depth,
            profile = recorded.profile,
            # この取得口のlive値は能力根拠として使わず、録画結果だけを公開する。
            live_available = False,
            recorded_available = recorded.available,
            live_reason_code = 'ProbeFailed',
            recorded_reason_code = recorded.reason_code,
        )

    @classmethod
    async def getRecordedAudioCapability(
        cls,
        codec: KonomiTVBS4KAudioCodec,
    ) -> KonomiTVBS4KPlaybackAudioCapability:
        """録画音声の固定FFmpeg可否だけを返し、live radio probeを起動しない。"""

        is_ffmpeg_available = cls.isFFmpegAvailable()
        binary_reason: KonomiTVBS4KPlaybackCapabilityReason | None = (
            None if is_ffmpeg_available is True else 'BinaryUnavailable'
        )
        return KonomiTVBS4KPlaybackAudioCapability(
            codec = codec,
            # この取得口は録画経路専用であり、live値を能力根拠として公開しない。
            live_available = codec == 'aac' and is_ffmpeg_available,
            recorded_available = is_ffmpeg_available,
            live_reason_code = (
                binary_reason
                if is_ffmpeg_available is False
                else (None if codec == 'aac' else 'ProbeFailed')
            ),
            recorded_reason_code = binary_reason,
        )

    @classmethod
    async def getLiveCombinationCapability(
        cls,
        encoder: KonomiTVBS4KPlaybackEncoder,
        video_codec: KonomiTVBS4KVideoCodec,
        video_bit_depth: KonomiTVBS4KVideoBitDepth,
        audio_codec: KonomiTVBS4KAudioCodec,
        *,
        recorded_capability: RecordedPlaybackCapability | None = None,
    ) -> KonomiTVBS4KPlaybackLiveCombinationCapability:
        """
        指定したexact combinationだけを実probeし、全行列の初回待ちを避ける。

        Args:
            encoder: 実際に使う映像エンコーダー。
            video_codec: 出力映像コーデック。
            video_bit_depth: 出力映像bit depth。
            audio_codec: 出力音声コーデック。
            recorded_capability: 同じ映像組の取得済み録画能力。targeted応答の重複probe回避に使う。

        Returns:
            指定した映像・音声・backendのexact live組み合わせ能力。
        """

        recorded = recorded_capability
        if recorded is None:
            recorded = await RecordedPlaybackCapabilityProbe.getCapability(
                encoder,
                video_codec,
                video_bit_depth,
            )
        elif (
            recorded.encoder,
            recorded.codec,
            recorded.bit_depth,
        ) != (encoder, video_codec, video_bit_depth):
            raise ValueError('Recorded capability does not match the requested live combination.')
        return await cls.__buildLiveCombinationCapability(recorded, audio_codec)

    @classmethod
    async def __buildLiveCombinationCapability(
        cls,
        recorded: RecordedPlaybackCapability,
        audio_codec: KonomiTVBS4KAudioCodec,
        *,
        is_ffmpeg_available: bool | None = None,
        is_bridge_available: bool | None = None,
    ) -> KonomiTVBS4KPlaybackLiveCombinationCapability:
        """
        取得済み録画能力を再利用し、指定音声とのexact live組み合わせ能力を構築する。

        Args:
            recorded: 同じエンコーダー・映像codec・bit depthの取得済み録画能力。
            audio_codec: 組み合わせる出力音声コーデック。

        Returns:
            全行列やaudio-only probeを起動せず構築したexact live組み合わせ能力。
        """

        is_advanced_combination = (
            recorded.codec in ('vp9', 'av1') or audio_codec == 'opus'
        )
        reason_code: KonomiTVBS4KPlaybackCapabilityReason | None = (
            recorded.reason_code if recorded.available is False else None
        )
        if recorded.available is True and is_advanced_combination is True:
            if is_ffmpeg_available is None:
                is_ffmpeg_available = cls.isFFmpegAvailable()
            if is_bridge_available is None and is_ffmpeg_available is True:
                is_bridge_available = await cls.isBridgeAvailableAsync()
            if is_ffmpeg_available is False:
                reason_code = 'BinaryUnavailable'
            elif is_bridge_available is False:
                reason_code = 'BridgeUnavailable'
            elif (
                await cls.isLiveTransportAvailable(
                    recorded.encoder,
                    recorded.codec,
                    recorded.bit_depth,
                    audio_codec,
                )
                is False
            ):
                reason_code = 'ProbeFailed'
        return KonomiTVBS4KPlaybackLiveCombinationCapability(
            encoder = recorded.encoder,
            video_codec = recorded.codec,
            video_bit_depth = recorded.bit_depth,
            audio_codec = audio_codec,
            available = reason_code is None,
            reason_code = reason_code,
        )
