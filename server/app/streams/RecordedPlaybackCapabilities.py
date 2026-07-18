from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal, cast

from app import logging
from app.constants import LIBRARY_PATH


RecordedPlaybackEncoder = Literal['FFmpeg', 'QSVEncC', 'NVEncC', 'VCEEncC']
RecordedPlaybackVideoCodec = Literal['avc', 'hevc', 'vp9', 'av1']
RecordedPlaybackBitDepth = Literal[8, 10]
RecordedPlaybackCapabilityReason = Literal[
    'BinaryUnavailable',
    'DeviceUnavailable',
    'DeviceInitializationFailed',
    'FilterUnavailable',
    'EncodeFailed',
    'ProbeFailed',
    'CodecMismatch',
    'BitDepthMismatch',
    'ProfileMismatch',
    'UnsupportedCombination',
]


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


class RecordedPlaybackBackend:
    """能力検査と実再生で共有するFFmpeg 8バックエンド定義を提供する。"""

    _ENCODERS: ClassVar[dict[RecordedPlaybackEncoder, dict[RecordedPlaybackVideoCodec, str | None]]] = {
        'FFmpeg': {'avc': 'libx264', 'hevc': 'libx265', 'vp9': 'libvpx-vp9', 'av1': 'libaom-av1'},
        'QSVEncC': {'avc': 'h264_qsv', 'hevc': 'hevc_qsv', 'vp9': 'vp9_qsv', 'av1': 'av1_qsv'},
        'NVEncC': {'avc': 'h264_nvenc', 'hevc': 'hevc_nvenc', 'vp9': None, 'av1': 'av1_nvenc'},
        'VCEEncC': {'avc': 'h264_amf', 'hevc': 'hevc_amf', 'vp9': None, 'av1': 'av1_amf'},
    }

    _VENDOR_IDS: ClassVar[dict[RecordedPlaybackEncoder, str]] = {
        'QSVEncC': '0x8086',
        'VCEEncC': '0x1002',
    }

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

        return LIBRARY_PATH['FFmpeg8AMD'] if encoder == 'VCEEncC' else LIBRARY_PATH['FFmpeg8']

    @staticmethod
    def getEnvironment(encoder: RecordedPlaybackEncoder) -> dict[str, str]:
        """録画用FFmpeg 8に限定したGPU runtime環境を返す。

        Args:
            encoder: 公開設定上のエンコーダー名。

        Returns:
            親プロセス環境を継承した実行環境。
        """

        environment = os.environ.copy()
        if encoder == 'QSVEncC':
            # iHD driverは同梱libva 2.23 ABIでビルドされているため、Ubuntu 22.04の
            # system libva 1.22と混在させず、QSV用FFmpeg 8プロセス内だけで一式を固定する。
            library_path = Path(LIBRARY_PATH['FFmpeg8']).parent.parent / 'Library'
            current_library_path = environment.get('LD_LIBRARY_PATH')
            environment['LD_LIBRARY_PATH'] = (
                f'{library_path}:{current_library_path}' if current_library_path else str(library_path)
            )
            environment['LIBVA_DRIVER_NAME'] = 'iHD'
            environment['LIBVA_DRIVERS_PATH'] = str(library_path / 'dri')
        return environment

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
    ) -> list[str]:
        """能力検査用の短いfMP4生成コマンドを構築する。

        Args:
            encoder: 公開設定上のエンコーダー名。
            codec: 出力映像コーデック。
            bit_depth: 出力bit depth。
            output_path: 検査用fMP4の出力先。
            device: QSV/AMFで使うrender node。CPU/NVENCではNone。

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
        if encoder == 'QSVEncC':
            if device is None:
                raise ValueError('QSV render device is required.')
            command += ['-init_hw_device', f'qsv=recorded_qsv:{device}', '-filter_hw_device', 'recorded_qsv']
        elif encoder == 'NVEncC':
            command += ['-init_hw_device', 'cuda=recorded_cuda:0', '-filter_hw_device', 'recorded_cuda']
        elif encoder == 'VCEEncC':
            if device is None:
                raise ValueError('AMD render device is required.')
            command += ['-init_hw_device', f'vaapi=recorded_vaapi:{device}', '-filter_hw_device', 'recorded_vaapi']

        # 一部のQSV HEVC/AV1 encoderは極端に小さい解像度を拒否するため、
        # 能力判定がfalse negativeにならない最小限の16:9 fixtureを使う。
        command += ['-f', 'lavfi', '-i', 'testsrc2=size=320x192:rate=10:duration=0.6']
        if encoder == 'FFmpeg':
            command += ['-vf', f'format={spec.pixel_format}']
        elif encoder == 'QSVEncC':
            command += ['-vf', f'format={spec.encoder_pixel_format},hwupload=extra_hw_frames=32']
        elif encoder == 'NVEncC':
            command += ['-vf', f'format={spec.encoder_pixel_format},hwupload_cuda']
        else:
            # AMF自体はsystem-memoryのNV12/P010を受けるため、能力検査でも実再生と同じ
            # VAAPI upload/download境界を通し、ドライバーとAMFの両方を検査する。
            command += [
                '-vf',
                f'format={spec.encoder_pixel_format},hwupload,scale_vaapi=w=320:h=192,'
                f'hwdownload,format={spec.encoder_pixel_format}',
            ]

        command += ['-an', '-c:v', ffmpeg_encoder]
        # QSV/CUDAはhwupload後のhardware frame formatをそのままencoderへ渡す。
        # software pixel formatを出力側で強制すると、不要なauto_scaleが挿入され接続できない。
        if encoder == 'FFmpeg':
            command += ['-pix_fmt', spec.pixel_format]
        elif encoder == 'NVEncC':
            # NVENCへCUDA frameを渡すことを明示し、software nv12/p010leへ戻す
            # auto_scaleがhwupload_cuda後へ挿入されるのを防ぐ。
            command += ['-pix_fmt', 'cuda']
        elif encoder == 'VCEEncC':
            command += ['-pix_fmt', spec.encoder_pixel_format]
        if codec == 'avc':
            command += ['-profile:v', 'high']
        elif codec == 'hevc':
            command += ['-profile:v', 'main10' if bit_depth == 10 else 'main']
        elif codec == 'vp9':
            command += [
                '-profile:v',
                ('profile2' if bit_depth == 10 else 'profile0') if encoder == 'QSVEncC' else
                ('2' if bit_depth == 10 else '0'),
            ]
        elif codec == 'av1':
            # av1_nvencはprofileオプション自体を公開しておらず、指定すると起動前に失敗する。
            # libaom-av1は数値、QSV/AMFはmainを受け付けるためバックエンド別に指定する。
            if encoder == 'FFmpeg':
                command += ['-profile:v', '0']
            elif encoder != 'NVEncC':
                command += ['-profile:v', 'main']
        command += cls.getTuningArguments(encoder, codec)
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
    _signature: ClassVar[str | None] = None
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    _selected_devices: ClassVar[dict[RecordedPlaybackEncoder, str]] = {}
    _probe_version: ClassVar[int] = 2

    @classmethod
    async def getCapabilities(cls) -> list[RecordedPlaybackCapability]:
        """現在のFFmpeg 8とGPU環境に対応する能力行列を返す。

        Returns:
            全エンコーダー・コーデック・bit depthの能力検査結果。
        """

        async with cls._lock:
            signature = await cls.__getSignature()
            if cls._result is not None and cls._signature == signature:
                return cls._result
            cls._selected_devices.clear()
            cls._result = await cls.__probeAll()
            cls._signature = signature
            return cls._result

    @classmethod
    def getSelectedDevice(cls, encoder: RecordedPlaybackEncoder) -> str | None:
        """能力検査で最初に初期化できたrender nodeを返す。"""

        return cls._selected_devices.get(encoder)

    @classmethod
    async def __getSignature(cls) -> str:
        """バイナリ更新時に能力キャッシュを破棄するための署名を返す。"""

        executable = Path(LIBRARY_PATH['FFmpeg8'])
        if executable.is_file() is False:
            return f'probe-{cls._probe_version}-missing'
        stat = await asyncio.to_thread(executable.stat)
        value = f'{cls._probe_version}:{stat.st_size}:{stat.st_mtime_ns}'
        return hashlib.sha256(value.encode()).hexdigest()

    @classmethod
    async def __probeAll(cls) -> list[RecordedPlaybackCapability]:
        """能力行列の全組み合わせを順番に検査する。"""

        results: list[RecordedPlaybackCapability] = []
        for encoder in cast(tuple[RecordedPlaybackEncoder, ...], ('FFmpeg', 'QSVEncC', 'NVEncC', 'VCEEncC')):
            for codec in cast(tuple[RecordedPlaybackVideoCodec, ...], ('avc', 'hevc', 'vp9', 'av1')):
                for bit_depth in cast(tuple[RecordedPlaybackBitDepth, ...], (8, 10)):
                    results.append(await cls.__probeOne(encoder, codec, bit_depth))
        return results

    @classmethod
    async def __probeOne(
        cls,
        encoder: RecordedPlaybackEncoder,
        codec: RecordedPlaybackVideoCodec,
        bit_depth: RecordedPlaybackBitDepth,
    ) -> RecordedPlaybackCapability:
        """1つの能力キーを実エンコードとFFprobe 8で検査する。"""

        spec = RecordedPlaybackBackend.getCodecSpec(codec, bit_depth)
        if RecordedPlaybackBackend.isCombinationSupported(encoder, codec, bit_depth) is False:
            return RecordedPlaybackCapability(encoder, codec, bit_depth, False, spec.profile, 'UnsupportedCombination')
        executable = Path(RecordedPlaybackBackend.getExecutable(encoder))
        ffprobe = Path(LIBRARY_PATH['FFprobe8'])
        if executable.is_file() is False or ffprobe.is_file() is False:
            return RecordedPlaybackCapability(encoder, codec, bit_depth, False, spec.profile, 'BinaryUnavailable')

        devices: list[str | None] = [None]
        if encoder in ('QSVEncC', 'VCEEncC'):
            selected_device = cls._selected_devices.get(encoder)
            devices = [selected_device] if selected_device is not None else [
                *RecordedPlaybackBackend.discoverRenderDevices(encoder),
            ]
            if len(devices) == 0:
                return RecordedPlaybackCapability(encoder, codec, bit_depth, False, spec.profile, 'DeviceUnavailable')

        last_reason: RecordedPlaybackCapabilityReason = 'DeviceInitializationFailed'
        for device in devices:
            with tempfile.TemporaryDirectory(prefix='konomitv-recorded-capability-') as temporary_directory:
                output_path = Path(temporary_directory) / 'probe.mp4'
                command = RecordedPlaybackBackend.buildProbeCommand(encoder, codec, bit_depth, output_path, device)
                try:
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        stdout = asyncio.subprocess.DEVNULL,
                        stderr = asyncio.subprocess.PIPE,
                        env = RecordedPlaybackBackend.getEnvironment(encoder),
                    )
                    _, stderr = await process.communicate()
                except OSError:
                    last_reason = 'BinaryUnavailable'
                    continue
                if process.returncode != 0:
                    decoded_stderr = stderr.decode(errors='ignore').strip()
                    logging.warning(
                        '[RecordedPlaybackCapabilityProbe] Probe encode failed. '
                        f'[encoder: {encoder}, codec: {codec}, bit_depth: {bit_depth}, '
                        f'device: {device}, stderr: {decoded_stderr}]'
                    )
                    last_reason = cls.classifyFailure(decoded_stderr)
                    continue

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
                stdout, _ = await probe_process.communicate()
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
                actual_pixel_format = str(stream.get('pix_fmt', ''))
                is_10bit = actual_pixel_format in ('yuv420p10le', 'p010le', 'p010')
                if is_10bit != (bit_depth == 10):
                    last_reason = 'BitDepthMismatch'
                    continue
                if str(stream.get('profile', '')).lower().replace(' ', '') != spec.profile.lower().replace(' ', ''):
                    last_reason = 'ProfileMismatch'
                    continue
                if device is not None:
                    cls._selected_devices.setdefault(encoder, device)
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
