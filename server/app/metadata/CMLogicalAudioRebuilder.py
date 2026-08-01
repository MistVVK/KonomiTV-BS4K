from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal, NoReturn, cast

import av
from av.error import FFmpegError
from typing_extensions import TypedDict


# Amatsukazeとの差分境界:
# - 取り込み/PCM化は自前TS・AAC parserではなく、隔離したPyAV subprocessで行う。
# - PTS整列はStreamReform::fillAudioFramesInOrderの半フレーム棄却と
#   frameDuration/4複製式に合わせる。
# - 映像末尾までの無音補完と、親側のFFmpeg fallbackだけはchapter_exeへ
#   固定長WAVを渡すKonomiTV-BS4K固有の契約である。
CMLogicalAudioRebuildStage = Literal[
    'Arguments',
    'Input',
    'StreamSelection',
    'Demux',
    'Decode',
    'Assemble',
    'Write',
    'Completed',
]


@dataclass(slots=True)
class CMLogicalAudioRebuildStatistics:
    """論理音声0のPTS再構築結果。"""

    decoded_frames: int = 0
    duplicated_frames: int = 0
    skipped_frames: int = 0
    decode_errors: int = 0
    demux_errors: int = 0
    padded_samples: int = 0
    output_samples: int = 0


class CMLogicalAudioRebuildReportJSON(TypedDict):
    """親プロセスへ返す論理音声再構築レポート。"""

    stage: CMLogicalAudioRebuildStage
    statistics: dict[str, int]
    error: str | None


class _CMLogicalAudioRebuildFailure(Exception):
    """処理段階・統計・CLI終了コードを失わず親へ返す再構築失敗。"""

    def __init__(
        self,
        stage: CMLogicalAudioRebuildStage,
        statistics: CMLogicalAudioRebuildStatistics,
        error: str,
        *,
        exit_code: Literal[1, 2] = 1,
    ) -> None:
        """失敗情報を初期化する。

        Args:
            stage: 失敗した処理段階。
            statistics: 失敗までに収集できた統計。
            error: 親とstderrへ返す英語診断。
            exit_code: CLI契約上の終了コード。

        Returns:
            None
        """

        super().__init__(error)
        # main() が例外発生後も処理段階を JSON 化するために保持する。
        self.stage: CMLogicalAudioRebuildStage = stage
        # 有効フレーム0を含め、失敗直前までの統計を必ず返すために保持する。
        self.statistics = statistics
        # UI/DBへ残す診断の原文を保持する。
        self.error = error
        # 1（回復不能）と2（有効フレーム0）を親が区別するために保持する。
        self.exit_code = exit_code


class _CMLogicalAudioArgumentError(Exception):
    """argparseが検出した内部CLI引数エラー。"""


class _CMLogicalAudioArgumentParser(argparse.ArgumentParser):
    """argparse の直接終了を JSON CLI 契約へ変換する。"""

    def error(self, message: str) -> NoReturn:
        """引数エラーを SystemExit ではなく main() へ返す。

        Args:
            message: argparse が生成した英語診断。

        Returns:
            None
        """

        raise _CMLogicalAudioArgumentError(message)


