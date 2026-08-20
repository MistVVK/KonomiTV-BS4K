from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from app import logging
from app.constants import LIBRARY_PATH, QUALITY, QUALITY_TYPES
from app.streams.KonomiTVBS4KPlaybackEncoding import (
    KonomiTVBS4KPlaybackCapabilityReason,
    KonomiTVBS4KPlaybackEncoder,
    KonomiTVBS4KVideoBitDepth,
    KonomiTVBS4KVideoCodec,
    ResolveKonomiTVBS4KPlaybackVideoBitrate,
)


# 既存importを壊さず、型の権威データだけをライブ・録画共通moduleへ移す。
RecordedPlaybackEncoder = KonomiTVBS4KPlaybackEncoder
RecordedPlaybackVideoCodec = KonomiTVBS4KVideoCodec
RecordedPlaybackBitDepth = KonomiTVBS4KVideoBitDepth
RecordedPlaybackCapabilityReason = KonomiTVBS4KPlaybackCapabilityReason


@dataclass(frozen=True, slots=True)
class RecordedPlaybackCodecSpec:
    """録画用FFmpeg 8で使用するコーデックと固定出力形式を表す。"""

    codec: RecordedPlaybackVideoCodec
    bit_depth: RecordedPlaybackBitDepth
    ffprobe_codec_name: str
    profile: str
    pixel_format: str
    encoder_pixel_format: str


@dataclass(frozen=True, slots=True)
class RecordedPlaybackCapability:
    """エンコーダー・コーデック・bit depth単位の能力検査結果を表す。"""

    encoder: RecordedPlaybackEncoder
    codec: RecordedPlaybackVideoCodec
    bit_depth: RecordedPlaybackBitDepth
    available: bool
    profile: str
    reason_code: RecordedPlaybackCapabilityReason | None


RecordedPlaybackCapabilityBaseKey = tuple[
    RecordedPlaybackEncoder,
    RecordedPlaybackVideoCodec,
    RecordedPlaybackBitDepth,
]
RecordedPlaybackCapabilityKey = RecordedPlaybackCapabilityBaseKey | tuple[
    RecordedPlaybackEncoder,
    RecordedPlaybackVideoCodec,
    RecordedPlaybackBitDepth,
    QUALITY_TYPES,
]


@dataclass(slots=True)
class _RecordedPlaybackProbeContext:
    """実probe Task内だけで成功したrender nodeを保持する。"""

    selected_device: str | None = None


