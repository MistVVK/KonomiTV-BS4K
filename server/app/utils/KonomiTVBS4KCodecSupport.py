"""コーデック対応 (KonomiTV-BS4K サーバー診断) の共有ジョブを実行する。

GPU の列挙・実 probe はホストの詳細情報を扱い、FFmpeg などの外部プロセスを多数起動する。
そのため管理者が明示的に開始したときだけ共有ジョブとして実行し、結果は環境署名ごとに
プロセス内でキャッシュする。応答へは render node パス・PCI BDF・GPU UUID・stderr 全文を
含めない。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal

from app import logging
from app.constants import LIBRARY_PATH, QUALITY, QUALITY_TYPES
from app.schemas import (
    KonomiTVBS4KCodecSupportBackend,
    KonomiTVBS4KCodecSupportCapability,
    KonomiTVBS4KCodecSupportDevice,
    KonomiTVBS4KCodecSupportDeviceKind,
    KonomiTVBS4KCodecSupportDeviceVendor,
    KonomiTVBS4KCodecSupportEvidence,
    KonomiTVBS4KCodecSupportJob,
    KonomiTVBS4KCodecSupportMediaType,
    KonomiTVBS4KCodecSupportOperationStatus,
    KonomiTVBS4KCodecSupportOperationSupport,
    KonomiTVBS4KCodecSupportReasonCode,
    KonomiTVBS4KCodecSupportStatus,
)
from app.streams.KonomiTVBS4KExternalProcessLimiter import (
    KonomiTVBS4KExternalProcessLimiter,
)
from app.streams.RecordedPlaybackCapabilities import (
    RecordedPlaybackBackend,
    RecordedPlaybackCapabilityProbe,
    RecordedPlaybackCodecSpec,
)


# 映像検査カタログ: (codec, profile, bit_depth, ffprobe の codec 名, KonomiTV 本線か)。
# CPU は全行を実 probe し、GPU は KonomiTV 本線を実 probe、それ以外を Driver 根拠に留める。
_VIDEO_CAPABILITIES: tuple[tuple[str, str | None, Literal[8, 10], str, bool], ...] = (
    ('avc', 'High', 8, 'h264', True),
    ('hevc', 'Main', 8, 'hevc', True),
    ('hevc', 'Main 10', 10, 'hevc', True),
    ('vp9', 'Profile 0', 8, 'vp9', True),
    ('vp9', 'Profile 2', 10, 'vp9', True),
    ('av1', 'Main', 8, 'av1', True),
    ('av1', 'Main', 10, 'av1', True),
    ('mpeg1', None, 8, 'mpeg1video', False),
    ('mpeg2', None, 8, 'mpeg2video', False),
    ('mpeg4', None, 8, 'mpeg4', False),
    ('vc1', None, 8, 'vc1', False),
)

# CPU ソフトウェア decode / encode 用の FFmpeg codec 名 (FFmpeg 8 既定ビルド)。
_VIDEO_SOFTWARE_DECODERS: dict[str, str] = {
    'avc': 'h264',
    'hevc': 'hevc',
    'vp9': 'vp9',
    'av1': 'av1',
    'mpeg1': 'mpeg1video',
    'mpeg2': 'mpeg2video',
    'mpeg4': 'mpeg4',
    'vc1': 'vc1',
}
_VIDEO_SOFTWARE_ENCODERS: dict[str, str | None] = {
    'avc': 'libx264',
    'hevc': 'libx265',
    'vp9': 'libvpx-vp9',
    'av1': 'libaom-av1',
    # 本線外の映像は FFmpeg にネイティブ encoder がほとんどないため、あれば Binary 根拠にする
    'mpeg1': 'mpeg1video',
    'mpeg2': 'mpeg2video',
    'mpeg4': 'mpeg4',
    # VC-1 のネイティブ encoder は FFmpeg 8 に無い。wmv2 は WMV8 であり根拠にしない。
    'vc1': None,
}

# GPU の hwaccel decode が名目上サポートする映像 codec (FFmpeg 8 ビルド)。
# CUDA (cuvid) は本線 4 コーデックのほかに mpeg1/2/4 にも専用 decoder を持ち、
# これらはソフトウェア encoder で入力を生成できるため実 probe 対象になる
# (_HWACCEL_VIDEO_DECODER_NAMES に載り入力を作れない codec だけが Driver 根拠に留まる)。
_HWACCEL_VIDEO_DECODERS: dict[str, frozenset[str]] = {
    'qsv': frozenset({'avc', 'hevc', 'vp9', 'av1'}),
    'cuda': frozenset({'avc', 'hevc', 'vp9', 'av1', 'mpeg1', 'mpeg2', 'mpeg4'}),
    'vaapi': frozenset({'avc', 'hevc', 'vp9', 'av1'}),
}

# QSV / CUVID は専用 decoder を明示し、-hwaccel の初期化失敗後に software decoder へ
# フォールバックした処理を hardware decode 成功と誤認しないようにする。
_HWACCEL_VIDEO_DECODER_NAMES: dict[str, dict[str, str]] = {
    'qsv': {
        'avc': 'h264_qsv',
        'hevc': 'hevc_qsv',
        'vp9': 'vp9_qsv',
        'av1': 'av1_qsv',
    },
    'cuda': {
        'avc': 'h264_cuvid',
        'hevc': 'hevc_cuvid',
        'vp9': 'vp9_cuvid',
        'av1': 'av1_cuvid',
        'mpeg1': 'mpeg1_cuvid',
        'mpeg2': 'mpeg2_cuvid',
        'mpeg4': 'mpeg4_cuvid',
    },
}

# 音声検査カタログ: (codec, profile, encoder 名, decoder 名, KonomiTV 本線か)。
# CPU の表示行はすべて実 probe し、AAC の profile / framing も生成コマンドで区別する。
_AUDIO_CAPABILITIES: tuple[tuple[str, str | None, str | None, str | None, bool], ...] = (
    ('aac_lc', 'LC', 'aac', 'aac', True),
    ('he_aac', 'HE-AAC', 'aac', 'aac', False),
    ('aac_latm', 'LATM', 'aac', 'aac_latm', False),
    ('mp2', None, 'mp2', 'mp2', False),
    ('mp3', None, 'libmp3lame', 'mp3', False),
    ('ac3', None, 'ac3', 'ac3', False),
    ('eac3', None, 'eac3', 'eac3', False),
    ('opus', None, 'libopus', 'opus', True),
    ('vorbis', None, 'libvorbis', 'vorbis', False),
    ('flac', None, 'flac', 'flac', False),
    ('lpcm', None, 'pcm_s16le', 'pcm_s16le', False),
)

# 音声 codec 単位の FFmpeg encoder / decoder トークン名 (バイナリ一覧の照合に使う)。
_AUDIO_CODEC_TOKENS: dict[str, tuple[str | None, str | None]] = {
    codec: (encoder, decoder)
    for codec, _profile, encoder, decoder, _used in _AUDIO_CAPABILITIES
}

# 失敗しても「もう動かない」と断定してよい理由コード。
# EncodeFailed / DecodeFailed は device busy・メモリ不足など一時障害も含むため、
# RecordedPlaybackCapabilityProbe と同じく決定的失敗にしない (Unknown のままキャッシュする)。
# UnsupportedByDevice は NVENC が encoder open 時に返す能力不足エラー由来で、
# 同じ device では再試行しても成功しないため決定的失敗に含める。
_DETERMINISTIC_FAILURE_REASON_CODES: frozenset[str] = frozenset({
    'EncoderUnavailable',
    'DecoderUnavailable',
    'CodecMismatch',
    'BitDepthMismatch',
    'ProfileMismatch',
    'FilterUnavailable',
    'UnsupportedCombination',
    'UnsupportedByDevice',
})

# サーバー診断の映像実 probe は KonomiTV-BS4K の地上波向け最高画質に揃える。
# 4K / 8K の能力までは断定せず、画面にはこの検査構成を明示する。
_VIDEO_PROBE_QUALITY: QUALITY_TYPES = '1080p-60fps'
_VIDEO_PROBE_CONFIGURATION = (
    f'{QUALITY[_VIDEO_PROBE_QUALITY].width}x{QUALITY[_VIDEO_PROBE_QUALITY].height} '
    f'{60 if QUALITY[_VIDEO_PROBE_QUALITY].is_60fps is True else 30}fps'
)


@dataclass
class _NvidiaGpu:
    """nvidia-smi から得た NVIDIA GPU の識別情報 (応答には PCI BDF を含めない)。"""

    name: str
    pci_bus_id: str


@dataclass
class _CodecSupportOperation:
    """1つの decode / encode operation の検査結果。"""

    status: KonomiTVBS4KCodecSupportOperationStatus
    evidence: KonomiTVBS4KCodecSupportEvidence
    backend: KonomiTVBS4KCodecSupportBackend | None
    reason_code: KonomiTVBS4KCodecSupportReasonCode | None
    tested_configuration: str | None


@dataclass
class _CodecSupportCapability:
    """1つの映像・音声コーデックの decode / encode 能力。"""

    media_type: KonomiTVBS4KCodecSupportMediaType
    codec: str
    profile: str | None
    bit_depth: Literal[8, 10] | None
    decode: _CodecSupportOperation
    encode: _CodecSupportOperation
    used_by_konomitv_bs4k: bool


@dataclass
class _CodecSupportDevice:
    """診断対象の CPU / GPU。device_path・cuda_ordinal は probe 専用で応答へ含めない。"""

    id: str
    label: str
    kind: KonomiTVBS4KCodecSupportDeviceKind
    vendor: KonomiTVBS4KCodecSupportDeviceVendor
    backend: KonomiTVBS4KCodecSupportBackend
    decode_accel: str
    device_path: str | None
    cuda_ordinal: int | None
    capabilities: list[_CodecSupportCapability]


@dataclass
class _CodecSupportJobState:
    """共有ジョブの状態。応答は __buildResponse() で機密を除去して生成する。"""

    status: KonomiTVBS4KCodecSupportStatus
    progress: float
    environment_signature: str | None
    devices: list[_CodecSupportDevice]
    total_operations: int
    completed_operations: int


@dataclass
class _CodecSupportProbeOperation:
    """実行する1つの外部プロセス操作 (クリップ生成 / 実 probe) を表す。"""

    name: str
    kind: str
    device: _CodecSupportDevice
    capability: _CodecSupportCapability | None
    side: str | None
    codec: str
    bit_depth: int | None
    clip_key: str | None
    output_path: Path | None
    tested_configuration: str | None

    def applyResult(self, success: bool, reason_code: KonomiTVBS4KCodecSupportReasonCode | None) -> None:
        """実 probe の結果を保持する capability の decode / encode cell へ反映する。"""

        if self.capability is None or self.side is None:
            return
        cell = self.capability.decode if self.side == 'decode' else self.capability.encode
        cell.backend = self.device.backend
        cell.tested_configuration = self.tested_configuration
        if success is True:
            cell.status = 'Supported'
            cell.evidence = 'VerifiedProbe'
            cell.reason_code = None
            return
        assert reason_code is not None
        if reason_code in _DETERMINISTIC_FAILURE_REASON_CODES:
            cell.status = 'Unsupported'
        else:
            cell.status = 'Unknown'
        cell.evidence = 'ProbeFailed'
        cell.reason_code = reason_code


@dataclass
class _CodecSupportProbePlan:
    """ジョブ全体で実行する外部プロセス操作の計画。"""

    operations: list[_CodecSupportProbeOperation] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.operations)


class KonomiTVBS4KCodecSupportJobManager:
    """管理者専用のコーデック対応サーバー診断ジョブを実行してキャッシュする。"""

    # probe 実装を変更したら必ず増加させ、環境署名を無効化して再実行させる
    _probe_version: ClassVar[int] = 4
    _probe_timeout_seconds: ClassVar[float] = 20.0
    _job_timeout_seconds: ClassVar[float] = 600.0
    _nvidia_smi_timeout_seconds: ClassVar[float] = 10.0
    _cancel_reclaim_timeout_seconds: ClassVar[float] = 30.0
    _max_cached_signatures: ClassVar[int] = 8

    _state: ClassVar[_CodecSupportJobState] = _CodecSupportJobState(
        status = 'Idle',
        progress = 0.0,
        environment_signature = None,
        devices = [],
        total_operations = 0,
        completed_operations = 0,
    )
    _task: ClassVar[asyncio.Task[None] | None] = None
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    _results_cache: ClassVar[dict[str, _CodecSupportJobState]] = {}

    @classmethod
    async def getState(cls) -> KonomiTVBS4KCodecSupportJob:
        """現在のジョブ状態 (実行中は部分結果を含む) を応答として返す。

        Returns:
            実行中は現在の部分結果、キャンセル後の Idle は同一環境署名の直近成功結果。
            再診断が失敗した場合は古い成功結果で隠さず Failed の現在状態。
        """

        async with cls._lock:
            state = cls._state
            # キャンセル後だけ直近成功結果を復元する。force 再診断の失敗は、利用者が
            # 失敗と部分結果を観測して再試行できるよう Failed のまま返す。
            if state.status == 'Idle' and state.environment_signature is not None:
                cached_state = cls._results_cache.get(state.environment_signature)
                if cached_state is not None:
                    state = cached_state
            return cls.__buildResponse(state)

    @classmethod
    async def startProbe(cls, *, force: bool) -> tuple[bool, KonomiTVBS4KCodecSupportJob]:
        """共有診断ジョブを開始し、または実行中ジョブへ合流する。

        Args:
            force: 同一環境署名の結果キャッシュがある場合でも再実行する。

        Returns:
            (キャッシュ以外を利用したか (新規開始・実行中ジョブへの合流)、現在の状態の応答)。
            True の場合は呼び出し側が 202 を、キャッシュ再利用 (False) の場合は 200 を返す。
        """

        signature = await cls.getEnvironmentSignature()
        async with cls._lock:
            if cls._task is not None and cls._task.done() is False:
                # 実行中ジョブへ合流した。診断は継続されるためキャッシュ再利用 (200) と区別して 202 扱いにする
                return True, cls.__buildResponse(cls._state)
            cached_state = cls._results_cache.get(signature)
            # 現在状態が Failed のときは旧成功キャッシュへ戻さず再実行する。
            # GET が Failed を隠さない契約と合わせ、再診断失敗後に成功結果が 200 で返るのを防ぐ
            if cached_state is not None and force is False and cls._state.status != 'Failed':
                return False, cls.__buildResponse(cached_state)
            cls._state = _CodecSupportJobState(
                status = 'Running',
                progress = 0.0,
                environment_signature = signature,
                devices = [],
                total_operations = 0,
                completed_operations = 0,
            )
            cls._task = asyncio.create_task(
                cls.__runJob(signature),
                name = 'KonomiTVBS4KCodecSupport-diagnostic',
            )
            return True, cls.__buildResponse(cls._state)

    @classmethod
    async def cancelProbe(cls) -> Literal['Idle', 'Cancelled', 'ReclaimTimeout']:
        """実行中ジョブをキャンセルし、子プロセス回収後の状態更新を待つ。

        Returns:
            Idle: 実行中ジョブがなかった (冪等)。
            Cancelled: キャンセルし、子プロセスの回収まで完了した。
            ReclaimTimeout: 回収が時間内に終わらず、外部プロセスが残っている可能性がある。
        """

        async with cls._lock:
            task = cls._task
        if task is None or task.done() is True:
            return 'Idle'
        task.cancel()
        # job Task は finally で子プロセスを回収して完了する。回収が終わるまで 204 を返さない。
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout = cls._cancel_reclaim_timeout_seconds,
            )
        except TimeoutError:
            logging.error(
                '[KonomiTVBS4KCodecSupport] Cancel timed out before child processes were reclaimed.',
            )
            return 'ReclaimTimeout'
        except asyncio.CancelledError:
            if task.done() is False:
                logging.error(
                    '[KonomiTVBS4KCodecSupport] Cancel waiter was cancelled before reclaim finished.',
                )
                return 'ReclaimTimeout'
            return 'Cancelled'
        if task.done() is False:
            return 'ReclaimTimeout'
        return 'Cancelled'

    @classmethod
    async def stop(cls) -> None:
        """アプリ終了時に実行中ジョブをキャンセルして子プロセスを回収する。"""

        result = await cls.cancelProbe()
        if result == 'ReclaimTimeout':
            # shutdown を止めても回収は進まないため、残存プロセスを残して終了する。
            logging.error(
                '[KonomiTVBS4KCodecSupport] Shutdown finished with unreclaimed probe processes.',
            )

    @classmethod
    async def _runProbeOperation(
        cls,
        operation: _CodecSupportProbeOperation,
        clips: dict[str, Path | None],
        on_complete: Callable[[], None],
    ) -> None:
        """slot を取得して操作の外部プロセスを実行し、結果を cell へ反映する。

        Args:
            operation: 実行する probe 操作。
            clips: クリップ生成操作の出力先を共有する dict。
            on_complete: 操作完了 (成功・失敗・キャンセル) 時に呼ばれる進捗報告のコールバック。
        """

        succeeded: bool | None = None
        try:
            async with KonomiTVBS4KExternalProcessLimiter.acquireSlot():
                if operation.kind == 'video_clip':
                    succeeded = await cls.__runVideoClip(operation.codec, operation.bit_depth, operation.output_path)
                elif operation.kind == 'audio_clip':
                    succeeded = await cls.__runAudioClip(operation.codec, operation.output_path)
                elif operation.kind == 'video_encode':
                    success, reason = await cls.__runVideoEncodeProbe(
                        operation.device,
                        operation.codec,
                        operation.bit_depth,
                        operation.output_path,
                    )
                    operation.applyResult(success, reason)
                    return
                elif operation.kind == 'video_decode':
                    clip = clips[operation.clip_key or '']
                    if clip is None or clip.is_file() is False:
                        # 入力クリップが生成できなかった decode は検証不能として扱う
                        operation.applyResult(False, 'ProbeInputUnavailable')
                        return
                    success, reason = await cls.__runVideoDecodeProbe(
                        operation.device,
                        operation.codec,
                        operation.bit_depth,
                        clip,
                    )
                    operation.applyResult(success, reason)
                    return
                elif operation.kind == 'audio_encode':
                    success, reason = await cls.__runAudioEncodeProbe(operation.codec, operation.output_path)
                    operation.applyResult(success, reason)
                    return
                else:  # audio_decode
                    clip = clips[operation.clip_key or '']
                    if clip is None or clip.is_file() is False:
                        operation.applyResult(False, 'ProbeInputUnavailable')
                        return
                    success, reason = await cls.__runAudioDecodeProbe(operation.codec, clip)
                    operation.applyResult(success, reason)
                    return
            # クリップ生成操作は cell を持たず、共有 dict へ結果だけ残す
            if operation.kind in ('video_clip', 'audio_clip'):
                clips[operation.clip_key or ''] = operation.output_path if succeeded is True else None
        except asyncio.CancelledError:
            raise
        except Exception as exception:
            # 想定外でも job 全体を落とさず、該当 cell を ProbeFailed として残す
            logging.warning(
                f'[KonomiTVBS4KCodecSupport] Probe operation failed. [operation: {operation.name}]',
                exc_info = exception,
            )
            if operation.capability is not None:
                operation.applyResult(False, 'ProbeFailed')
        finally:
            on_complete()

    @classmethod
    async def runSubprocess(
        cls,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, bytes, bytes]:
        """診断用の外部プロセスを上限時間内に実行し、例外時も必ず回収する。

        Args:
            args: create_subprocess_exec() へ渡す引数列。
            env: 実行環境。None の場合は親環境を継承する。
            timeout: 上限時間 (秒)。None の場合は probe 上限を使う。

        Returns:
            (returncode, stdout, stderr)。
        """

        process = await asyncio.create_subprocess_exec(
            *args,
            stdout = asyncio.subprocess.PIPE,
            stderr = asyncio.subprocess.PIPE,
            env = env,
        )
        try:
            async with asyncio.timeout(timeout if timeout is not None else cls._probe_timeout_seconds):
                stdout, stderr = await process.communicate()
            return process.returncode if process.returncode is not None else -1, stdout or b'', stderr or b''
        except BaseException:
            # timeout / キャンセル / 予期しない例外でも子プロセスを残留させない
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
    async def listBinaryCodecNames(
        cls,
        kind: Literal['encoders', 'decoders'],
    ) -> set[str] | None:
        """FFmpeg 8 バイナリが持つ encoder / decoder 名の一覧をパースして返す。

        Args:
            kind: 'encoders' または 'decoders'。

        Returns:
            codec 名の集合。バイナリ不在・実行失敗時は None。
        """

        executable = Path(LIBRARY_PATH['FFmpeg8'])
        if executable.is_file() is False:
            return None
        try:
            # 外部プロセス総数上限の対象外にならないよう slot を取得する
            async with KonomiTVBS4KExternalProcessLimiter.acquireSlot():
                # FFmpeg の一覧はハイフン付きの -encoders / -decoders。ハイフンなしだと出力ファイル名と解釈される。
                returncode, stdout, _ = await cls.runSubprocess([str(executable), '-hide_banner', f'-{kind}'])
        except (OSError, TimeoutError):
            return None
        if returncode != 0:
            return None
        names: set[str] = set()
        for line in stdout.decode(errors='ignore').splitlines():
            # ' V....D libaom-av1  libaom AV1' 形式。先頭が V/A/S、続く 5 文字は F/S/X/B/D などのフラグ。
            match = re.match(r'^\s*[VAS][.A-Z]{5}\s+([A-Za-z0-9_-]+)', line)
            if match is not None:
                names.add(match.group(1))
        return names

    @classmethod
    async def queryNvidiaGpus(cls) -> list[_NvidiaGpu] | None:
        """nvidia-smi で NVIDIA GPU を列挙する。利用不可・失敗時は None を返す。"""

        executable = shutil.which('nvidia-smi')
        if executable is None:
            return None
        try:
            async with KonomiTVBS4KExternalProcessLimiter.acquireSlot():
                returncode, stdout, _ = await cls.runSubprocess(
                    [
                        executable,
                        '--query-gpu=name,pci.bus_id',
                        '--format=csv,noheader,nounits',
                    ],
                    timeout = cls._nvidia_smi_timeout_seconds,
                )
        except (OSError, TimeoutError):
            logging.warning('[KonomiTVBS4KCodecSupport] Failed to query nvidia-smi.')
            return None
        if returncode != 0:
            return None
        gpus: list[_NvidiaGpu] = []
        for line in stdout.decode(errors='ignore').splitlines():
            name, _, pci_bus_id = line.partition(',')
            gpus.append(_NvidiaGpu(name = name.strip(), pci_bus_id = pci_bus_id.strip()))
        return gpus

    @classmethod
    async def getEnvironmentSignature(cls) -> str:
        """FFmpeg バイナリ・driver・GPU トポロジー・probe 改訂値から署名を返す。"""

        values = [f'probe-version:{cls._probe_version}']
        for key in ('FFmpeg8', 'FFmpeg8AMD', 'FFprobe8'):
            executable = Path(LIBRARY_PATH[key])
            try:
                stat = await asyncio.to_thread(executable.stat)
                values.append(f'{key}:{stat.st_size}:{stat.st_mtime_ns}')
            except OSError:
                values.append(f'{key}:missing')
        # AMD proprietary driver の切替は VAAPI の挙動が変わるため署名へ含める
        values.append(f'amd-vaapi-driver:{RecordedPlaybackBackend.getAMDVAAPIDriverDirectory()}')
        for node in RecordedPlaybackBackend.listRenderDevices():
            render_node_name = Path(node.path).name
            device_directory = Path(f'/sys/class/drm/{render_node_name}/device')
            device_identity: list[str] = []
            for field_name in ('vendor', 'device', 'revision'):
                try:
                    device_identity.append(device_directory.joinpath(field_name).read_text().strip())
                except OSError:
                    device_identity.append('unknown')
            try:
                driver_name = device_directory.joinpath('driver').resolve().name
            except OSError:
                driver_name = 'unknown'
            try:
                driver_version = Path(f'/sys/module/{driver_name}/version').read_text().strip()
            except OSError:
                driver_version = 'unknown'
            values.append(
                f'render-node:{render_node_name}:{node.vendor_id}:{":".join(device_identity)}:'
                f'{driver_name}:{driver_version}'
            )
        nvidia_gpus = await cls.queryNvidiaGpus() or []
        for gpu in sorted(nvidia_gpus, key=lambda gpu: cls.__normalizePciBdf(gpu.pci_bus_id) or gpu.pci_bus_id):
            normalized = cls.__normalizePciBdf(gpu.pci_bus_id)
            values.append(f'nvidia:{normalized or "unknown"}:{gpu.name}')
        return hashlib.sha256(':'.join(values).encode()).hexdigest()

    @classmethod
    async def __runJob(cls, signature: str) -> None:
        """診断ジョブ本体を列挙→計画→probe→cache の順で実行する。"""

        state = cls._state
        child_tasks: list[asyncio.Task[None]] = []
        try:
            async with asyncio.timeout(cls._job_timeout_seconds):
                # 1. GPU トポロジーの列挙 (nvidia-smi と PCI BDF を含む)
                devices = await cls.__enumerateDevices()
                state.devices = devices

                # 2. FFmpeg バイナリの encoder / decoder 一覧 (Binary 根拠)
                binary_encoders = await cls.listBinaryCodecNames('encoders')
                binary_decoders = await cls.listBinaryCodecNames('decoders')

                # 3. 実 probe 不要の cell を確定根拠で埋めつつ、probe 計画を作る
                with tempfile.TemporaryDirectory(prefix='konomitv-bs4k-codec-support-') as temporary_directory:
                    plan = cls.__buildProbePlan(
                        devices,
                        binary_encoders,
                        binary_decoders,
                        Path(temporary_directory),
                    )
                    state.total_operations = plan.total
                    if plan.total == 0:
                        # FFmpeg バイナリが全体的に不在などで probe 不要の場合は確定 cell のみで完了
                        state.status = 'Completed'
                        state.progress = 1.0
                        async with cls._lock:
                            cls.__storeCache(signature, state)
                        return

                    def on_complete() -> None:
                        state.completed_operations += 1
                        state.progress = min(1.0, state.completed_operations / state.total_operations)

                    clips: dict[str, Path | None] = {}
                    # クリップ生成→probe の順で走らせ、入力生成失敗を decode へだけ波及させる
                    for operation_group in (
                        [operation for operation in plan.operations if operation.kind in ('video_clip', 'audio_clip')],
                        [operation for operation in plan.operations if operation.kind not in ('video_clip', 'audio_clip')],
                    ):
                        tasks = [
                            asyncio.create_task(
                                cls._runProbeOperation(operation, clips, on_complete),
                                name = f'KonomiTVBS4KCodecSupport-{operation.name}',
                            )
                            for operation in operation_group
                        ]
                        child_tasks.extend(tasks)
                        await asyncio.gather(*tasks, return_exceptions = True)

                state.status = 'Completed'
                state.progress = 1.0
                async with cls._lock:
                    cls.__storeCache(signature, state)
        except asyncio.CancelledError:
            # DELETE キャンセル: 子プロセスを finally で回収してから状態を Idle に戻す。
            # 同一署名の成功キャッシュがあれば GET から復元できるよう署名だけ保持する。
            state.status = 'Idle'
            state.devices = []
            state.progress = 0.0
            state.environment_signature = signature if signature in cls._results_cache else None
            state.total_operations = 0
            state.completed_operations = 0
            raise
        except Exception as exception:
            # タイムアウト (TimeoutError) を含む想定外は部分結果を保持した Failed として残す
            logging.error('[KonomiTVBS4KCodecSupport] Diagnostic job failed.', exc_info=exception)
            state.status = 'Failed'
        finally:
            pending = [task for task in child_tasks if task.done() is False]
            if pending:
                # 親のキャンセルだけでは子 Task は止まらないため、ここで明示的に cancel して回収する
                for task in pending:
                    task.cancel()
                _done, still_pending = await asyncio.wait(
                    pending,
                    timeout = cls._cancel_reclaim_timeout_seconds,
                )
                if still_pending:
                    logging.error(
                        '[KonomiTVBS4KCodecSupport] Failed to reclaim '
                        f'{len(still_pending)} probe task(s).',
                    )
            async with cls._lock:
                if cls._task is asyncio.current_task():
                    cls._task = None

    @classmethod
    def __storeCache(cls, signature: str, state: _CodecSupportJobState) -> None:
        """完了した結果を環境署名付きでキャッシュし、古い順に上限まで淘汰する。"""

        cls._results_cache[signature] = state
        while len(cls._results_cache) > cls._max_cached_signatures:
            cls._results_cache.pop(next(iter(cls._results_cache)), None)

    @classmethod
    async def __enumerateDevices(cls) -> list[_CodecSupportDevice]:
        """CPU とコンテナから利用可能な GPU を列挙する。

        Returns:
            device 一覧。AMD は render node ごとに encode / decode を検査する。
        """

        devices: list[_CodecSupportDevice] = [
            _CodecSupportDevice(
                id = 'cpu',
                label = 'CPU (FFmpeg software)',
                kind = 'CPU',
                vendor = 'None',
                backend = 'FFmpeg',
                decode_accel = 'software',
                device_path = None,
                cuda_ordinal = None,
                capabilities = cls.__buildCapabilityRows(include_audio = True),
            ),
        ]
        nvidia_gpus = await cls.queryNvidiaGpus()
        nvidia_by_bdf: dict[str, tuple[_NvidiaGpu, int]] = {}
        if nvidia_gpus is not None:
            # CUDA_DEVICE_ORDER=PCI_BUS_ID を前提に、canonical BDF の昇順から CUDA ordinal を割り当てる。
            # nvidia-smi の出力順は NVML index に依存するため、そのまま ordinal とみなさない。
            normalized_gpus = [
                (normalized, gpu)
                for gpu in nvidia_gpus
                if (normalized := cls.__normalizePciBdf(gpu.pci_bus_id)) is not None
            ]
            for index, (normalized, gpu) in enumerate(sorted(normalized_gpus, key=lambda item: item[0])):
                nvidia_by_bdf[normalized] = (gpu, index)

        seen_nvidia_bdfs: set[str] = set()
        gpu_index = 0
        render_devices = RecordedPlaybackBackend.listRenderDevices()
        vendor_gpu_totals = {
            vendor_name: sum(node.vendor_name == vendor_name for node in render_devices)
            for vendor_name in ('Intel', 'AMD')
        }
        vendor_gpu_indices = {'Intel': 0, 'AMD': 0}
        for node in render_devices:
            pci_bdf = cls.__readPciAddress(node.path)
            normalized_bdf = cls.__normalizePciBdf(pci_bdf) if pci_bdf is not None else None
            if node.vendor_name == 'NVIDIA':
                # DRM の render node と nvidia-smi の結果を PCI BDF で統合し重複表示を避ける
                ## nvidia-smi の name は 'NVIDIA' 接頭辞付きの製品名なのでそのまま表示する
                matched = nvidia_by_bdf.get(normalized_bdf) if normalized_bdf is not None else None
                if matched is None and nvidia_by_bdf:
                    # sysfs から BDF を読めない DRM node は /dev/dri だけが見えるコンテナで出現し、
                    # 下の nvidia-smi 側の列挙と同じ物理 GPU を二重表示する。smi 側が表示可能な
                    # GPU を持つなら出さない (smi の BDF が全て不正なら情報を失うため保持する)
                    logging.debug(
                        f'[KonomiTVBS4KCodecSupport] Skipping NVIDIA render node without PCI BDF '
                        f'because nvidia-smi already lists the GPU. [node: {node.path}]'
                    )
                    continue
                label = matched[0].name if matched is not None and matched[0].name else 'NVIDIA GPU'
                devices.append(_CodecSupportDevice(
                    id = f'gpu-{gpu_index}',
                    label = label,
                    kind = 'GPU',
                    vendor = 'NVIDIA',
                    backend = 'NVENC',
                    decode_accel = 'cuda',
                    device_path = node.path,
                    cuda_ordinal = matched[1] if matched is not None else None,
                    capabilities = cls.__buildCapabilityRows(include_audio = False),
                ))
                if normalized_bdf is not None:
                    seen_nvidia_bdfs.add(normalized_bdf)
                gpu_index += 1
            elif node.vendor_name in ('Intel', 'AMD'):
                # Intel は QSV、AMD は公開名 AMF (実体 Mesa VAAPI) の device として扱う
                vendor_gpu_indices[node.vendor_name] += 1
                label = f'{node.vendor_name} GPU'
                if vendor_gpu_totals[node.vendor_name] > 1:
                    # render node は応答へ出さず、同一 vendor の表示だけ連番で識別可能にする。
                    label += f' {vendor_gpu_indices[node.vendor_name]}'
                devices.append(_CodecSupportDevice(
                    id = f'gpu-{gpu_index}',
                    label = label,
                    kind = 'GPU',
                    vendor = node.vendor_name,
                    backend = 'QSV' if node.vendor_name == 'Intel' else 'AMF',
                    decode_accel = 'qsv' if node.vendor_name == 'Intel' else 'vaapi',
                    device_path = node.path,
                    cuda_ordinal = None,
                    capabilities = cls.__buildCapabilityRows(include_audio = False),
                ))
                gpu_index += 1
            else:
                # 未知 vendor の render node は本診断の対象外なので表示しない
                logging.debug(
                    f'[KonomiTVBS4KCodecSupport] Skipping render node with unknown vendor. '
                    f'[node: {node.path}, vendor_id: {node.vendor_id}]'
                )

        # nvidia-smi にのみ見える GPU (render node 未割り出しの CUDA) も列挙する
        for normalized_bdf, (gpu, index) in nvidia_by_bdf.items():
            if normalized_bdf in seen_nvidia_bdfs:
                continue
            label = gpu.name if gpu.name else 'NVIDIA GPU'
            devices.append(_CodecSupportDevice(
                id = f'gpu-{gpu_index}',
                label = label,
                kind = 'GPU',
                vendor = 'NVIDIA',
                backend = 'NVENC',
                decode_accel = 'cuda',
                device_path = None,
                cuda_ordinal = index,
                capabilities = cls.__buildCapabilityRows(include_audio = False),
            ))
            gpu_index += 1

        # AMD も QSV と同じく render node を固定して encode を検査する。
        # 集約 device は実再生経路と一致しないため作らない。
        return devices

    @classmethod
    def __buildCapabilityRows(cls, *, include_audio: bool) -> list[_CodecSupportCapability]:
        """未検査の cell を保持する capability 行をカタログ順に作る。"""

        rows: list[_CodecSupportCapability] = []
        for codec, profile, bit_depth, _ffprobe_name, used in _VIDEO_CAPABILITIES:
            rows.append(_CodecSupportCapability(
                media_type = 'Video',
                codec = codec,
                profile = profile,
                bit_depth = bit_depth,
                decode = _CodecSupportOperation('Unknown', 'Unavailable', None, None, None),
                encode = _CodecSupportOperation('Unknown', 'Unavailable', None, None, None),
                used_by_konomitv_bs4k = used,
            ))
        if include_audio is True:
            for codec, profile, _encoder, _decoder, used in _AUDIO_CAPABILITIES:
                rows.append(_CodecSupportCapability(
                    media_type = 'Audio',
                    codec = codec,
                    profile = profile,
                    bit_depth = None,
                    decode = _CodecSupportOperation('Unknown', 'Unavailable', None, None, None),
                    encode = _CodecSupportOperation('Unknown', 'Unavailable', None, None, None),
                    used_by_konomitv_bs4k = used,
                ))
        return rows

    @staticmethod
    def __readPciAddress(render_node_path: str) -> str | None:
        """render node の sysfs device symlink から PCI BDF (device 名) を返す。"""

        try:
            device_path = Path(f'/sys/class/drm/{Path(render_node_path).name}/device')
            return device_path.resolve().name
        except OSError:
            return None

    @staticmethod
    def __normalizePciBdf(pci_bus_id: str) -> str | None:
        """nvidia-smi の pci.bus_id と sysfs の device 名を canonical PCI BDF へ正規化する。

        Args:
            pci_bus_id: 4 桁または nvidia-smi 形式の 8 桁 domain を持つ PCI BDF。

        Returns:
            domain を 4 桁へ揃えた小文字の PCI BDF。不正な値または 16 bit を超える domain は None。
        """

        match = re.fullmatch(
            r'([0-9A-Fa-f]{4}(?:[0-9A-Fa-f]{4})?):([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2})\.([0-7])',
            pci_bus_id.strip(),
        )
        if match is None:
            return None
        domain = int(match.group(1), 16)
        if domain > 0xFFFF:
            return None
        return f'{domain:04x}:{match.group(2).lower()}:{match.group(3).lower()}.{match.group(4)}'

    @classmethod
    def __buildProbePlan(
        cls,
        devices: list[_CodecSupportDevice],
        binary_encoders: set[str] | None,
        binary_decoders: set[str] | None,
        temp_root: Path,
    ) -> _CodecSupportProbePlan:
        """実 probe 不要の cell を確定根拠で埋め、probe が必要な操作を計画する。"""

        plan = _CodecSupportProbePlan()

        # decode 入力用のクリップは device 横断で一度だけ生成し共有する。
        # CPU の比較用 codec も Binary 推定で終わらせず、入力を生成できる限り実 decode する。
        clip_paths: dict[str, Path] = {}
        if binary_encoders is not None:
            for codec, _profile, bit_depth, _ffprobe_name, _used in _VIDEO_CAPABILITIES:
                encoder_name = _VIDEO_SOFTWARE_ENCODERS[codec]
                if encoder_name is None or encoder_name not in binary_encoders:
                    continue
                key = f'video:{codec}:{bit_depth}'
                # Matroska は MPEG-1/2 を含む全対象映像を同じ decode fixture 形式へ統一できる。
                clip_path = temp_root / f'video-{codec}-{bit_depth}.mkv'
                clip_paths[key] = clip_path
                plan.operations.append(cls.__makeOperation(
                    devices[0],
                    name = f'clip-video-{codec}-{bit_depth}',
                    kind = 'video_clip',
                    codec = codec,
                    bit_depth = bit_depth,
                    clip_key = key,
                    output_path = clip_path,
                ))
            for codec, _profile, encoder, _decoder, _used in _AUDIO_CAPABILITIES:
                if encoder is None or encoder not in binary_encoders:
                    continue
                key = f'audio:{codec}'
                clip_path = temp_root / f'audio-{codec}{cls.__audioProbeSuffix(codec)}'
                clip_paths[key] = clip_path
                plan.operations.append(cls.__makeOperation(
                    devices[0],
                    name = f'clip-audio-{codec}',
                    kind = 'audio_clip',
                    codec = codec,
                    bit_depth = None,
                    clip_key = key,
                    output_path = clip_path,
                ))

        for device in devices:
            for capability in device.capabilities:
                if capability.media_type == 'Video':
                    cls.__planVideoCapability(
                        device,
                        capability,
                        binary_encoders,
                        binary_decoders,
                        temp_root,
                        plan,
                    )
                else:
                    cls.__planAudioCapability(
                        device,
                        capability,
                        binary_encoders,
                        binary_decoders,
                        temp_root,
                        plan,
                    )
        return plan

    @classmethod
    def __planVideoCapability(
        cls,
        device: _CodecSupportDevice,
        capability: _CodecSupportCapability,
        binary_encoders: set[str] | None,
        binary_decoders: set[str] | None,
        temp_root: Path,
        plan: _CodecSupportProbePlan,
    ) -> None:
        """映像 capability の decode / encode cell を確定するか probe を計画する。"""

        codec = capability.codec
        bit_depth = capability.bit_depth
        used = capability.used_by_konomitv_bs4k

        # --- decode ---
        if binary_decoders is None:
            cls.__fillUnavailable(capability.decode, device, 'Unknown', 'BinaryUnavailable')
        elif device.kind == 'CPU':
            if _VIDEO_SOFTWARE_DECODERS[codec] not in binary_decoders:
                cls.__fillUnavailable(capability.decode, device, 'Unsupported', 'DecoderUnavailable')
            else:
                encoder_name = _VIDEO_SOFTWARE_ENCODERS[codec]
                if (
                    binary_encoders is not None and
                    encoder_name is not None and
                    encoder_name in binary_encoders
                ):
                    plan.operations.append(cls.__makeOperation(
                        device,
                        name = f'decode-cpu-{codec}-{bit_depth}',
                        kind = 'video_decode',
                        capability = capability,
                        side = 'decode',
                        codec = codec,
                        bit_depth = bit_depth,
                        clip_key = f'video:{codec}:{bit_depth}',
                        tested_configuration = f'{codec} {bit_depth}bit {_VIDEO_PROBE_CONFIGURATION} decode',
                    ))
                else:
                    # decoder は存在するが正しい入力を生成できないため、機能確認までは断定しない。
                    cls.__fillUnverified(
                        capability.decode,
                        device,
                        'Binary',
                        'ProbeInputUnavailable',
                    )
        elif used is True:
            if device.decode_accel == 'cuda' and device.cuda_ordinal is None:
                # nvidia-smi と PCI BDF が結び付かない GPU を CUDA 0 へ落とすと別世代の結果になる
                cls.__fillUnavailable(capability.decode, device, 'Unknown', 'DeviceUnavailable')
            else:
                # 本線 4 コーデックは全 GPU の hwaccel decode が名目対応するため実 probe する
                plan.operations.append(cls.__makeOperation(
                    device,
                    name = f'decode-{device.id}-{codec}-{bit_depth}',
                    kind = 'video_decode',
                    capability = capability,
                    side = 'decode',
                    codec = codec,
                    bit_depth = bit_depth,
                    clip_key = f'video:{codec}:{bit_depth}',
                    tested_configuration = (
                        f'{codec} {bit_depth}bit {_VIDEO_PROBE_CONFIGURATION} hwaccel decode'
                    ),
                ))
        elif codec in _HWACCEL_VIDEO_DECODER_NAMES.get(device.decode_accel, {}):
            # 本線外でも専用 hwaccel decoder を持つ GPU codec (現状は CUDA の mpeg1/2/4) は、
            # ソフトウェア encoder で入力クリップを生成できる限り Driver 推定で終わらせず実 probe する
            if device.decode_accel == 'cuda' and device.cuda_ordinal is None:
                # nvidia-smi と PCI BDF が結び付かない GPU を CUDA 0 へ落とすと別世代の結果になる
                cls.__fillUnavailable(capability.decode, device, 'Unknown', 'DeviceUnavailable')
            else:
                encoder_name = _VIDEO_SOFTWARE_ENCODERS[codec]
                if (
                    binary_encoders is not None and
                    encoder_name is not None and
                    encoder_name in binary_encoders
                ):
                    plan.operations.append(cls.__makeOperation(
                        device,
                        name = f'decode-{device.id}-{codec}-{bit_depth}',
                        kind = 'video_decode',
                        capability = capability,
                        side = 'decode',
                        codec = codec,
                        bit_depth = bit_depth,
                        clip_key = f'video:{codec}:{bit_depth}',
                        tested_configuration = (
                            f'{codec} {bit_depth}bit {_VIDEO_PROBE_CONFIGURATION} hwaccel decode'
                        ),
                    ))
                else:
                    # decoder は存在するが正しい入力を生成できないため、名目対応の推定に留める
                    cls.__fillLikely(capability.decode, device, 'Driver')
        elif codec in _HWACCEL_VIDEO_DECODERS.get(device.decode_accel, frozenset()):
            # 専用 decoder マッピングを持たない本線外 GPU codec は実 probe できないため Driver 根拠に留める
            cls.__fillLikely(capability.decode, device, 'Driver')
        else:
            cls.__fillUnavailable(capability.decode, device, 'Unknown', 'DecoderUnavailable')

        # --- encode ---
        if binary_encoders is None:
            cls.__fillUnavailable(capability.encode, device, 'Unknown', 'BinaryUnavailable')
        elif device.kind == 'CPU':
            encoder_name = _VIDEO_SOFTWARE_ENCODERS[codec]
            if encoder_name is not None and encoder_name in binary_encoders:
                plan.operations.append(cls.__makeEncodeOperation(
                    device,
                    capability,
                    codec,
                    bit_depth,
                    f'encode-cpu-{codec}-{bit_depth}',
                    temp_root,
                ))
            else:
                cls.__fillUnavailable(capability.encode, device, 'Unsupported', 'EncoderUnavailable')
        elif used is True:
            if device.backend == 'NVENC' and device.cuda_ordinal is None:
                # 対応付けできない NVIDIA GPU を CUDA 0 で検査すると別 GPU の結果になる
                cls.__fillUnavailable(capability.encode, device, 'Unknown', 'DeviceUnavailable')
            else:
                # GPU encode の組み合わせは KonomiTV 本線 4 コーデックしか持たないため、
                # バックエンドの型 (RecordedPlaybackVideoCodec / BitDepth) へ絞り込む
                assert codec in ('avc', 'hevc', 'vp9', 'av1')
                assert bit_depth in (8, 10)
                if RecordedPlaybackBackend.isCombinationSupported(device.backend, codec, bit_depth) is True:
                    plan.operations.append(cls.__makeEncodeOperation(
                        device,
                        capability,
                        codec,
                        bit_depth,
                        f'encode-{device.id}-{codec}-{bit_depth}',
                        temp_root,
                    ))
                else:
                    cls.__fillUnavailable(capability.encode, device, 'Unsupported', 'UnsupportedCombination')
        else:
            # GPU backend は本線 4 コーデックしか持たない設計のため他は非対応
            cls.__fillUnavailable(capability.encode, device, 'Unsupported', 'UnsupportedCombination')

    @classmethod
    def __makeEncodeOperation(
        cls,
        device: _CodecSupportDevice,
        capability: _CodecSupportCapability,
        codec: str,
        bit_depth: int | None,
        name: str,
        temp_root: Path,
    ) -> _CodecSupportProbeOperation:
        """encode 用 probe 操作を作成し、temp_root 配下に出力先を割り当てる。"""

        output_suffix = '.mp4' if codec in ('avc', 'hevc', 'vp9', 'av1') else '.mkv'
        return _CodecSupportProbeOperation(
            name = name,
            kind = 'video_encode',
            device = device,
            capability = capability,
            side = 'encode',
            codec = codec,
            bit_depth = bit_depth,
            clip_key = None,
            output_path = temp_root / f'{name}{output_suffix}',
            tested_configuration = f'{codec} {bit_depth}bit {_VIDEO_PROBE_CONFIGURATION} encode',
        )

    @classmethod
    def __planAudioCapability(
        cls,
        device: _CodecSupportDevice,
        capability: _CodecSupportCapability,
        binary_encoders: set[str] | None,
        binary_decoders: set[str] | None,
        temp_root: Path,
        plan: _CodecSupportProbePlan,
    ) -> None:
        """音声 capability (CPU のみ) の decode / encode cell を確定するか probe を計画する。"""

        codec = capability.codec
        encoder_token, decoder_token = _AUDIO_CODEC_TOKENS[codec]

        # (side, バイナリ一覧の token, token 不在時の理由コード)
        sides: tuple[
            tuple[Literal['decode', 'encode'], str | None, KonomiTVBS4KCodecSupportReasonCode],
            tuple[Literal['decode', 'encode'], str | None, KonomiTVBS4KCodecSupportReasonCode],
        ] = (
            ('decode', decoder_token, 'DecoderUnavailable'),
            ('encode', encoder_token, 'EncoderUnavailable'),
        )
        for side, token, unavailable_reason in sides:
            cell = capability.decode if side == 'decode' else capability.encode
            binary_set = binary_encoders if side == 'encode' else binary_decoders
            if binary_set is None:
                cls.__fillUnavailable(cell, device, 'Unknown', 'BinaryUnavailable')
            elif token is None or token not in binary_set:
                cls.__fillUnavailable(cell, device, 'Unsupported', unavailable_reason)
            elif side == 'decode' and (
                binary_encoders is None or
                encoder_token is None or
                encoder_token not in binary_encoders
            ):
                # decoder は存在しても正しい入力を生成できなければ実行確認できない。
                cls.__fillUnverified(cell, device, 'Binary', 'ProbeInputUnavailable')
            else:
                plan.operations.append(cls.__makeOperation(
                    device,
                    name = f'{side}-cpu-audio-{codec}',
                    kind = f'audio_{side}',
                    capability = capability,
                    side = side,
                    codec = codec,
                    bit_depth = None,
                    clip_key = None if side == 'encode' else f'audio:{codec}',
                    # encode は出力ファイルを作る。decode は clip_key の入力だけ使う。
                    output_path = (
                        temp_root / f'encode-cpu-audio-{codec}{cls.__audioProbeSuffix(codec)}'
                        if side == 'encode' else None
                    ),
                    tested_configuration = f'{codec} 440Hz 48kHz 0.6s {side}',
                ))

    @staticmethod
    def __makeOperation(
        device: _CodecSupportDevice,
        *,
        name: str,
        kind: str,
        codec: str,
        bit_depth: int | None,
        clip_key: str | None,
        tested_configuration: str | None = None,
        capability: _CodecSupportCapability | None = None,
        side: str | None = None,
        output_path: Path | None = None,
    ) -> _CodecSupportProbeOperation:
        """probe 操作の共通コンストラクタ。クリップ生成操作は cell を持たない。"""

        return _CodecSupportProbeOperation(
            name = name,
            kind = kind,
            device = device,
            capability = capability,
            side = side,
            codec = codec,
            bit_depth = bit_depth,
            clip_key = clip_key,
            output_path = output_path,
            tested_configuration = tested_configuration,
        )

    @staticmethod
    def __fillLikely(
        cell: _CodecSupportOperation,
        device: _CodecSupportDevice,
        evidence: Literal['Driver', 'Binary'],
    ) -> None:
        """Binary / Driver 根拠で推定対応として確定する。"""

        cell.status = 'Likely'
        cell.evidence = evidence
        cell.backend = device.backend
        cell.reason_code = None
        cell.tested_configuration = None

    @staticmethod
    def __fillUnverified(
        cell: _CodecSupportOperation,
        device: _CodecSupportDevice,
        evidence: Literal['Driver', 'Binary'],
        reason_code: KonomiTVBS4KCodecSupportReasonCode,
    ) -> None:
        """能力の候補は存在するが実 probe できない cell を Unknown として残す。"""

        cell.status = 'Unknown'
        cell.evidence = evidence
        cell.backend = device.backend
        cell.reason_code = reason_code
        cell.tested_configuration = None

    @staticmethod
    def __fillUnavailable(
        cell: _CodecSupportOperation,
        device: _CodecSupportDevice,
        status: KonomiTVBS4KCodecSupportOperationStatus,
        reason_code: KonomiTVBS4KCodecSupportReasonCode,
    ) -> None:
        """API / binary / driver / device がない cell を確定する。"""

        cell.status = status
        cell.evidence = 'Unavailable'
        cell.backend = device.backend
        cell.reason_code = reason_code
        cell.tested_configuration = None

    @staticmethod
    def __audioProbeSuffix(codec: str) -> str:
        """音声 probe 出力に使う container の拡張子を返す。"""

        # LATM decoder を検査する行だけ LOAS/LATM を使い、他は全対象 codec を保持できる Matroska へ統一する。
        return '.latm' if codec == 'aac_latm' else '.mka'

    @staticmethod
    def __audioProbeSpec(codec: str) -> tuple[list[str], str, str | None, str]:
        """音声 probe の encoder 引数・期待 codec/profile・muxer を返す。"""

        return {
            'aac_lc': (['-c:a', 'aac', '-profile:a', 'aac_low', '-b:a', '96k'], 'aac', 'LC', 'matroska'),
            'he_aac': (['-c:a', 'aac', '-profile:a', 'aac_he', '-b:a', '64k'], 'aac', 'HE-AAC', 'matroska'),
            'aac_latm': (['-c:a', 'aac', '-profile:a', 'aac_low', '-b:a', '96k'], 'aac_latm', 'LC', 'latm'),
            'mp2': (['-c:a', 'mp2', '-b:a', '192k'], 'mp2', None, 'matroska'),
            'mp3': (['-c:a', 'libmp3lame', '-b:a', '192k'], 'mp3', None, 'matroska'),
            'ac3': (['-c:a', 'ac3', '-b:a', '448k'], 'ac3', None, 'matroska'),
            'eac3': (['-c:a', 'eac3', '-b:a', '640k'], 'eac3', None, 'matroska'),
            'opus': (['-c:a', 'libopus', '-b:a', '96k'], 'opus', None, 'matroska'),
            'vorbis': (['-c:a', 'libvorbis', '-b:a', '128k'], 'vorbis', None, 'matroska'),
            'flac': (['-c:a', 'flac'], 'flac', None, 'matroska'),
            'lpcm': (['-c:a', 'pcm_s16le'], 'pcm_s16le', None, 'matroska'),
        }[codec]

    @staticmethod
    def __buildSoftwareVideoProbeCommand(codec: str, bit_depth: int, output_path: Path) -> list[str]:
        """CPU 映像の fixture 生成と encode probe で共有する FFmpeg コマンドを構築する。"""

        encoder_name = _VIDEO_SOFTWARE_ENCODERS[codec]
        assert encoder_name is not None
        quality = QUALITY[_VIDEO_PROBE_QUALITY]
        frame_rate = '60000/1001' if quality.is_60fps is True else '30000/1001'
        profile_arguments: list[str] = []
        tuning_arguments: list[str] = []
        if codec in ('avc', 'hevc', 'vp9', 'av1'):
            assert bit_depth in (8, 10)
            spec = RecordedPlaybackBackend.getCodecSpec(codec, bit_depth)
            pixel_format = spec.pixel_format
            profile = {
                'avc': 'high',
                'hevc': 'main10' if bit_depth == 10 else 'main',
                'vp9': '2' if bit_depth == 10 else '0',
                'av1': '0',
            }[codec]
            profile_arguments = ['-profile:v', profile]
            # libaom-av1 / libvpx-vp9 などの既定値では fixture だけで timeout するため、
            # 実再生・encode probe と同じ realtime tuning を必ず適用する。
            tuning_arguments = RecordedPlaybackBackend.getTuningArguments('FFmpeg', codec)
        else:
            pixel_format = 'yuv420p'
        container = 'matroska' if output_path.suffix == '.mkv' else 'mp4'
        return [
            str(Path(LIBRARY_PATH['FFmpeg8'])),
            '-hide_banner',
            '-loglevel',
            'error',
            '-f',
            'lavfi',
            '-i',
            f'testsrc2=size=320x192:rate={frame_rate}:duration=0.2',
            '-vf',
            f'scale={quality.width}:{quality.height},format={pixel_format}',
            '-an',
            '-c:v',
            encoder_name,
            *profile_arguments,
            *tuning_arguments,
            '-pix_fmt',
            pixel_format,
            '-r',
            frame_rate,
            '-frames:v',
            '6',
            '-f',
            container,
            '-y',
            str(output_path),
        ]

    @classmethod
    async def __runVideoClip(
        cls,
        codec: str,
        bit_depth: int | None,
        output_path: Path | None,
    ) -> bool:
        """CPU ソフトウェア encoder で decode 入力の短いクリップを生成する。"""

        assert bit_depth is not None and bit_depth in (8, 10)
        assert output_path is not None
        command = cls.__buildSoftwareVideoProbeCommand(codec, bit_depth, output_path)
        try:
            returncode, _, stderr = await cls.runSubprocess(command)
        except (OSError, TimeoutError):
            return False
        if returncode != 0:
            logging.warning(
                f'[KonomiTVBS4KCodecSupport] Clip generation failed. '
                f'[codec: {codec}, bit_depth: {bit_depth}, '
                f'stderr: {stderr.decode(errors="ignore").strip()}]'
            )
            return False
        return output_path.is_file()

    @classmethod
    async def __runAudioClip(cls, codec: str, output_path: Path | None) -> bool:
        """CPU encoder で音声 decode 入力の短いクリップを生成する。"""

        assert output_path is not None
        executable = Path(LIBRARY_PATH['FFmpeg8'])
        encoder_args, _expected_codec, _expected_profile, muxer = cls.__audioProbeSpec(codec)
        command = [
            str(executable),
            '-hide_banner',
            '-loglevel',
            'error',
            '-f',
            'lavfi',
            '-i',
            'sine=frequency=440:sample_rate=48000:duration=0.6',
            *encoder_args,
            '-f',
            muxer,
            '-y',
            str(output_path),
        ]
        try:
            returncode, _, stderr = await cls.runSubprocess(command)
        except (OSError, TimeoutError):
            return False
        if returncode != 0:
            logging.warning(
                f'[KonomiTVBS4KCodecSupport] Audio clip generation failed. '
                f'[codec: {codec}, stderr: {stderr.decode(errors="ignore").strip()}]'
            )
            return False
        return output_path.is_file()

    @classmethod
    async def __runVideoEncodeProbe(
        cls,
        device: _CodecSupportDevice,
        codec: str,
        bit_depth: int | None,
        output_path: Path | None,
    ) -> tuple[bool, KonomiTVBS4KCodecSupportReasonCode | None]:
        """device 固有の encoder で短い映像を生成し FFprobe 8 で検証する。"""

        assert bit_depth is not None and bit_depth in (8, 10)
        assert output_path is not None
        is_main_codec = codec in ('avc', 'hevc', 'vp9', 'av1')
        if device.kind == 'GPU':
            # GPU encode は RecordedPlaybackBackend が所有する KonomiTV 本線だけを実 probe する。
            assert is_main_codec is True
        spec: RecordedPlaybackCodecSpec | None = None
        if is_main_codec is True:
            # assert で codec / bit depth をバックエンドの Literal 型へ絞り込む
            assert codec in ('avc', 'hevc', 'vp9', 'av1')
            assert bit_depth in (8, 10)
            spec = RecordedPlaybackBackend.getCodecSpec(codec, bit_depth)
        expected_codec_name = (
            spec.ffprobe_codec_name
            if spec is not None else
            next(ffprobe_name for name, _profile, _depth, ffprobe_name, _used in _VIDEO_CAPABILITIES if name == codec)
        )
        executable = Path(RecordedPlaybackBackend.getExecutable(device.backend))
        ffprobe = Path(LIBRARY_PATH['FFprobe8'])
        if executable.is_file() is False or ffprobe.is_file() is False:
            return False, 'BinaryUnavailable'
        if device.backend == 'NVENC' and device.cuda_ordinal is None:
            return False, 'DeviceUnavailable'
        if device.backend in ('QSV', 'AMF') and device.device_path is None:
            return False, 'DeviceUnavailable'

        if device.kind == 'CPU' and is_main_codec is False:
            command = cls.__buildSoftwareVideoProbeCommand(codec, bit_depth, output_path)
        else:
            assert codec in ('avc', 'hevc', 'vp9', 'av1')
            command = RecordedPlaybackBackend.buildProbeCommand(
                device.backend,
                codec,
                bit_depth,
                output_path,
                device.device_path,
                quality = _VIDEO_PROBE_QUALITY,
                # 複数 GPU 環境では対象 device の ordinal を直接渡す
                cuda_ordinal = device.cuda_ordinal,
            )

        try:
            returncode, _, stderr = await cls.runSubprocess(
                command,
                env = cls.__probeEnvironment(device),
            )
        except TimeoutError:
            # TimeoutError は OSError の子类のため、必ず先に見る
            return False, 'ProbeTimeout'
        except OSError:
            return False, 'BinaryUnavailable'
        if returncode != 0:
            reason = RecordedPlaybackCapabilityProbe.classifyFailure(
                stderr.decode(errors = 'ignore'),
            )
            # classifyFailure() が返し得るのは上記4コードのみで、本診断ではそれ以外は出ない
            assert reason in ('DeviceUnavailable', 'DeviceInitializationFailed', 'FilterUnavailable', 'EncodeFailed', 'UnsupportedByDevice')
            return False, reason

        # 出力を実際の FFprobe 8 で検証し、encoder 名の詐称を捉える
        try:
            probe_returncode, stdout, _ = await cls.runSubprocess(
                [
                    str(ffprobe),
                    '-v',
                    'error',
                    '-count_frames',
                    '-show_streams',
                    '-of',
                    'json',
                    str(output_path),
                ],
                env = cls.__probeEnvironment(device),
            )
        except (OSError, TimeoutError):
            return False, 'ProbeFailed'
        if probe_returncode != 0:
            return False, 'ProbeFailed'
        try:
            stream = json.loads(stdout.decode(errors = 'ignore'))['streams'][0]
        except (ValueError, KeyError, IndexError, TypeError):
            return False, 'ProbeFailed'
        if stream.get('codec_name') != expected_codec_name:
            return False, 'CodecMismatch'
        if int(stream.get('nb_read_frames', 0)) < 2:
            return False, 'ProbeFailed'
        quality = QUALITY[_VIDEO_PROBE_QUALITY]
        if (
            int(stream.get('width', 0)) != quality.width or
            int(stream.get('height', 0)) != quality.height
        ):
            return False, 'ProbeFailed'
        actual_pixel_format = str(stream.get('pix_fmt', ''))
        is_10bit = actual_pixel_format in ('yuv420p10le', 'p010le', 'p010')
        if is_10bit != (bit_depth == 10):
            return False, 'BitDepthMismatch'
        if (
            spec is not None and
            str(stream.get('profile', '')).lower().replace(' ', '') != spec.profile.lower().replace(' ', '')
        ):
            return False, 'ProfileMismatch'
        return True, None

    @classmethod
    async def __runVideoDecodeProbe(
        cls,
        device: _CodecSupportDevice,
        codec: str,
        bit_depth: int | None,
        clip_path: Path,
    ) -> tuple[bool, KonomiTVBS4KCodecSupportReasonCode | None]:
        """device の hwaccel (CPU はソフトウェア) でクリップをデコードして検証する。"""

        executable = Path(RecordedPlaybackBackend.getExecutable(device.backend))
        if executable.is_file() is False:
            return False, 'BinaryUnavailable'
        command = [str(executable), '-hide_banner', '-loglevel', 'error']
        if device.decode_accel == 'qsv':
            if device.device_path is None:
                return False, 'DeviceUnavailable'
            command += [
                '-hwaccel',
                'qsv',
                '-hwaccel_device',
                device.device_path,
                '-hwaccel_output_format',
                'qsv',
                '-c:v',
                _HWACCEL_VIDEO_DECODER_NAMES['qsv'][codec],
            ]
        elif device.decode_accel == 'vaapi':
            if device.device_path is None:
                return False, 'DeviceUnavailable'
            # VAAPI は codec 別 decoder を持たないため、hardware frame format の固定で
            # software frame へのフォールバックを失敗として検出する。
            command += [
                '-hwaccel',
                'vaapi',
                '-hwaccel_device',
                device.device_path,
                '-hwaccel_output_format',
                'vaapi',
            ]
        elif device.decode_accel == 'cuda':
            if device.cuda_ordinal is None:
                return False, 'DeviceUnavailable'
            command += [
                '-hwaccel',
                'cuda',
                '-hwaccel_device',
                str(device.cuda_ordinal),
                '-hwaccel_output_format',
                'cuda',
                '-c:v',
                _HWACCEL_VIDEO_DECODER_NAMES['cuda'][codec],
            ]
        # CPU (decode_accel == 'software') は自分で生成したクリップをデコードするだけなので
        # decoder は自動選択に任せる。同梱 FFmpeg 8 のネイティブ av1 decoder はこの環境で
        # 動かず、-c:v av1 を明示すると実際に利用可能な libaom-av1 decoder を選べなくなるため。
        command += ['-i', str(clip_path), '-f', 'null', '-']
        try:
            returncode, _, stderr = await cls.runSubprocess(
                command,
                env = cls.__probeEnvironment(device),
            )
        except TimeoutError:
            # TimeoutError は OSError の子类のため、必ず先に見る
            return False, 'ProbeTimeout'
        except OSError:
            return False, 'BinaryUnavailable'
        if returncode != 0:
            reason = RecordedPlaybackCapabilityProbe.classifyFailure(stderr.decode(errors = 'ignore'))
            # classifyFailure() が返し得るのは上記4コードのみで、本診断ではそれ以外は出ない
            assert reason in ('DeviceUnavailable', 'DeviceInitializationFailed', 'FilterUnavailable', 'EncodeFailed', 'UnsupportedByDevice')
            # classifyFailure の既定値は encode 向けのため decode では DecodeFailed へ置き換える
            if reason == 'EncodeFailed':
                reason = 'DecodeFailed'
            return False, reason
        return True, None

    @classmethod
    async def __runAudioEncodeProbe(
        cls,
        codec: str,
        output_path: Path | None,
    ) -> tuple[bool, KonomiTVBS4KCodecSupportReasonCode | None]:
        """音声を CPU encoder で生成し FFprobe 8 で codec/profile を検証する。"""

        assert output_path is not None
        executable = Path(LIBRARY_PATH['FFmpeg8'])
        ffprobe = Path(LIBRARY_PATH['FFprobe8'])
        if executable.is_file() is False or ffprobe.is_file() is False:
            return False, 'BinaryUnavailable'
        encoder_args, expected_codec_name, expected_profile, muxer = cls.__audioProbeSpec(codec)
        command = [
            str(executable),
            '-hide_banner',
            '-loglevel',
            'error',
            '-f',
            'lavfi',
            '-i',
            'sine=frequency=440:sample_rate=48000:duration=0.6',
            *encoder_args,
            '-f',
            muxer,
            '-y',
            str(output_path),
        ]
        try:
            returncode, _, _ = await cls.runSubprocess(command)
        except TimeoutError:
            # TimeoutError は OSError の子类のため、必ず先に見る
            return False, 'ProbeTimeout'
        except OSError:
            return False, 'BinaryUnavailable'
        if returncode != 0:
            return False, 'EncodeFailed'
        try:
            probe_returncode, stdout, _ = await cls.runSubprocess(
                [
                    str(ffprobe),
                    '-v',
                    'error',
                    '-show_streams',
                    '-of',
                    'json',
                    str(output_path),
                ],
            )
        except (OSError, TimeoutError):
            return False, 'ProbeFailed'
        if probe_returncode != 0:
            return False, 'ProbeFailed'
        try:
            stream = json.loads(stdout.decode(errors = 'ignore'))['streams'][0]
        except (ValueError, KeyError, IndexError, TypeError):
            return False, 'ProbeFailed'
        if stream.get('codec_name') != expected_codec_name:
            return False, 'CodecMismatch'
        if (
            expected_profile is not None and
            str(stream.get('profile', '')).lower().replace(' ', '') != expected_profile.lower().replace(' ', '')
        ):
            return False, 'ProfileMismatch'
        return True, None

    @classmethod
    async def __runAudioDecodeProbe(
        cls,
        codec: str,
        clip_path: Path,
    ) -> tuple[bool, KonomiTVBS4KCodecSupportReasonCode | None]:
        """音声クリップを対象ソフトウェア decoder でデコードして検証する。"""

        executable = Path(LIBRARY_PATH['FFmpeg8'])
        if executable.is_file() is False:
            return False, 'BinaryUnavailable'
        decoder_name = _AUDIO_CODEC_TOKENS[codec][1]
        if decoder_name is None:
            return False, 'DecoderUnavailable'
        command = [
            str(executable),
            '-hide_banner',
            '-loglevel',
            'error',
            '-c:a',
            decoder_name,
            '-i',
            str(clip_path),
            '-f',
            'null',
            '-',
        ]
        try:
            returncode, _, stderr = await cls.runSubprocess(command)
        except TimeoutError:
            # TimeoutError は OSError の子类のため、必ず先に見る
            return False, 'ProbeTimeout'
        except OSError:
            return False, 'BinaryUnavailable'
        if returncode != 0:
            reason = RecordedPlaybackCapabilityProbe.classifyFailure(stderr.decode(errors = 'ignore'))
            # classifyFailure() が返し得るのは上記4コードのみで、本診断ではそれ以外は出ない
            assert reason in ('DeviceUnavailable', 'DeviceInitializationFailed', 'FilterUnavailable', 'EncodeFailed', 'UnsupportedByDevice')
            if reason == 'EncodeFailed':
                reason = 'DecodeFailed'
            return False, reason
        return True, None

    @staticmethod
    def __probeEnvironment(device: _CodecSupportDevice) -> dict[str, str]:
        """probe subprocess の実行環境を backend 固有の設定付きで返す。"""

        environment = RecordedPlaybackBackend.getEnvironment(device.backend)
        if device.decode_accel == 'cuda':
            # nvidia-smi の表示順 (PCI bus ID) が CUDA ordinal と対応するよう固定する
            environment['CUDA_DEVICE_ORDER'] = 'PCI_BUS_ID'
        return environment

    @staticmethod
    def __buildResponse(state: _CodecSupportJobState) -> KonomiTVBS4KCodecSupportJob:
        """内部状態を応答スキーマへ変換する (render node パス等はここで除去する)。"""

        return KonomiTVBS4KCodecSupportJob(
            status = state.status,
            progress = state.progress,
            environment_signature = state.environment_signature,
            devices = [
                KonomiTVBS4KCodecSupportDevice(
                    id = device.id,
                    label = device.label,
                    kind = device.kind,
                    vendor = device.vendor,
                    capabilities = [
                        KonomiTVBS4KCodecSupportCapability(
                            media_type = capability.media_type,
                            codec = capability.codec,
                            profile = capability.profile,
                            bit_depth = capability.bit_depth,
                            decode = KonomiTVBS4KCodecSupportOperationSupport(
                                status = capability.decode.status,
                                evidence = capability.decode.evidence,
                                backend = capability.decode.backend,
                                reason_code = capability.decode.reason_code,
                                tested_configuration = capability.decode.tested_configuration,
                            ),
                            encode = KonomiTVBS4KCodecSupportOperationSupport(
                                status = capability.encode.status,
                                evidence = capability.encode.evidence,
                                backend = capability.encode.backend,
                                reason_code = capability.encode.reason_code,
                                tested_configuration = capability.encode.tested_configuration,
                            ),
                            used_by_konomitv_bs4k = capability.used_by_konomitv_bs4k,
                        )
                        for capability in device.capabilities
                    ],
                )
                for device in state.devices
            ],
        )