class _PCMFrameAssembler:
    """Amatsukaze StreamReform と同じ規則でPTS付きPCMフレームを並べる。"""

    def __init__(
        self,
        write_frame: Callable[[bytes, int, int], None],
        maximum_samples: int,
        statistics: CMLogicalAudioRebuildStatistics | None = None,
    ) -> None:
        # push()/finish() がPCMを逐次公開する唯一の書き込み先。
        self._write_frame = write_frame
        # 映像尺を越えて音声を生成しないための48kHz sample上限。
        self._maximum_samples = maximum_samples
        # decode/demux段と同じ統計を更新できるよう、呼び出し側から共有可能にする。
        self.statistics = statistics or CMLogicalAudioRebuildStatistics()

    @property
    def is_complete(self) -> bool:
        """映像尺に必要な音声フレームを既に出力したか。"""

        return self.statistics.output_samples >= self._maximum_samples

    def push(self, start_sample: int, samples: int, pcm: bytes) -> None:
        """一つの有効PCMフレームをPTS位置へ追加する。"""

        if samples <= 0 or len(pcm) != samples * 4 or self.is_complete:
            return
        cursor = self.statistics.output_samples
        # Amatsukaze はフレーム中央が現在位置より前なら古いフレームとして捨てる。
        if start_sample + (samples / 2) < cursor:
            self.statistics.skipped_frames += 1
            return

        # 空きは無音にせず、次に見つかった有効フレームを必要数だけ複製する。
        # StreamReform.cpp と同じく frameDuration/4 を加えて整数へ切り捨てる。
        frame_count = max(1, int(((start_sample - cursor) + (samples / 4)) / samples))
        remaining_frame_count = max(
            1,
            math.ceil((self._maximum_samples - cursor) / samples),
        )
        frame_count = min(frame_count, remaining_frame_count)
        for _ in range(frame_count):
            output_samples = min(
                samples,
                self._maximum_samples - self.statistics.output_samples,
            )
            self._write_frame(
                pcm[:output_samples * 4],
                output_samples,
                self.statistics.output_samples,
            )
            self.statistics.output_samples += output_samples
        self.statistics.decoded_frames += 1
        self.statistics.duplicated_frames += frame_count - 1

    def finish(self) -> None:
        """後続フレームがない末尾だけを無音で映像尺まで補う。"""

        silence = bytes(1024 * 4)
        while self.is_complete is False:
            samples = min(1024, self._maximum_samples - self.statistics.output_samples)
            self._write_frame(
                silence[:samples * 4],
                samples,
                self.statistics.output_samples,
            )
            self.statistics.output_samples += samples
            self.statistics.padded_samples += samples


def _isMPEGTS(format_name: str | None) -> bool:
    """FFprobeのformat名からMPEG-TSを判定する。"""

    return format_name is not None and 'mpegts' in {
        item.strip().lower()
        for item in format_name.split(',')
    }


def _selectAudioStream(
    container: Any,
    stream_index: int,
    stream_id: int | None,
) -> Any:
    """同じ入力probeで確定した論理音声0の初期streamを選ぶ。"""

    audio_streams = [stream for stream in container.streams if stream.type == 'audio']
    if stream_id is not None:
        matched_by_id = next(
            (stream for stream in audio_streams if cast(int | None, stream.id) == stream_id),
            None,
        )
        if matched_by_id is not None:
            return matched_by_id
    matched_by_index = next(
        (stream for stream in audio_streams if cast(int, stream.index) == stream_index),
        None,
    )
    if matched_by_index is not None:
        return matched_by_index
    raise RuntimeError('The selected logical audio stream is unavailable.')


def _decodePacket(
    packet: Any,
    statistics: CMLogicalAudioRebuildStatistics,
) -> list[Any]:
    """一つのpacketをdecodeし、既知のFFmpeg/POSIXエラーだけを隔離する。

    Args:
        packet: 選択済み論理音声streamのPyAV packet。
        statistics: packet単位の失敗を記録する処理全体統計。

    Returns:
        decodeできた音声フレーム。既知の失敗時は空リスト。
    """

    try:
        return cast(list[Any], packet.decode())
    except (FFmpegError, OSError) as ex:
        # InvalidDataErrorだけでなく、avcodec_send_packet()のEPERMがPython側で
        # PermissionErrorになる場合も同じ無効フレームとして飛ばす。Amatsukazeも
        # 無効なAACフレームを列へ入れず、次の有効フレームで空きを水増しする。
        statistics.decode_errors += 1
        if statistics.decode_errors <= 5:
            print(
                f'Logical audio packet decode failed and was skipped: {type(ex).__name__}: {ex}',
                file=sys.stderr,
            )
        elif statistics.decode_errors == 6:
            print('Further logical audio packet decode errors are suppressed.', file=sys.stderr)
        return []