class RecordedPlaybackBackend:
    """録画能力検査・録画再生・ライブで共有するFFmpeg 8バックエンド定義を提供する。"""

    _ENCODERS: ClassVar[dict[RecordedPlaybackEncoder, dict[RecordedPlaybackVideoCodec, str | None]]] = {
        'FFmpeg': {'avc': 'libx264', 'hevc': 'libx265', 'vp9': 'libvpx-vp9', 'av1': 'libaom-av1'},
        'QSV': {'avc': 'h264_qsv', 'hevc': 'hevc_qsv', 'vp9': 'vp9_qsv', 'av1': 'av1_qsv'},
        'NVENC': {'avc': 'h264_nvenc', 'hevc': 'hevc_nvenc', 'vp9': None, 'av1': 'av1_nvenc'},
        # 公開名 AMF は設定互換のため残し、実エンコードは Mesa VAAPI を使う。
        # proprietary AMF はホスト kernel と amdgpu-pro の ABI がずれると初期化できない。
        'AMF': {'avc': 'h264_vaapi', 'hevc': 'hevc_vaapi', 'vp9': None, 'av1': 'av1_vaapi'},
    }

    _VENDOR_IDS: ClassVar[dict[RecordedPlaybackEncoder, str]] = {
        'QSV': '0x8086',
        'AMF': '0x1002',
    }
    _AMD_PROPRIETARY_VAAPI_DRIVER_DIRECTORY: ClassVar[Path] = Path('/opt/amdgpu/lib/x86_64-linux-gnu/dri')
    _AMD_MESA_VAAPI_DRIVER_DIRECTORY: ClassVar[Path] = Path('/usr/lib/x86_64-linux-gnu/dri')

    @staticmethod
    def getCodecSpec(
        codec: RecordedPlaybackVideoCodec,
        bit_depth: RecordedPlaybackBitDepth,
    ) -> RecordedPlaybackCodecSpec:
        """固定出力方針からコーデック仕様を返す。

        Args:
            codec: 出力映像コーデック。
            bit_depth: 出力bit depth。

        Returns:
            固定profileとpixel formatを含むコーデック仕様。
        """

        profile = {
            ('avc', 8): 'High',
            ('hevc', 8): 'Main',
            ('hevc', 10): 'Main 10',
            ('vp9', 8): 'Profile 0',
            ('vp9', 10): 'Profile 2',
            ('av1', 8): 'Main',
            ('av1', 10): 'Main',
        }.get((codec, bit_depth), '')
        return RecordedPlaybackCodecSpec(
            codec = codec,
            bit_depth = bit_depth,
            ffprobe_codec_name = {'avc': 'h264', 'hevc': 'hevc', 'vp9': 'vp9', 'av1': 'av1'}[codec],
            profile = profile,
            pixel_format = 'yuv420p' if bit_depth == 8 else 'yuv420p10le',
            encoder_pixel_format = 'nv12' if bit_depth == 8 else 'p010le',
        )

    @classmethod
    def isCombinationSupported(
        cls,
        encoder: RecordedPlaybackEncoder,
        codec: RecordedPlaybackVideoCodec,
        bit_depth: RecordedPlaybackBitDepth,
    ) -> bool:
        """設計上対応する組み合わせかを返す。

        Args:
            encoder: 公開設定上のエンコーダー名。
            codec: 出力映像コーデック。
            bit_depth: 出力bit depth。

        Returns:
            能力検査を行う候補ならTrue。
        """

        if codec == 'avc' and bit_depth == 10:
            return False
        return cls._ENCODERS[encoder][codec] is not None

    @classmethod
    def getEncoderName(
        cls,
        encoder: RecordedPlaybackEncoder,
        codec: RecordedPlaybackVideoCodec,
    ) -> str | None:
        """FFmpeg 8の映像エンコーダー名を返す。

        Args:
            encoder: 公開設定上のエンコーダー名。
            codec: 出力映像コーデック。

        Returns:
            FFmpegエンコーダー名。非対応ならNone。
        """

        return cls._ENCODERS[encoder][codec]

    @staticmethod
    def getTuningArguments(
        encoder: RecordedPlaybackEncoder,
        codec: RecordedPlaybackVideoCodec,
    ) -> list[str]:
        """オンデマンド生成を実時間へ近づけるエンコーダー固有引数を返す。

        Args:
            encoder: 録画再生バックエンド。
            codec: 出力映像コーデック。

        Returns:
            能力検査と実再生で共通利用するFFmpeg引数。
        """

        # GPU encoderは各実装の低遅延寄り既定値を維持する。CPU encoderは既定値のままだと
        # 特にlibaom-av1が数秒のsegment生成に数分を要するため、視聴用途の速度へ固定する。
        if encoder != 'FFmpeg':
            return []
        return {
            'avc': ['-preset', 'veryfast'],
            'hevc': ['-preset', 'veryfast'],
            'vp9': ['-deadline', 'realtime', '-cpu-used', '6', '-row-mt', '1'],
            'av1': ['-usage', 'realtime', '-cpu-used', '8', '-row-mt', '1'],
        }[codec]

    @staticmethod
    def getExecutable(encoder: RecordedPlaybackEncoder) -> str:
        """バックエンドに対応する内部FFmpeg 8を返す。

        Args:
            encoder: 公開設定上のエンコーダー名。

        Returns:
            実行ファイルの絶対パス。
        """

        return LIBRARY_PATH['FFmpeg8AMD'] if encoder == 'AMF' else LIBRARY_PATH['FFmpeg8']

    @staticmethod
    def getEnvironment(encoder: RecordedPlaybackEncoder) -> dict[str, str]:
        """KonomiTV-BS4KのFFmpeg 8に限定したGPU runtime環境を返す。

        Args:
            encoder: 公開設定上のエンコーダー名。

        Returns:
            親プロセス環境を継承した実行環境。
        """

        environment = os.environ.copy()
        if encoder in ('QSV', 'AMF'):
            # VAAPI driver と loader の ABI を Ubuntu 22.04 の system libva に依存させない。
            # vendor-neutral な同梱 libva 2.23 を共用し、driver だけを GPU ごとに切り替える。
            library_path = Path(LIBRARY_PATH['FFmpeg8']).parent.parent / 'Library'
            current_library_path = environment.get('LD_LIBRARY_PATH')
            environment['LD_LIBRARY_PATH'] = (
                f'{library_path}:{current_library_path}' if current_library_path else str(library_path)
            )
            if encoder == 'QSV':
                environment['LIBVA_DRIVER_NAME'] = 'iHD'
                environment['LIBVA_DRIVERS_PATH'] = str(library_path / 'dri')
            else:
                environment['LIBVA_DRIVER_NAME'] = 'radeonsi'
                environment['LIBVA_DRIVERS_PATH'] = str(RecordedPlaybackBackend.getAMDVAAPIDriverDirectory())
        return environment

    @classmethod
    def getAMDVAAPIDriverDirectory(cls) -> Path:
        """AMD proprietary 有効時はその driver、無効時は Mesa を返す。"""

        proprietary_driver = cls._AMD_PROPRIETARY_VAAPI_DRIVER_DIRECTORY / 'radeonsi_drv_video.so'
        if proprietary_driver.is_file():
            return cls._AMD_PROPRIETARY_VAAPI_DRIVER_DIRECTORY
        return cls._AMD_MESA_VAAPI_DRIVER_DIRECTORY

    @classmethod
    def discoverRenderDevices(cls, encoder: RecordedPlaybackEncoder) -> list[str]:
        """vendor IDが一致するDRM render nodeを列挙する。

        Args:
            encoder: QSVまたはAMFを表す公開エンコーダー名。

        Returns:
            デバイス初期化を試す順番のrender node一覧。
        """

        vendor_id = cls._VENDOR_IDS.get(encoder)
        if vendor_id is None:
            return []
        devices: list[str] = []
        for device_path in sorted(Path('/sys/class/drm').glob('renderD*/device')):
            try:
                if device_path.joinpath('vendor').read_text().strip().lower() == vendor_id:
                    devices.append(f'/dev/dri/{device_path.parent.name}')
            except OSError:
                continue
        return devices

    @classmethod
    def buildProbeCommand(
        cls,
        encoder: RecordedPlaybackEncoder,
        codec: RecordedPlaybackVideoCodec,
        bit_depth: RecordedPlaybackBitDepth,
        output_path: Path,
        device: str | None,
        *,
        quality: QUALITY_TYPES | None = None,
    ) -> list[str]:
        """能力検査用の短いfMP4生成コマンドを構築する。

        Args:
            encoder: 公開設定上のエンコーダー名。
            codec: 出力映像コーデック。
            bit_depth: 出力bit depth。
            output_path: 検査用fMP4の出力先。
            device: QSV/AMFで使うrender node。CPU/NVENCではNone。
            quality: 実再生相当の出力解像度・フレームレートを検査する画質。

        Returns:
            create_subprocess_exec()へそのまま渡せる完全な引数列。
        """

        spec = cls.getCodecSpec(codec, bit_depth)
        ffmpeg_encoder = cls.getEncoderName(encoder, codec)
        if ffmpeg_encoder is None:
            raise ValueError(f'Unsupported encoder/codec combination: {encoder}/{codec}')

        command = [
            cls.getExecutable(encoder),
            '-hide_banner',
            '-loglevel',
            'error',
        ]
        if encoder == 'QSV':
            if device is None:
                raise ValueError('QSV render device is required.')
            command += ['-init_hw_device', f'qsv=recorded_qsv:{device}', '-filter_hw_device', 'recorded_qsv']
        elif encoder == 'NVENC':
            command += ['-init_hw_device', 'cuda=recorded_cuda:0', '-filter_hw_device', 'recorded_cuda']
        elif encoder == 'AMF':
            if device is None:
                raise ValueError('AMD render device is required.')
            command += ['-init_hw_device', f'vaapi=recorded_vaapi:{device}', '-filter_hw_device', 'recorded_vaapi']

        # 入力生成自体のCPU・メモリ負荷は小さく保ち、scale後のsurfaceとencoder設定だけを
        # 実画質へ揃える。これで8K非対応GPUを検出しつつ、8K fixture生成の余計な負荷を避ける。
        frame_rate = (
            '60000/1001'
            if quality is not None and QUALITY[quality].is_60fps is True
            else ('30000/1001' if quality is not None else '10')
        )
        command += ['-f', 'lavfi', '-i', f'testsrc2=size=320x192:rate={frame_rate}:duration=0.6']
        if encoder == 'FFmpeg':
            filters = []
            if quality is not None:
                filters += [
                    f'scale=w={QUALITY[quality].width}:h={QUALITY[quality].height}:'
                    'force_original_aspect_ratio=decrease',
                    f'pad={QUALITY[quality].width}:{QUALITY[quality].height}:(ow-iw)/2:(oh-ih)/2',
                ]
            filters.append(f'format={spec.pixel_format}')
            command += ['-vf', ','.join(filters)]
        elif encoder == 'QSV':
            filters = [f'format={spec.encoder_pixel_format}', 'hwupload=extra_hw_frames=32']
            if quality is not None:
                filters.append(
                    f'vpp_qsv=w={QUALITY[quality].width}:h={QUALITY[quality].height}:'
                    f'format={spec.encoder_pixel_format}'
                )
            command += ['-vf', ','.join(filters)]
        elif encoder == 'NVENC':
            filters = [f'format={spec.encoder_pixel_format}', 'hwupload_cuda']
            if quality is not None:
                filters.append(
                    f'scale_cuda=w={QUALITY[quality].width}:h={QUALITY[quality].height}:'
                    f'format={spec.encoder_pixel_format}'
                )
            command += ['-vf', ','.join(filters)]
        else:
            # QSV/NVENC と同様に、upload 後の VAAPI 面を encoder へ直接渡す。
            output_width = QUALITY[quality].width if quality is not None else 320
            output_height = QUALITY[quality].height if quality is not None else 192
            command += [
                '-vf',
                f'format={spec.encoder_pixel_format},hwupload,'
                f'scale_vaapi=w={output_width}:h={output_height}:format={spec.encoder_pixel_format}',
            ]

        command += ['-an', '-c:v', ffmpeg_encoder]
        # QSV/CUDAはhwupload後のhardware frame formatをそのままencoderへ渡す。
        # software pixel formatを出力側で強制すると、不要なauto_scaleが挿入され接続できない。
        if encoder == 'FFmpeg':
            command += ['-pix_fmt', spec.pixel_format]
        elif encoder == 'NVENC':
            # NVENCへCUDA frameを渡すことを明示し、software nv12/p010leへ戻す
            # auto_scaleがhwupload_cuda後へ挿入されるのを防ぐ。
            command += ['-pix_fmt', 'cuda']
        elif encoder == 'AMF':
            command += ['-pix_fmt', 'vaapi']
        if codec == 'avc':
            command += ['-profile:v', 'high']
        elif codec == 'hevc':
            command += ['-profile:v', 'main10' if bit_depth == 10 else 'main']
        elif codec == 'vp9':
            command += [
                '-profile:v',
                ('profile2' if bit_depth == 10 else 'profile0') if encoder == 'QSV' else
                ('2' if bit_depth == 10 else '0'),
            ]
        elif codec == 'av1':
            # av1_nvencはprofileオプション自体を公開しておらず、指定すると起動前に失敗する。
            # libaom-av1は数値、QSV/AMFはmainを受け付けるためバックエンド別に指定する。
            if encoder == 'FFmpeg':
                command += ['-profile:v', '0']
            elif encoder != 'NVENC':
                command += ['-profile:v', 'main']
        command += cls.getTuningArguments(encoder, codec)
        if quality is not None:
            bitrate = ResolveKonomiTVBS4KPlaybackVideoBitrate(quality, codec)
            bitrate_max_kbps = int(bitrate.video_bitrate_max.removesuffix('K'))
            command += [
                '-r',
                frame_rate,
                '-fps_mode',
                'cfr',
                '-b:v',
                bitrate.video_bitrate,
                '-maxrate',
                bitrate.video_bitrate_max,
                '-bufsize',
                f'{bitrate_max_kbps * 2}K',
                '-aspect',
                '16:9',
            ]
        command += [
            '-frames:v',
            '6',
            '-movflags',
            '+frag_keyframe+delay_moov+default_base_moof',
            '-f',
            'mp4',
            '-y',
            str(output_path),
        ]
        return command