def rebuildLogicalAudio(
    input_path: Path,
    output_path: Path,
    *,
    format_name: str | None,
    stream_index: int,
    stream_id: int | None,
    video_start_time_seconds: float,
    video_duration_seconds: float,
    statistics: CMLogicalAudioRebuildStatistics | None = None,
) -> CMLogicalAudioRebuildStatistics:
    """入力形式に依存せず、論理音声0を映像PTS基準の48kHz stereo PCMへ再構築する。"""

    rebuild_statistics = statistics or CMLogicalAudioRebuildStatistics()
    if math.isfinite(video_start_time_seconds) is False:
        raise _CMLogicalAudioRebuildFailure(
            'Input',
            rebuild_statistics,
            'The video start time must be finite.',
        )
    if math.isfinite(video_duration_seconds) is False or video_duration_seconds <= 0:
        raise _CMLogicalAudioRebuildFailure(
            'Input',
            rebuild_statistics,
            'The video duration must be a finite positive number.',
        )
    maximum_samples = max(1, math.ceil(video_duration_seconds * 48_000))
    is_mpegts = _isMPEGTS(format_name)
    input_container: Any | None = None
    output_container: Any | None = None
    # ARIB TSではstream_identifierが通常付くため、FFmpegは同一componentの
    # PID交代を一つのAVStreamへ統合する。記述子のない特殊TSではFFmpegの
    # PMT内ES位置fallbackとなり、AmatsukazeのAACだけの記載順とは差が残る。
    # ここが自前TS parserでaudioIdxを追跡するAmatsukazeとの相違点であり、
    # probeで確定したstream_id/index以外へ途中で乗り換えないことを優先する。
    input_options = {'merge_pmt_versions': '1'} if is_mpegts else None
    try:
        try:
            opened_input_container = cast(Any, av.open(str(input_path), options=input_options))
            input_container = opened_input_container
        except Exception as ex:
            raise _CMLogicalAudioRebuildFailure(
                'Input',
                rebuild_statistics,
                f'{type(ex).__name__}: {ex}',
            ) from ex

        try:
            selected_stream = _selectAudioStream(opened_input_container, stream_index, stream_id)
        except Exception as ex:
            raise _CMLogicalAudioRebuildFailure(
                'StreamSelection',
                rebuild_statistics,
                f'{type(ex).__name__}: {ex}',
            ) from ex
        try:
            selected_stream_index = cast(int, selected_stream.index)
        except Exception as ex:
            raise _CMLogicalAudioRebuildFailure(
                'StreamSelection',
                rebuild_statistics,
                f'{type(ex).__name__}: {ex}',
            ) from ex
        try:
            opened_output_container = cast(Any, av.open(
                str(output_path),
                mode='w',
                format='wav',
                options={'rf64': 'auto'},
            ))
            output_container = opened_output_container
            output_stream = opened_output_container.add_stream('pcm_s16le', rate=48_000)
            output_stream.layout = 'stereo'
        except Exception as ex:
            raise _CMLogicalAudioRebuildFailure(
                'Write',
                rebuild_statistics,
                f'{type(ex).__name__}: {ex}',
            ) from ex

        def WriteFrame(pcm: bytes, samples: int, start_sample: int) -> None:
            """PCM packetをWAV muxerへ書き込む。

            Args:
                pcm: stereo s16le PCM。
                samples: 1chあたりのsample数。
                start_sample: 48kHz出力時間軸上の開始sample。

            Returns:
                None
            """

            try:
                packet = av.Packet(pcm)
                packet.stream = output_stream
                packet.pts = start_sample
                packet.dts = start_sample
                packet.duration = samples
                packet.time_base = Fraction(1, 48_000)
                opened_output_container.mux(packet)
            except Exception as ex:
                raise _CMLogicalAudioRebuildFailure(
                    'Write',
                    rebuild_statistics,
                    f'{type(ex).__name__}: {ex}',
                ) from ex

        assembler = _PCMFrameAssembler(WriteFrame, maximum_samples, rebuild_statistics)
        resampler: Any | None = None
        resampler_signature: tuple[str, str, int] | None = None
        next_resampled_start_sample = 0

        def PushResampledFrames(
            resampled_frames: list[Any],
            fallback_start_sample: int,
        ) -> int:
            """resample済みフレームをPTS順にassemblerへ渡す。

            Args:
                resampled_frames: PyAVが48kHz stereo s16へ変換したフレーム。
                fallback_start_sample: PTS欠落時に用いる直前フレーム末尾。

            Returns:
                次のPTS欠落フレームに使う開始sample。
            """

            current_start_sample = fallback_start_sample
            for resampled_frame in resampled_frames:
                try:
                    pcm = resampled_frame.to_ndarray().tobytes()
                    samples = cast(int, resampled_frame.samples)
                    # resamplerがPTSを保持した場合は丸め誤差を含めてもそちらを正とする。
                    if resampled_frame.pts is not None and resampled_frame.time_base is not None:
                        current_start_sample = round(
                            (
                                float(resampled_frame.pts * resampled_frame.time_base)
                                - video_start_time_seconds
                            ) * 48_000,
                        )
                    assembler.push(current_start_sample, samples, pcm)
                except _CMLogicalAudioRebuildFailure:
                    raise
                except Exception as ex:
                    raise _CMLogicalAudioRebuildFailure(
                        'Assemble',
                        rebuild_statistics,
                        f'{type(ex).__name__}: {ex}',
                    ) from ex
                current_start_sample += samples
                if assembler.is_complete:
                    break
            return current_start_sample

        # demux(selected_stream) は動的PMTでStreamContainerが更新される際、PyAV側で
        # 選択indexを再解決してIndexErrorになることがある。全packetをdemuxして、
        # merge_pmt_versionsが維持する論理stream indexをこちらで絞り込む。
        try:
            demux_iterator = iter(opened_input_container.demux())
        except Exception as ex:
            rebuild_statistics.demux_errors += 1
            raise _CMLogicalAudioRebuildFailure(
                'Demux',
                rebuild_statistics,
                f'{type(ex).__name__}: {ex}',
            ) from ex
        demux_failed = False
        while True:
            try:
                packet = next(demux_iterator)
            except StopIteration:
                break
            except IndexError as ex:
                if is_mpegts is False:
                    assembler.statistics.demux_errors += 1
                    raise _CMLogicalAudioRebuildFailure(
                        'Demux',
                        rebuild_statistics,
                        f'{type(ex).__name__}: {ex}',
                    ) from ex
                # PyAV 16 / FFmpeg 8 はmerge中に追加された未選択streamをEOFで
                # StreamContainerへ引き直す際、稀に範囲外indexを返す。選択音声が
                # 映像末尾まで到達済みなら既得frameを保ち、末尾paddingへ進める。
                assembler.statistics.demux_errors += 1
                demux_failed = True
                break
            except (FFmpegError, OSError) as ex:
                # demux iterator自体が失敗した後の再開可否は保証されないため、
                # packet.decode()と違って同じiteratorを回し続けず段階付きで終了する。
                assembler.statistics.demux_errors += 1
                raise _CMLogicalAudioRebuildFailure(
                    'Demux',
                    rebuild_statistics,
                    f'{type(ex).__name__}: {ex}',
                ) from ex
            except Exception as ex:
                assembler.statistics.demux_errors += 1
                raise _CMLogicalAudioRebuildFailure(
                    'Demux',
                    rebuild_statistics,
                    f'{type(ex).__name__}: {ex}',
                ) from ex
            try:
                packet_stream_index = cast(int, packet.stream.index)
            except Exception as ex:
                rebuild_statistics.demux_errors += 1
                raise _CMLogicalAudioRebuildFailure(
                    'Demux',
                    rebuild_statistics,
                    f'{type(ex).__name__}: {ex}',
                ) from ex
            if packet_stream_index != selected_stream_index:
                continue
            try:
                decoded_frames = _decodePacket(packet, rebuild_statistics)
            except Exception as ex:
                # 未知例外を握りつぶしてPyAVの内部状態を再利用せず、安全に子だけ終了する。
                raise _CMLogicalAudioRebuildFailure(
                    'Decode',
                    rebuild_statistics,
                    f'{type(ex).__name__}: {ex}',
                ) from ex
            for decoded_frame in decoded_frames:
                try:
                    if decoded_frame.pts is None:
                        frame_start_sample = assembler.statistics.output_samples
                    else:
                        frame_time_base = decoded_frame.time_base or packet.time_base
                        if frame_time_base is None:
                            frame_start_sample = assembler.statistics.output_samples
                        else:
                            frame_start_sample = round(
                                (
                                    float(decoded_frame.pts * frame_time_base)
                                    - video_start_time_seconds
                                ) * 48_000,
                            )
                    signature = (
                        cast(str, decoded_frame.format.name),
                        cast(str, decoded_frame.layout.name),
                        cast(int, decoded_frame.sample_rate),
                    )
                    if resampler is None or signature != resampler_signature:
                        if resampler is not None:
                            next_resampled_start_sample = PushResampledFrames(
                                resampler.resample(None),
                                next_resampled_start_sample,
                            )
                        # AmatsukazeのAUDIO_FORMAT_CHANGEDイベントに相当し、入力
                        # formatが変わった境界でresamplerの履歴を持ち越さない。
                        resampler = av.AudioResampler(format='s16', layout='stereo', rate=48_000)
                        resampler_signature = signature
                    resampled_frames = resampler.resample(decoded_frame)
                except _CMLogicalAudioRebuildFailure:
                    raise
                except Exception as ex:
                    raise _CMLogicalAudioRebuildFailure(
                        'Assemble',
                        rebuild_statistics,
                        f'{type(ex).__name__}: {ex}',
                    ) from ex
                next_resampled_start_sample = PushResampledFrames(
                    resampled_frames,
                    frame_start_sample,
                )
                if assembler.is_complete:
                    break
            if assembler.is_complete:
                break

        if (
            demux_failed
            and (
                assembler.statistics.output_samples < maximum_samples * 0.95
                or maximum_samples - assembler.statistics.output_samples > 5 * 48_000
            )
        ):
            raise _CMLogicalAudioRebuildFailure(
                'Demux',
                rebuild_statistics,
                'MPEG-TS demuxing stopped before logical audio 0 reached the video tail.',
            )
        if resampler is not None and assembler.is_complete is False:
            try:
                flushed_frames = resampler.resample(None)
            except Exception as ex:
                raise _CMLogicalAudioRebuildFailure(
                    'Assemble',
                    rebuild_statistics,
                    f'{type(ex).__name__}: {ex}',
                ) from ex
            PushResampledFrames(flushed_frames, next_resampled_start_sample)
        if assembler.statistics.decoded_frames == 0:
            raise _CMLogicalAudioRebuildFailure(
                'Decode',
                rebuild_statistics,
                'No decodable frame was found in logical audio stream 0.',
                exit_code=2,
            )
        try:
            # StreamReform::fillAudioFrames()は末尾に有効フレームがなければ短いままにする。
            # ここだけはchapter_exeへ固定長WAVを渡すKonomiTV-BS4K契約として無音補完する。
            assembler.finish()
        except _CMLogicalAudioRebuildFailure:
            raise
        except Exception as ex:
            raise _CMLogicalAudioRebuildFailure(
                'Assemble',
                rebuild_statistics,
                f'{type(ex).__name__}: {ex}',
            ) from ex
        try:
            opened_output_container.close()
            output_container = None
        except Exception as ex:
            raise _CMLogicalAudioRebuildFailure(
                'Write',
                rebuild_statistics,
                f'{type(ex).__name__}: {ex}',
            ) from ex
        return rebuild_statistics
    finally:
        if input_container is not None:
            try:
                input_container.close()
            except Exception:
                pass
        if output_container is not None:
            try:
                output_container.close()
            except Exception:
                pass


def _buildArgumentParser() -> argparse.ArgumentParser:
    """CMAnalyzerから呼び出す内部CLIを定義する。"""

    parser = _CMLogicalAudioArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--format-name')
    parser.add_argument('--stream-index', type=int, required=True)
    parser.add_argument('--stream-id', type=int)
    parser.add_argument('--video-start-time', type=float, required=True)
    parser.add_argument('--video-duration', type=float, required=True)
    return parser


def main() -> int:
    """内部CLIのentry point。"""

    statistics = CMLogicalAudioRebuildStatistics()
    output_path: Path | None = None
    try:
        arguments = _buildArgumentParser().parse_args()
        output_path = arguments.output
        rebuildLogicalAudio(
            arguments.input,
            arguments.output,
            format_name=arguments.format_name,
            stream_index=arguments.stream_index,
            stream_id=arguments.stream_id,
            video_start_time_seconds=arguments.video_start_time,
            video_duration_seconds=arguments.video_duration,
            statistics=statistics,
        )
    except _CMLogicalAudioArgumentError as ex:
        failure = _CMLogicalAudioRebuildFailure(
            'Arguments',
            statistics,
            f'ArgumentError: {ex}',
        )
    except _CMLogicalAudioRebuildFailure as ex:
        failure = ex
    except Exception as ex:
        failure = _CMLogicalAudioRebuildFailure(
            'Input',
            statistics,
            f'{type(ex).__name__}: {ex}',
        )
    else:
        report = CMLogicalAudioRebuildReportJSON(
            stage='Completed',
            statistics=cast(dict[str, int], asdict(statistics)),
            error=None,
        )
        print(json.dumps(report, separators=(',', ':'), sort_keys=True))
        return 0

    # 親が空/partial WAVを成功と誤認しないよう、失敗時の生成物は必ず除去する。
    if output_path is not None:
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            pass
    print(failure.error, file=sys.stderr)
    report = CMLogicalAudioRebuildReportJSON(
        stage=failure.stage,
        statistics=cast(dict[str, int], asdict(failure.statistics)),
        error=failure.error,
    )
    print(json.dumps(report, separators=(',', ':'), sort_keys=True))
    return failure.exit_code


if __name__ == '__main__':
    raise SystemExit(main())