class RecordedPlaybackCapabilityProbe:
    """録画用FFmpeg 8の能力行列を遅延検査し、プロセス内で共有する。"""

    _result: ClassVar[list[RecordedPlaybackCapability] | None] = None
    _individual_results: ClassVar[dict[RecordedPlaybackCapabilityKey, RecordedPlaybackCapability]] = {}
    _signature: ClassVar[str | None] = None
    _signature_generation: ClassVar[int] = 0
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    _inflight_tasks: ClassVar[
        dict[
            tuple[int, RecordedPlaybackCapabilityKey],
            asyncio.Task[tuple[RecordedPlaybackCapability, str | None] | None],
        ]
    ] = {}
    _matrix_inflight_tasks: ClassVar[
        dict[int, asyncio.Task[list[RecordedPlaybackCapability] | None]]
    ] = {}
    _selected_devices: ClassVar[dict[RecordedPlaybackCapabilityKey, str]] = {}
    _probe_context: ClassVar[ContextVar[_RecordedPlaybackProbeContext | None]] = ContextVar(
        'recorded_playback_probe_context',
        default = None,
    )
    _probe_semaphore: ClassVar[asyncio.Semaphore] = asyncio.Semaphore(2)
    _probe_version: ClassVar[int] = 5
    _probe_timeout_seconds: ClassVar[float] = 20.0

    @classmethod
    async def getCapabilities(cls) -> list[RecordedPlaybackCapability]:
        """現在のFFmpeg 8とGPU環境に対応する能力行列を返す。

        Returns:
            全エンコーダー・コーデック・bit depthの能力検査結果。
        """

        while True:
            signature = await cls.__getSignature()
            async with cls._lock:
                generation = cls.__resetCacheForSignature(signature)
                if cls._result is not None:
                    return cls._result
                task = cls._matrix_inflight_tasks.get(generation)
                if task is None:
                    task = asyncio.create_task(
                        cls.__runMatrixProbeForGeneration(generation, signature),
                        name = f'RecordedPlaybackCapabilityProbe-matrix-{generation}',
                    )
                    cls._matrix_inflight_tasks[generation] = task

            result = await asyncio.shield(task)
            latest_signature = await cls.__getSignature()
            async with cls._lock:
                latest_generation = cls.__resetCacheForSignature(latest_signature)
                if (
                    result is not None
                    and latest_generation == generation
                    and latest_signature == signature
                ):
                    if cls._result is None:
                        cls._result = result
                    return cls._result

    @classmethod
    async def getCapability(
        cls,
        encoder: RecordedPlaybackEncoder,
        codec: RecordedPlaybackVideoCodec,
        bit_depth: RecordedPlaybackBitDepth,
        *,
        quality: QUALITY_TYPES | None = None,
    ) -> RecordedPlaybackCapability:
        """指定した1組・画質だけを実probeし、全32行の初回検査を強制せず返す。"""

        # 全行列の基礎能力と実画質能力を別キーにし、低解像度の成功を8Kへ流用しない。
        key: RecordedPlaybackCapabilityKey = (
            (encoder, codec, bit_depth)
            if quality is None
            else (encoder, codec, bit_depth, quality)
        )
        while True:
            signature = await cls.__getSignature()
            async with cls._lock:
                generation = cls.__resetCacheForSignature(signature)
                cached = cls._individual_results.get(key)
                if cached is not None:
                    return cached
                task_key = (generation, key)
                task = cls._inflight_tasks.get(task_key)
                if task is None:
                    task = asyncio.create_task(
                        cls.__runProbeForGeneration(
                            generation,
                            signature,
                            key,
                        ),
                        name = (
                            f'RecordedPlaybackCapabilityProbe-{generation}-'
                            f'{encoder}-{codec}-{bit_depth}-{quality or "baseline"}'
                        ),
                    )
                    cls._inflight_tasks[task_key] = task

            outcome = await asyncio.shield(task)
            latest_signature = await cls.__getSignature()
            async with cls._lock:
                latest_generation = cls.__resetCacheForSignature(latest_signature)
                if (
                    outcome is not None
                    and latest_generation == generation
                    and latest_signature == signature
                ):
                    capability, selected_device = outcome
                    cached = cls._individual_results.setdefault(key, capability)
                    if selected_device is not None:
                        cls._selected_devices.setdefault(key, selected_device)
                    return cached

    @classmethod
    def __resetCacheForSignature(cls, signature: str) -> int:
        """固定バイナリ署名が変わった場合だけ全能力cacheと選択deviceを破棄する。"""

        if cls._signature == signature:
            return cls._signature_generation
        cls._signature_generation += 1
        cls._result = None
        cls._individual_results.clear()
        cls._selected_devices.clear()
        cls._signature = signature
        return cls._signature_generation

    @classmethod
    def getSelectedDevice(
        cls,
        encoder: RecordedPlaybackEncoder,
        codec: RecordedPlaybackVideoCodec | None = None,
        bit_depth: RecordedPlaybackBitDepth | None = None,
        quality: QUALITY_TYPES | None = None,
    ) -> str | None:
        """能力組み合わせ・画質で実際に成功したrender nodeを返す。"""

        if codec is not None and bit_depth is not None:
            key: RecordedPlaybackCapabilityKey = (
                (encoder, codec, bit_depth)
                if quality is None
                else (encoder, codec, bit_depth, quality)
            )
            return cls._selected_devices.get(key)
        # CM解析のdecode専用候補ではcodecを固定できないため、同encoderで成功済みの任意deviceを返す。
        return next(
            (
                device
                for key, device in cls._selected_devices.items()
                if key[0] == encoder
            ),
            None,
        )

    @classmethod
    async def __getSignature(cls) -> str:
        """バイナリ更新時に能力キャッシュを破棄するための署名を返す。"""

        values = [str(cls._probe_version)]
        for key in ('FFmpeg8', 'FFmpeg8AMD', 'FFprobe8'):
            executable = Path(LIBRARY_PATH[key])
            try:
                stat = await asyncio.to_thread(executable.stat)
                values.append(f'{key}:{stat.st_size}:{stat.st_mtime_ns}')
            except OSError:
                values.append(f'{key}:missing')
        return hashlib.sha256(':'.join(values).encode()).hexdigest()

    @classmethod
    async def __runMatrixProbeForGeneration(
        cls,
        generation: int,
        signature: str,
    ) -> list[RecordedPlaybackCapability] | None:
        """1署名世代の全行列を共有Taskで構築し、安定した世代だけcacheする。"""

        try:
            result = await cls.__probeAllUsingIndividualCache(generation)
            if result is None:
                return None
            latest_signature = await cls.__getSignature()
            async with cls._lock:
                latest_generation = cls.__resetCacheForSignature(latest_signature)
                if latest_generation == generation and latest_signature == signature:
                    cls._result = result
            return result
        finally:
            async with cls._lock:
                current_task = asyncio.current_task()
                if cls._matrix_inflight_tasks.get(generation) is current_task:
                    cls._matrix_inflight_tasks.pop(generation, None)

    @classmethod
    async def __probeAllUsingIndividualCache(
        cls,
        generation: int,
    ) -> list[RecordedPlaybackCapability] | None:
        """全32組を2組ずつgetCapabilityへ渡し、exact probeの割り込み余地を保つ。"""

        encoders: tuple[RecordedPlaybackEncoder, ...] = ('FFmpeg', 'QSV', 'NVENC', 'AMF')
        codecs: tuple[RecordedPlaybackVideoCodec, ...] = ('avc', 'hevc', 'vp9', 'av1')
        bit_depths: tuple[RecordedPlaybackBitDepth, ...] = (8, 10)
        keys: list[RecordedPlaybackCapabilityBaseKey] = [
            (encoder, codec, bit_depth)
            for encoder in encoders
            for codec in codecs
            for bit_depth in bit_depths
        ]
        key_indexes = {key: index for index, key in enumerate(keys)}
        probe_order: list[tuple[int, RecordedPlaybackCapabilityBaseKey]] = [
            (key_indexes[(encoder, codec, bit_depth)], (encoder, codec, bit_depth))
            for codec in codecs
            for bit_depth in bit_depths
            for encoder in encoders
        ]
        results: dict[int, RecordedPlaybackCapability] = {}
        for index in range(0, len(probe_order), 2):
            async with cls._lock:
                if cls._signature_generation != generation:
                    return None
            chunk = probe_order[index:index + 2]
            capabilities = await asyncio.gather(*(
                cls.getCapability(encoder, codec, bit_depth)
                for _, (encoder, codec, bit_depth) in chunk
            ))
            for (result_index, _), capability in zip(chunk, capabilities, strict=True):
                results[result_index] = capability
        return [results[index] for index in range(len(keys))]

    @classmethod
    async def __runProbeForGeneration(
        cls,
        generation: int,
        signature: str,
        key: RecordedPlaybackCapabilityKey,
    ) -> tuple[RecordedPlaybackCapability, str | None] | None:
        """署名世代と能力キーに対応する唯一の実probeを実行する。"""

        task_key = (generation, key)
        encoder, codec, bit_depth = key[:3]
        quality = key[3] if len(key) == 4 else None
        try:
            # exact要求を全行列の後ろへ滞留させず、実probe総数は全backend合計2件に制限する。
            async with cls._probe_semaphore:
                async with cls._lock:
                    if cls._signature_generation != generation or cls._signature != signature:
                        return None
                context = _RecordedPlaybackProbeContext()
                token = cls._probe_context.set(context)
                try:
                    if quality is None:
                        capability = await cls.__probeOne(encoder, codec, bit_depth)
                    else:
                        capability = await cls.__probeOne(
                            encoder,
                            codec,
                            bit_depth,
                            quality = quality,
                        )
                finally:
                    cls._probe_context.reset(token)

            outcome = (capability, context.selected_device)
            latest_signature = await cls.__getSignature()
            async with cls._lock:
                latest_generation = cls.__resetCacheForSignature(latest_signature)
                if latest_generation == generation and latest_signature == signature:
                    cls._individual_results.setdefault(key, capability)
                    if context.selected_device is not None:
                        cls._selected_devices.setdefault(key, context.selected_device)
            return outcome
        finally:
            async with cls._lock:
                current_task = asyncio.current_task()
                if cls._inflight_tasks.get(task_key) is current_task:
                    cls._inflight_tasks.pop(task_key, None)

    @classmethod
    async def __communicateWithTimeout(
        cls,
        process: asyncio.subprocess.Process,
    ) -> tuple[bytes, bytes]:
        """
        probe subprocess を上限時間内に drain し、異常終了時も必ず回収する。

        Args:
            process (asyncio.subprocess.Process): encoder または FFprobe の subprocess。

        Returns:
            tuple[bytes, bytes]: subprocess の標準出力と標準エラー出力。
        """

        try:
            async with asyncio.timeout(cls._probe_timeout_seconds):
                stdout, stderr = await process.communicate()
                return stdout or b'', stderr or b''

        # TimeoutError だけでなく、呼び出し元 Task の CancelledError や communicate() 自体の例外でも
        ## 子プロセスを残留させない。BaseException は回収後にそのまま再送出する。
        except BaseException:
            if process.returncode is None:
                try:
                    process.kill()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    await process.wait()
                except (ProcessLookupError, OSError):
                    pass
            raise

    @classmethod
    async def __probeOne(
        cls,
        encoder: RecordedPlaybackEncoder,
        codec: RecordedPlaybackVideoCodec,
        bit_depth: RecordedPlaybackBitDepth,
        *,
        quality: QUALITY_TYPES | None = None,
    ) -> RecordedPlaybackCapability:
        """1つの能力キー・画質を実エンコードとFFprobe 8で検査する。"""

        spec = RecordedPlaybackBackend.getCodecSpec(codec, bit_depth)
        if RecordedPlaybackBackend.isCombinationSupported(encoder, codec, bit_depth) is False:
            return RecordedPlaybackCapability(encoder, codec, bit_depth, False, spec.profile, 'UnsupportedCombination')
        executable = Path(RecordedPlaybackBackend.getExecutable(encoder))
        ffprobe = Path(LIBRARY_PATH['FFprobe8'])
        if executable.is_file() is False or ffprobe.is_file() is False:
            return RecordedPlaybackCapability(encoder, codec, bit_depth, False, spec.profile, 'BinaryUnavailable')

        devices: list[str | None] = [None]
        if encoder in ('QSV', 'AMF'):
            # 同vendorでも世代ごとにcodec能力が異なるため、各能力キーで全deviceを試す。
            devices = [*RecordedPlaybackBackend.discoverRenderDevices(encoder)]
            if len(devices) == 0:
                return RecordedPlaybackCapability(encoder, codec, bit_depth, False, spec.profile, 'DeviceUnavailable')

        last_reason: RecordedPlaybackCapabilityReason = 'DeviceInitializationFailed'
        for device in devices:
            with tempfile.TemporaryDirectory(prefix='konomitv-bs4k-recorded-capability-') as temporary_directory:
                output_path = Path(temporary_directory) / 'probe.mp4'
                if quality is None:
                    command = RecordedPlaybackBackend.buildProbeCommand(
                        encoder,
                        codec,
                        bit_depth,
                        output_path,
                        device,
                    )
                else:
                    command = RecordedPlaybackBackend.buildProbeCommand(
                        encoder,
                        codec,
                        bit_depth,
                        output_path,
                        device,
                        quality = quality,
                    )
                try:
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        stdout = asyncio.subprocess.DEVNULL,
                        stderr = asyncio.subprocess.PIPE,
                        env = RecordedPlaybackBackend.getEnvironment(encoder),
                    )
                    _, stderr = await cls.__communicateWithTimeout(process)
                except TimeoutError:
                    last_reason = 'EncodeFailed'
                    continue
                except OSError:
                    last_reason = 'BinaryUnavailable'
                    continue
                if process.returncode != 0:
                    decoded_stderr = stderr.decode(errors='ignore').strip()
                    logging.warning(
                        '[RecordedPlaybackCapabilityProbe] Probe encode failed. '
                        f'[encoder: {encoder}, codec: {codec}, bit_depth: {bit_depth}, '
                        f'quality: {quality}, device: {device}, stderr: {decoded_stderr}]'
                    )
                    last_reason = cls.classifyFailure(decoded_stderr)
                    continue

                try:
                    probe_process = await asyncio.create_subprocess_exec(
                        str(ffprobe),
                        '-v',
                        'error',
                        '-count_frames',
                        '-show_streams',
                        '-of',
                        'json',
                        str(output_path),
                        stdout = asyncio.subprocess.PIPE,
                        stderr = asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await cls.__communicateWithTimeout(probe_process)
                except (OSError, TimeoutError):
                    last_reason = 'ProbeFailed'
                    continue
                if probe_process.returncode != 0:
                    last_reason = 'ProbeFailed'
                    continue
                try:
                    stream = json.loads(stdout)['streams'][0]
                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                    last_reason = 'ProbeFailed'
                    continue
                if stream.get('codec_name') != spec.ffprobe_codec_name:
                    last_reason = 'CodecMismatch'
                    continue
                if int(stream.get('nb_read_frames', 0)) < 2:
                    last_reason = 'ProbeFailed'
                    continue
                if quality is not None and (
                    int(stream.get('width', 0)) != QUALITY[quality].width
                    or int(stream.get('height', 0)) != QUALITY[quality].height
                ):
                    last_reason = 'ProbeFailed'
                    continue
                if quality is not None:
                    try:
                        frame_rate_numerator, frame_rate_denominator = (
                            int(value)
                            for value in str(stream.get('r_frame_rate', '0/1')).split('/', maxsplit=1)
                        )
                        actual_frame_rate = frame_rate_numerator / frame_rate_denominator
                    except (ValueError, ZeroDivisionError):
                        last_reason = 'ProbeFailed'
                        continue
                    expected_frame_rate = 60000 / 1001 if QUALITY[quality].is_60fps is True else 30000 / 1001
                    if abs(actual_frame_rate - expected_frame_rate) > 0.01:
                        last_reason = 'ProbeFailed'
                        continue
                actual_pixel_format = str(stream.get('pix_fmt', ''))
                is_10bit = actual_pixel_format in ('yuv420p10le', 'p010le', 'p010')
                if is_10bit != (bit_depth == 10):
                    last_reason = 'BitDepthMismatch'
                    continue
                if str(stream.get('profile', '')).lower().replace(' ', '') != spec.profile.lower().replace(' ', ''):
                    last_reason = 'ProfileMismatch'
                    continue
                if device is not None:
                    context = cls._probe_context.get()
                    if context is None:
                        # private probeを単体利用する既存経路では従来どおり即時公開する。
                        key: RecordedPlaybackCapabilityKey = (
                            (encoder, codec, bit_depth)
                            if quality is None
                            else (encoder, codec, bit_depth, quality)
                        )
                        cls._selected_devices[key] = device
                    else:
                        # 通常経路では署名再確認後にだけ現在世代へ反映する。
                        context.selected_device = device
                return RecordedPlaybackCapability(encoder, codec, bit_depth, True, spec.profile, None)

        return RecordedPlaybackCapability(encoder, codec, bit_depth, False, spec.profile, last_reason)

    @staticmethod
    def classifyFailure(stderr: str) -> RecordedPlaybackCapabilityReason:
        """FFmpegの初期化失敗を能力APIの安定理由コードへ正規化する。"""

        normalized_stderr = stderr.lower()
        if any(message in normalized_stderr for message in (
            'cannot load libcuda',
            'cannot load libnvidia-encode',
            'no va display found',
            'no such file or directory',
        )):
            return 'DeviceUnavailable'
        if any(message in normalized_stderr for message in (
            'device creation failed',
            'failed to initialise vaapi connection',
            'device setup failed',
            'failed to initialize encoder',
        )):
            return 'DeviceInitializationFailed'
        if any(message in normalized_stderr for message in (
            'no such filter',
            'filter not found',
            'error initializing filter',
        )):
            return 'FilterUnavailable'
        return 'EncodeFailed'
