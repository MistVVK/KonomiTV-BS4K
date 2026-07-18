
import hashlib
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Literal, cast

import typer
from biim.mpeg2ts import ts
from pydantic import BaseModel, field_validator
from rich import print

from app import logging, schemas
from app.config import Config, LoadConfig
from app.constants import JST, LIBRARY_PATH
from app.metadata.TSInfoAnalyzer import TSInfoAnalyzer
from app.utils import ClosestMultiple
from app.utils.TSInformation import TSInformation
from app.utils.TSKeyFrameSeeker import TSKeyFrameSeeker, TSStreamInfo


class FFprobeFormat(BaseModel):
    """FFprobe から返される format セクションの情報"""
    format_name: str  # 必須 - "mpegts", "mp4" など
    format_long_name: str | None = None
    duration: str | None = None  # オプショナル - 秒数の文字列、TS ファイルでは稀に取得できない場合がある
    size: str | None = None
    bit_rate: str | None = None

    @field_validator('format_name')
    @classmethod
    def validate_container_format(cls, format_name: str) -> str:
        """サポートされているコンテナ形式かを検証"""
        if format_name.strip() == '':
            raise ValueError('Container format is empty.')
        return format_name

class FFprobeVideoStream(BaseModel):
    """FFprobe から返される映像ストリームの情報"""
    index: int
    codec_type: Literal['video']  # 必須 - "video" 固定
    codec_name: str  # 必須 - "mpeg2video", "h264", "hevc" など
    duration: float | None = None  # オプショナル - 映像の長さ（秒）（部分解析時はパイプ渡しのため取得できない）
    profile: str | None = None  # オプショナル - "High@L4.0", "Main@L3.1" など
    width: int  # 必須 - 映像幅
    height: int  # 必須 - 映像高さ
    avg_frame_rate: str  # 必須 - "30000/1001", "30/1" など分数形式
    r_frame_rate: str  # 必須 - "30000/1001", "30/1" など分数形式
    field_order: str | None = None  # オプショナル - "progressive", "tt", "bb" など
    ts_packetsize: str | None = None  # オプショナル - TS パケットサイズ ("188" / "192" / "204")
    tags: dict[str, str] = {}

    @field_validator('codec_name')
    @classmethod
    def validate_video_codec(cls, codec_name: str) -> str:
        """サポートされている映像コーデックかを検証"""
        if codec_name.strip() == '':
            raise ValueError('Video codec is empty.')
        return codec_name

    @field_validator('width', 'height')
    @classmethod
    def validate_resolution(cls, resolution: int) -> int:
        """解像度が有効な値かを検証"""
        if resolution <= 0:
            raise ValueError(f'Invalid resolution: {resolution}')
        return resolution

    @field_validator('avg_frame_rate', 'r_frame_rate')
    @classmethod
    def validate_frame_rate(cls, frame_rate: str) -> str:
        """フレームレートが有効な形式かを検証"""
        if not frame_rate or frame_rate == '0/0':
            raise ValueError(f'Invalid frame rate: {frame_rate}')
        return frame_rate

class FFprobeAudioStream(BaseModel):
    """FFprobe から返される音声ストリームの情報"""
    index: int
    codec_type: Literal['audio']  # 必須 - "audio" 固定
    codec_name: str  # 必須 - "aac" など
    duration: float | None = None  # オプショナル - 音声の長さ（秒）（部分解析時はパイプ渡しのため取得できない）
    start_time: float | None = None
    id: str | int | None = None
    profile: str | None = None  # オプショナル - "LC", "HE-AAC" など
    # TS の PMT には存在するがまだ PES が出現していない音声は FFprobe が channels=0 / sample_rate欠落で返す。
    # 動的音声タイムラインの候補として保持し、実データがあるサンプル解析の値で後から補完する。
    channels: int = 0
    sample_rate: str = '48000'
    ts_packetsize: str | None = None  # オプショナル - TS パケットサイズ ("188" / "192" / "204")
    channel_layout: str | None = None
    tags: dict[str, str] = {}

    @field_validator('codec_name')
    @classmethod
    def validate_audio_codec(cls, codec_name: str) -> str:
        """サポートされている音声コーデックかを検証"""
        if codec_name.strip() == '':
            raise ValueError('Audio codec is empty.')
        return codec_name

    @field_validator('channels')
    @classmethod
    def validate_channels(cls, channels: int) -> int:
        """サポートされているチャンネル数かを検証"""
        if channels < 0:
            raise ValueError(f'Unsupported channel count: {channels}')
        return channels

    @field_validator('sample_rate')
    @classmethod
    def validate_sample_rate(cls, sample_rate: str) -> str:
        """サンプルレートが有効な値かを検証"""
        try:
            rate = int(sample_rate)
            if rate <= 0:
                raise ValueError(f'Invalid sample rate: {sample_rate}')
        except ValueError:
            raise ValueError(f'Invalid sample rate format: {sample_rate}')
        return sample_rate

class FFprobeOtherStream(BaseModel):
    """FFprobe から返される映像・音声以外のストリームの情報"""
    index: int
    codec_type: str = 'unknown'  # オプショナル - "subtitle", "data", そのほか未知のもの
    codec_name: str = 'unknown'  # オプショナル - "arib_caption", "bin_data", そのほか未知のもの
    id: str | int | None = None
    ts_packetsize: str | None = None  # オプショナル - TS パケットサイズ ("188" / "192" / "204")
    tags: dict[str, str] = {}

class FFprobeSubtitleStream(BaseModel):
    """FFprobe から返される字幕ストリームの情報"""
    index: int
    codec_type: Literal['subtitle']
    codec_name: str
    id: str | int | None = None
    tags: dict[str, str] = {}

class FFprobeFrame(BaseModel):
    """FFprobe から返される映像フレームの走査方式情報"""
    interlaced_frame: int | None = None  # 0: プログレッシブ、1: インターレース

class FFprobeProgram(BaseModel):
    """FFprobe から返される program セクションの情報"""
    program_id: int | None = None
    program_num: int | None = None
    nb_streams: int | None = None
    pmt_pid: int | None = None
    pcr_pid: int | None = None
    streams: list[dict[str, Any]] = []

class FFprobeResult(BaseModel):
    """FFprobe から返される JSON 全体の情報"""
    format: FFprobeFormat
    streams: list[FFprobeVideoStream | FFprobeAudioStream | FFprobeSubtitleStream | FFprobeOtherStream] = []
    programs: list[FFprobeProgram] = []

    def getVideoStreams(self) -> list[FFprobeVideoStream]:
        """映像ストリームのみを抽出してバリデーション"""
        video_streams = []
        for stream in self.streams:
            # 厳密に FFprobeVideoStream 型のみを許可する（詳細情報を取得できない場合は FFprobeOtherStream になる）
            if isinstance(stream, FFprobeVideoStream):
                try:
                    video_streams.append(stream)
                except Exception as ex:
                    logging.warning('Invalid video stream data:', exc_info=ex)
        return video_streams

    def getAudioStreams(self) -> list[FFprobeAudioStream]:
        """音声ストリームのみを抽出してバリデーション"""
        audio_streams = []
        for stream in self.streams:
            # 厳密に FFprobeAudioStream 型のみを許可する（詳細情報を取得できない場合は FFprobeOtherStream になる）
            if isinstance(stream, FFprobeAudioStream):
                try:
                    audio_streams.append(stream)
                except Exception as ex:
                    logging.warning('Invalid audio stream data:', exc_info=ex)
        return audio_streams

    def getSubtitleStreams(self) -> list[FFprobeSubtitleStream]:
        return [stream for stream in self.streams if isinstance(stream, FFprobeSubtitleStream)]

class FFprobeSampleResult(BaseModel):
    """FFprobe のサンプル解析から返される情報（映像・音声のみ）"""
    streams: list[FFprobeVideoStream | FFprobeAudioStream | FFprobeSubtitleStream | FFprobeOtherStream] = []
    frames: list[FFprobeFrame] = []

    def getVideoStreams(self) -> list[FFprobeVideoStream]:
        """映像ストリームのみを抽出してバリデーション"""
        video_streams = []
        for stream in self.streams:
            # 厳密に FFprobeVideoStream 型のみを許可する（詳細情報を取得できない場合は FFprobeOtherStream になる）
            if isinstance(stream, FFprobeVideoStream):
                try:
                    video_streams.append(stream)
                except Exception as ex:
                    logging.warning('Invalid video stream data:', exc_info=ex)
        return video_streams

    def getAudioStreams(self) -> list[FFprobeAudioStream]:
        """音声ストリームのみを抽出してバリデーション"""
        audio_streams = []
        for stream in self.streams:
            # 厳密に FFprobeAudioStream 型のみを許可する（詳細情報を取得できない場合は FFprobeOtherStream になる）
            if isinstance(stream, FFprobeAudioStream):
                try:
                    audio_streams.append(stream)
                except Exception as ex:
                    logging.warning('Invalid audio stream data:', exc_info=ex)
        return audio_streams

    def getSubtitleStreams(self) -> list[FFprobeSubtitleStream]:
        return [stream for stream in self.streams if isinstance(stream, FFprobeSubtitleStream)]


class MetadataAnalyzer:
    """
    録画ファイルのメタデータを解析するクラス
    app.metadata モジュール内の各クラスを統括し、録画ファイルから取り出せるだけのメタデータを取り出す
    """

    # FFprobe が VPS/SPS/PPS を確実に読み込めるよう解析対象の尺とサイズを拡張する
    FFPROBE_ANALYZE_DURATION_US: ClassVar[str] = str(30 * 1_000_000)
    FFPROBE_PROBESIZE: ClassVar[str] = '80M'
    # TS の映像ストリーム変化検出で、各サンプル位置から読み込む最大バイト数
    ## 末尾サンプルでも同じ値を使い、割合指定だけでは短くなりやすい小さめの録画でも PMT を拾える範囲を確保する
    MAX_STREAM_SCAN_BYTES: ClassVar[int] = 8 * 1024 * 1024

    def __init__(self, recorded_file_path: Path) -> None:
        """
        録画ファイルのメタデータを解析するクラスを初期化する

        Args:
            recorded_file_path (Path): 録画ファイルのパス
        """

        self.recorded_file_path = recorded_file_path


    def analyze(self) -> schemas.RecordedProgram | None:
        """
        録画ファイル内のメタデータを解析する
        このメソッドは同期的なため、非同期メソッドから実行する際は asyncio.to_thread() または ProcessPoolExecutor で実行すること

        Returns:
            schemas.RecordedProgram | None: 録画番組情報（中に録画ファイル情報・チャンネル情報が含まれる）を表すモデル
                (KonomiTV で再生可能なファイルではない場合は None が返される)
        """

        def ParseFPS(fps_string: str | None) -> float | None:
            """
            FFprobe から返されるフレームレート文字列をパースして float に変換する

            Args:
                fps_string (str | None): "30000/1001", "30/1" などの分数形式の文字列

            Returns:
                float | None: パースされたフレームレート、失敗時は None
            """
            if fps_string is None:
                return None
            if '/' in fps_string:
                try:
                    num, den = fps_string.split('/')
                    if float(den) == 0.0:
                        return None
                    return round(float(num) / float(den), 2)
                except Exception:
                    return None
            try:
                return round(float(fps_string), 2)
            except Exception:
                return None

        def GetAudioCodec(codec_name: str, profile: str | None) -> str:
            if codec_name == 'aac':
                if profile is None or 'LC' in profile:
                    return 'AAC-LC'
                return 'AAC'
            if codec_name == 'mp2':
                return 'MP2'
            return codec_name.replace('_', ' ').upper()

        def GetAudioChannelLabel(channels: int) -> str:
            if channels == 1:
                return 'Monaural'
            if channels == 2:
                return 'Stereo'
            if channels == 6:
                return '5.1ch'
            if channels == 24:
                return '22.2ch'
            return f'{channels}ch'

        # もし Config() の実行時に AssertionError が発生した場合は、LoadConfig() を実行してサーバー設定データをロードする
        ## 通常ならこの関数を ProcessPoolExecutor で実行した場合もサーバー設定データはロード状態になっているはずだが、
        ## 自動リロードモード時のみなぜかグローバル変数がマルチプロセスに引き継がれないため、明示的にロードさせる必要がある
        try:
            Config()
        except AssertionError:
            # バリデーションは既にサーバー起動時に行われているためスキップする
            LoadConfig(bypass_validation=True)

        # 必要な情報を一旦変数として保持
        duration: float | None = None
        container_format: str | None = None
        video_codec: str | None = None
        video_codec_profile: str | None = None
        video_scan_type: Literal['Interlaced', 'Progressive'] | None = None
        video_frame_rate: float | None = None
        video_resolution_width: int | None = None
        video_resolution_height: int | None = None
        has_video_stream_changes: bool = False
        primary_audio_codec: str | None = None
        primary_audio_channel: str | None = None
        primary_audio_sampling_rate: int | None = None
        secondary_audio_codec: str | None = None
        secondary_audio_channel: str | None = None
        secondary_audio_sampling_rate: int | None = None
        audio_tracks: list[schemas.AudioTrack] = []
        subtitle_tracks: list[schemas.SubtitleTrack] = []

        # FFprobe から録画ファイルのメディア情報を取得
        ## 取得に失敗した場合は KonomiTV で再生可能なファイルではないと判断し、None を返す
        result = self.__analyzeFFprobe()
        if result is None:
            return None
        full_probe, sample_probe, end_ts_offset = result
        logging.debug(f'{self.recorded_file_path}: FFprobe analysis completed.')

        # MPEG-TS の TS パケットサイズが 188 以外であれば弾く（BDAV 等は非対応）
        if full_probe.format.format_name == 'mpegts':
            try:
                sizes: list[int] = []
                for stream in full_probe.streams:
                    packet_size = getattr(stream, 'ts_packetsize', None)
                    if packet_size is None:
                        continue
                    sizes.append(int(packet_size))
                if len(sizes) > 0 and any(size != 188 for size in sizes):
                    first_bad = next(size for size in sizes if size != 188)
                    logging.warning(f'{self.recorded_file_path}: Unsupported TS packet size detected: {first_bad} bytes.')
                    return None
            except Exception as ex:
                logging.warning(f'{self.recorded_file_path}: Failed to validate ts_packetsize:', exc_info=ex)

        # メディア情報から録画ファイルのメタデータを取得
        # 全般（コンテナ情報）
        ## コンテナ形式
        format_names = set(full_probe.format.format_name.lower().split(','))
        if 'mpegts' in format_names:
            container_format = 'MPEG-TS'
        elif 'mp4' in format_names or 'mov' in format_names:
            container_format = 'MPEG-4'
        elif 'matroska' in format_names:
            container_format = 'WebM' if 'webm' in format_names or self.recorded_file_path.suffix.lower() == '.webm' else 'Matroska'
        elif 'ogg' in format_names:
            container_format = 'Ogg'
        else:
            container_format = full_probe.format.format_long_name or full_probe.format.format_name

        ## 部分解析: 映像・音声情報を取得
        is_video_track_analyzed = False
        is_primary_audio_track_analyzed = False
        is_secondary_audio_track_analyzed = False
        full_probe_video_streams = full_probe.getVideoStreams()
        full_probe_audio_streams = full_probe.getAudioStreams()
        sample_probe_video_streams = sample_probe.getVideoStreams()
        sample_probe_audio_streams = sample_probe.getAudioStreams()
        selected_program_stream_indices: set[int] | None = None
        # MPEG-TS に複数サービスが含まれる場合、代表映像（映像がなければ代表音声）と同じ program だけを採用する
        ## 他サービスの音声を HLS 代替音声として誤登録しないため、FFprobe の program.streams を利用する
        if full_probe.format.format_name == 'mpegts' and (sample_probe_video_streams or sample_probe_audio_streams):
            representative_stream_index = (sample_probe_video_streams or sample_probe_audio_streams)[0].index
            for program in full_probe.programs:
                program_stream_indices = {
                    int(stream['index']) for stream in program.streams if stream.get('index') is not None
                }
                if representative_stream_index in program_stream_indices:
                    selected_program_stream_indices = program_stream_indices
                    break
            if selected_program_stream_indices is not None:
                full_probe_video_streams = [
                    stream for stream in full_probe_video_streams if stream.index in selected_program_stream_indices
                ]
                full_probe_audio_streams = [
                    stream for stream in full_probe_audio_streams if stream.index in selected_program_stream_indices
                ]
                sample_probe_video_streams = [
                    stream for stream in sample_probe_video_streams if stream.index in selected_program_stream_indices
                ]
                sample_probe_audio_streams = [
                    stream for stream in sample_probe_audio_streams if stream.index in selected_program_stream_indices
                ]
        if len(full_probe_video_streams) == 0 and len(full_probe_audio_streams) == 0:
            logging.warning(f'{self.recorded_file_path}: No valid video or audio streams found. (from full probe)')
            return None
        if len(sample_probe_audio_streams) == 0:
            logging.warning(f'{self.recorded_file_path}: No valid audio streams found. (from sample probe), falling back to full probe audio streams.')
            sample_probe_audio_streams = full_probe_audio_streams

        ## FFprobe の結果として "video" / "audio" の codec_type そのものは存在するが、
        ## 詳細が取得できず FFprobeOtherStream にフォールバックしているケース（スクランブルや不正 TS）を検出する
        has_video_codec_type = any(s.codec_type == 'video' for s in sample_probe.streams)
        has_audio_codec_type = any(s.codec_type == 'audio' for s in sample_probe.streams)
        if len(sample_probe_video_streams) == 0 and has_video_codec_type is True:
            logging.warning(f'{self.recorded_file_path}: Video stream details are missing. (Is the TS scrambled or unsupported?)')
            return None
        if len(sample_probe_audio_streams) == 0 and has_audio_codec_type is True:
            logging.warning(f'{self.recorded_file_path}: Audio stream details are missing. (Is the TS scrambled or unsupported?)')
            return None
        if len(sample_probe_video_streams) == 0 and len(sample_probe_audio_streams) == 0:
            logging.warning(f'{self.recorded_file_path}: No valid video or audio streams found.')
            return None

        ## 再生時間
        duration_streams = full_probe_video_streams or full_probe_audio_streams
        representative_duration = duration_streams[0].duration
        if full_probe.format.duration is not None:
            ## コンテナ自体の再生時間が取得できている場合（通常のケース）
            if (representative_duration is not None and
                representative_duration < float(full_probe.format.duration)):
                ## コンテナ自体の再生時間は特に tsreplace した TS データなどでは映像や音声よりも長くなる場合があるため、
                ## 映像ストリームの再生時間が取得できていて、かつコンテナ自体の再生時間より短い場合はそれを優先的に使う
                duration = representative_duration
            else:
                ## 万が一映像ストリームの再生時間が取得できていない場合、コンテナ自体の再生時間を使う
                duration = float(full_probe.format.duration)
        elif representative_duration is not None:
            ## 万が一コンテナ自体の再生時間が取得できていない場合、映像ストリームの再生時間が取得できていればそれを使う
            duration = representative_duration
        else:
            ## どちらの再生時間も取得できていない場合は録画ファイルが破損していると判断し、None を返す
            logging.warning(f'{self.recorded_file_path}: Duration is missing or invalid. ignored.')
            return None

        ## 映像情報
        for video_stream in sample_probe_video_streams:
            if is_video_track_analyzed is False:
                ## コーデック
                if video_stream.codec_name == 'mpeg2video':
                    video_codec = 'MPEG-2'
                elif video_stream.codec_name == 'h264':
                    video_codec = 'H.264'
                elif video_stream.codec_name == 'hevc':
                    video_codec = 'H.265'
                elif video_stream.codec_name == 'av1':
                    video_codec = 'AV1'
                elif video_stream.codec_name == 'vp9':
                    video_codec = 'VP9'
                elif video_stream.codec_name == 'vp8':
                    video_codec = 'VP8'
                elif video_stream.codec_name == 'theora':
                    video_codec = 'Theora'
                else:
                    video_codec = video_stream.codec_name.replace('_', ' ').upper()
                ## プロファイル
                ## Main@High や High@L5 など @ 区切りで Level や Tier などが付与されている場合があるので、それらを除去する
                profile = video_stream.profile or ''
                video_codec_profile = profile.split('@')[0] if profile else 'Unknown'
                ## スキャン形式
                ## HEVC エンコーダーでインターレース解除した映像などでは、ストリーム単位の field_order が
                ## FFprobe の結果に含まれないことがある。その場合は部分解析で取得した実フレームの
                ## interlaced_frame を参照し、少なくとも1枚でもインターレースなら Interlaced とする。
                field_order = (video_stream.field_order or '').lower()
                if field_order == 'progressive':
                    video_scan_type = 'Progressive'
                elif field_order in ['tt', 'bb', 'tb', 'bt']:
                    video_scan_type = 'Interlaced'
                elif any(frame.interlaced_frame == 1 for frame in sample_probe.frames):
                    video_scan_type = 'Interlaced'
                elif any(frame.interlaced_frame == 0 for frame in sample_probe.frames):
                    video_scan_type = 'Progressive'
                else:
                    # ストリーム・フレームのどちらからも判定できない場合は、誤ってインターレース映像を
                    # Progressive と扱わないよう、従来どおり保守的に Interlaced とする。
                    video_scan_type = 'Interlaced'
                ## ここで Progressive になっているが、全体解析側で field_order が明示的にインターレースを
                ## 示している場合は Interlaced とする。field_order の欠落は反証には使わない。
                ## (部分解析のみ、稀に本来インターレースにもかかわらずプログレッシブ映像と判定される場合があるため)
                full_probe_field_order = full_probe_video_streams[0].field_order if full_probe_video_streams else None
                if (video_scan_type == 'Progressive' and full_probe_field_order is not None and
                    full_probe_field_order.lower() in ['tt', 'bb', 'tb', 'bt']):
                    video_scan_type = 'Interlaced'
                ## フレームレート
                video_frame_rate = ParseFPS(video_stream.avg_frame_rate) or ParseFPS(video_stream.r_frame_rate)
                ## 解像度
                video_resolution_width = video_stream.width
                video_resolution_height = video_stream.height
                ## 映像トラックの解析が完了したことをマーク
                is_video_track_analyzed = True

        # TS は途中で PMT / PID が変化し得るため全体 probe の和集合を使う。
        # 非 TS はコンテナに宣言されたトラックが全時間有効なので部分 probe で十分。
        if container_format == 'MPEG-TS':
            audio_source_streams = self.__mergeTSAudioStreams(
                full_probe_audio_streams,
                sample_probe_audio_streams,
            )
        else:
            audio_source_streams = sample_probe_audio_streams
        for audio_stream in audio_source_streams:
            audio_codec = GetAudioCodec(audio_stream.codec_name, audio_stream.profile)
            audio_channel = GetAudioChannelLabel(audio_stream.channels)
            audio_sampling_rate = int(audio_stream.sample_rate)
            try:
                audio_pid = int(str(audio_stream.id), 0) if audio_stream.id is not None else None
            except ValueError:
                audio_pid = None
            audio_track: schemas.AudioTrackTimelineTrack = {
                'index': len(audio_tracks) + 1,
                'stream_index': audio_stream.index,
                'codec': audio_codec,
                'channel': audio_channel,
                'channel_layout': audio_stream.channel_layout,
                'sampling_rate': audio_sampling_rate,
                'language': audio_stream.tags.get('language'),
                'title': audio_stream.tags.get('title'),
                'is_dual_mono': False,
            }
            if audio_pid is not None:
                audio_track['pid'] = audio_pid
            audio_tracks.append(audio_track)

            ## 主音声情報
            if is_primary_audio_track_analyzed is False:
                primary_audio_codec = audio_codec
                primary_audio_channel = audio_channel
                primary_audio_sampling_rate = audio_sampling_rate
                is_primary_audio_track_analyzed = True

            ## 副音声情報（存在する場合）
            elif is_secondary_audio_track_analyzed is False:
                secondary_audio_codec = audio_codec
                secondary_audio_channel = audio_channel
                secondary_audio_sampling_rate = audio_sampling_rate
                is_secondary_audio_track_analyzed = True

        for subtitle_stream in full_probe.getSubtitleStreams():
            if selected_program_stream_indices is not None and subtitle_stream.index not in selected_program_stream_indices:
                continue
            subtitle_track: schemas.SubtitleTrack = {
                'index': len(subtitle_tracks) + 1,
                'stream_index': subtitle_stream.index,
                'codec': subtitle_stream.codec_name,
                'language': subtitle_stream.tags.get('language'),
                'title': subtitle_stream.tags.get('title'),
            }
            try:
                subtitle_pid = int(str(subtitle_stream.id), 0) if subtitle_stream.id is not None else None
            except ValueError:
                subtitle_pid = None
            if subtitle_pid is not None:
                subtitle_track['pid'] = subtitle_pid
            subtitle_tracks.append(subtitle_track)

        # FFmpeg 8でも放送TSのARIB字幕がbin_dataとしてprobeされる場合がある。
        # PMT descriptorで字幕と確認できたPIDだけを採用し、データ放送は登録しない。
        if container_format == 'MPEG-TS':
            try:
                arib_caption_pids = TSKeyFrameSeeker.findARIBCaptionPIDs(self.recorded_file_path)
            except (OSError, ValueError):
                arib_caption_pids = set()
            known_subtitle_stream_indexes = {track['stream_index'] for track in subtitle_tracks}
            for data_stream in full_probe.streams:
                if not isinstance(data_stream, FFprobeOtherStream) or data_stream.codec_type != 'data':
                    continue
                if data_stream.index in known_subtitle_stream_indexes:
                    continue
                if selected_program_stream_indices is not None and data_stream.index not in selected_program_stream_indices:
                    continue
                try:
                    data_pid = int(str(data_stream.id), 0) if data_stream.id is not None else None
                except ValueError:
                    data_pid = None
                if data_pid is None or data_pid not in arib_caption_pids:
                    continue
                subtitle_tracks.append({
                    'index': len(subtitle_tracks) + 1,
                    'stream_index': data_stream.index,
                    'codec': 'arib_caption',
                    'language': data_stream.tags.get('language'),
                    'title': data_stream.tags.get('title'),
                    'pid': data_pid,
                })

        has_video = video_codec is not None
        has_audio = primary_audio_codec is not None
        if has_video is False and has_audio is False:
            logging.warning(f'{self.recorded_file_path}: Video and audio tracks are missing or invalid. ignored.')
            return None

        # 必要な情報が全て揃っていることを保証
        assert duration is not None, 'duration not found.'
        assert container_format is not None, 'container_format not found.'

        ## 現状 ariblib は先頭が sync_byte でない or 途中で同期が壊れる (破損した TS パケットが存在する) TS ファイルを想定していないため、
        ## ariblib に入力する録画ファイルは必ず正常な TS ファイルである必要がある
        ## ファイルの末尾の TS パケットだけ破損してるだけなら再生できるのでファイルサイズはチェックせず、ファイルの先頭が sync_byte であるかだけチェックする
        ## ファイルの先頭1バイトだけを読み込んで sync_byte をチェックする
        if container_format == 'MPEG-TS':
            with self.recorded_file_path.open('rb') as f:
                if f.read(1)[0] != 0x47:
                    logging.warning(f'{self.recorded_file_path}: sync_byte is missing. ignored.')
                    return None

            has_video_stream_changes = self.__detectTSVideoStreamChanges(end_ts_offset) if has_video else False

        # ファイルハッシュを計算
        try:
            file_hash = self.__calculateFileHash(end_ts_offset)
        except ValueError:
            logging.warning(f'{self.recorded_file_path}: File size is too small. ignored.')
            return None

        # 録画ファイル情報を表すモデルを作成
        now = datetime.now(tz=JST)
        stat_info = self.recorded_file_path.stat()
        recorded_video = schemas.RecordedVideo(
            status = 'Recorded',  # この時点では録画済みとしておく
            file_path = str(self.recorded_file_path),
            file_hash = file_hash,
            file_size = stat_info.st_size,
            file_created_at = datetime.fromtimestamp(stat_info.st_ctime, tz=JST),
            file_modified_at = datetime.fromtimestamp(stat_info.st_mtime, tz=JST),
            recording_start_time = None,
            recording_end_time = None,
            duration = duration,
            container_format = container_format,
            has_video = has_video,
            has_audio = has_audio,
            video_codec = video_codec,
            video_codec_profile = video_codec_profile,
            video_scan_type = video_scan_type,
            video_frame_rate = video_frame_rate,
            video_resolution_width = video_resolution_width,
            video_resolution_height = video_resolution_height,
            has_video_stream_changes = has_video_stream_changes,
            primary_audio_codec = primary_audio_codec,
            primary_audio_channel = primary_audio_channel,
            primary_audio_sampling_rate = primary_audio_sampling_rate,
            secondary_audio_codec = secondary_audio_codec,
            secondary_audio_channel = secondary_audio_channel,
            secondary_audio_sampling_rate = secondary_audio_sampling_rate,
            audio_tracks = audio_tracks,
            audio_track_timeline = self.__buildAudioTrackTimeline(audio_source_streams, audio_tracks, duration),
            subtitle_tracks = subtitle_tracks,
            # 必須フィールドのため作成日時・更新日時は適当に現在時刻を入れている
            # この値は参照されず、DB の値は別途自動生成される
            created_at = now,
            updated_at = now,
        )

        recorded_program = None
        if container_format == 'MPEG-TS':
            # FFprobe の programs 配列から、実際にストリームが存在する service_id を特定する
            ## 複数サービスを含む TS ファイル (CS放送やマルチ編成) では、PAT に複数のサービスが含まれている場合がある
            ## FFprobe は実際のストリーム構成を解析するため、nb_streams > 0 かつ pcr_pid > 0 の program_id が
            ## 実際に放送されているサービスの service_id である可能性が高い
            preferred_service_id: int | None = None
            for program in full_probe.programs:
                # nb_streams > 0 かつ pcr_pid > 0 のプログラムを探す
                ## pcr_pid > 0 は実際に放送中のサービスを示す (pcr_pid == 0 は未使用のサブチャンネル)
                if (program.nb_streams is not None and program.nb_streams > 0 and
                    program.pcr_pid is not None and program.pcr_pid > 0 and
                    program.program_num is not None):
                    preferred_service_id = program.program_num
                    logging.debug(
                        f'{self.recorded_file_path}: Detected preferred service_id {preferred_service_id} from FFprobe '
                        f'(nb_streams: {program.nb_streams}, pcr_pid: {program.pcr_pid}).'
                    )
                    break

            # TS ファイルに含まれる番組情報・チャンネル情報を解析する
            analyzer = TSInfoAnalyzer(recorded_video, end_ts_offset=end_ts_offset, preferred_service_id=preferred_service_id)
            recorded_program = analyzer.analyze()  # 取得失敗時は None が返る
            if recorded_program is not None:
                logging.debug(f'{self.recorded_file_path}: MPEG-TS SDT/EIT analysis completed.')
                # 取得成功時は録画開始時刻と録画終了時刻も解析する
                recording_time = analyzer.analyzeRecordingTime()
                if recording_time is not None:
                    recorded_video.recording_start_time = recording_time[0]
                    recorded_video.recording_end_time = recording_time[1]
            else:
                # 取得失敗時、最終更新日時が現在時刻から30秒以内ならまだ録画中の可能性が高いので、None を返し DB には保存しない
                if (now - recorded_video.file_modified_at).total_seconds() < 30:
                    logging.warning(f'{self.recorded_file_path}: MPEG-TS SDT/EIT analysis failed. (still recording?)')
                    return None
        else:
            # 何らかのメタ情報から番組情報・チャンネル情報を解析する
            analyzer = TSInfoAnalyzer(recorded_video)
            recorded_program = analyzer.analyze()  # 取得失敗時は None が返る
            if recorded_program is not None:
                logging.debug(f'{self.recorded_file_path}: {container_format} Service/Event analysis completed.')
                # 取得成功時は録画開始時刻と録画終了時刻も解析する
                recording_time = analyzer.analyzeRecordingTime()
                if recording_time is not None:
                    recorded_video.recording_start_time = recording_time[0]
                    recorded_video.recording_end_time = recording_time[1]

        if recorded_program is not None:
            # TSInfoAnalyzer は RecordedProgram 構築後に EIT 音声記述子で self.recorded_video を補正するため、
            # Pydantic が構築時に複製した RecordedProgram 側へ最終結果を明示的に戻す。
            if ('デュアルモノ' in recorded_program.primary_audio_type and recorded_video.audio_tracks):
                recorded_video.audio_tracks[0]['is_dual_mono'] = True
                recorded_video.audio_tracks[0]['channel'] = 'Dual Mono'
                recorded_video.audio_tracks[0]['language'] = recorded_program.primary_audio_language
                recorded_video.primary_audio_channel = 'Dual Mono'
                for interval in recorded_video.audio_track_timeline:
                    for timeline_track in interval['tracks']:
                        if int(timeline_track['index']) == int(recorded_video.audio_tracks[0]['index']):
                            timeline_track['is_dual_mono'] = True
                            timeline_track['channel'] = 'Dual Mono'
                            timeline_track['language'] = recorded_program.primary_audio_language
            recorded_program.recorded_video = recorded_video

        # MPEG-TS 形式ではなくメタ情報も存在しなければ番組情報は取得できないので、ファイル名などから最低限の情報を設定する
        # MPEG-TS 形式だが TS ファイルからチャンネル情報・番組情報を取得できなかった場合も同様
        ## 他の値は RecordedProgram モデルで設定されたデフォルト値が自動的に入るので、タイトルと日時だけここで設定する
        if recorded_program is None:
            ## 録画開始時刻を取得できている場合は、それを番組開始時刻として使用する
            if recorded_video.recording_start_time is not None:
                recording_start_time = recorded_video.recording_start_time
            ## 録画開始時刻を取得できていない場合、ファイルの更新日時 - 動画長を番組開始時刻として使用する
            ## 作成日時だとコピー/移動した際にコピーが完了した日時に変更されることがあるため、更新日時ベースの方が適切
            ## 変更を加えていない録画 TS ファイルであれば、ファイルの更新日時は録画終了時刻に近い値になるはず
            else:
                recording_start_time = datetime.fromtimestamp(
                    self.recorded_file_path.stat().st_mtime,
                    tz = JST,
                ) - timedelta(seconds=recorded_video.duration)
            ## 拡張子を除いたファイル名をフォーマットした上でタイトルとして使用する
            title = TSInformation.formatString(self.recorded_file_path.stem)
            recorded_program = schemas.RecordedProgram(
                recorded_video = recorded_video,
                title = title,
                start_time = recording_start_time,
                end_time = recording_start_time + timedelta(seconds=recorded_video.duration),
                duration = recorded_video.duration,
                # 必須フィールドのため作成日時・更新日時は適当に現在時刻を入れている
                # この値は参照されず、DB の値は別途自動生成される
                created_at = datetime.now(tz=JST),
                updated_at = datetime.now(tz=JST),
            )

        # この時点で番組情報を正常に取得できており、かつ録画開始時刻・録画終了時刻の両方が取得できている場合
        elif (recorded_video.recording_start_time is not None and
            recorded_video.recording_end_time is not None):

            # 録画マージン (開始/終了) を算出
            ## 取得できなかった場合はデフォルト値として 0 が自動設定される
            ## 番組の途中から録画した/番組終了前に録画を中断したなどで録画マージンがマイナスになる場合も 0 が設定される
            recorded_program.recording_start_margin = \
                max((recorded_program.start_time - recorded_video.recording_start_time).total_seconds(), 0.0)
            recorded_program.recording_end_margin = \
                max((recorded_video.recording_end_time - recorded_program.end_time).total_seconds(), 0.0)

            # 番組開始時刻 < 録画開始時刻 or 録画終了時刻 < 番組終了時刻 の場合、部分的に録画されていることを示すフラグを立てる
            ## 番組全編を録画するには、録画開始時刻が番組開始時刻よりも前で、録画終了時刻が番組終了時刻よりも後である必要がある
            if (recorded_program.start_time < recorded_video.recording_start_time or
                recorded_video.recording_end_time < recorded_program.end_time):
                recorded_program.is_partially_recorded = True
            else:
                recorded_program.is_partially_recorded = False

        return recorded_program


    @staticmethod
    def __mergeTSAudioStreams(
        full_probe_streams: list[FFprobeAudioStream],
        sample_probe_streams: list[FFprobeAudioStream],
    ) -> list[FFprobeAudioStream]:
        """全体probeと部分probeのTS音声をPID単位で統合する。

        Args:
            full_probe_streams: 録画先頭から得た音声ストリーム。
            sample_probe_streams: 録画25%位置の切り出しから得た音声ストリーム。

        Returns:
            同一PIDを重複させず、実音声情報が豊富な方を採用したストリーム一覧。
        """

        merged_streams: dict[tuple[str, int], FFprobeAudioStream] = {}
        stream_scores: dict[tuple[str, int], tuple[bool, bool, bool, bool]] = {}
        for is_sample, streams in ((False, full_probe_streams), (True, sample_probe_streams)):
            for stream in streams:
                try:
                    pid = int(str(stream.id), 0) if stream.id is not None else None
                except ValueError:
                    pid = None
                # TSのstream indexは切り出したサンプル内で振り直される。同一PIDが取得できる場合は
                # indexが異なっても同じelementary streamとして扱い、Trackの二重登録を防ぐ。
                stream_key = ('pid', pid) if pid is not None else ('index', stream.index)
                score = (
                    stream.channels > 0,
                    stream.channel_layout is not None,
                    bool(stream.tags.get('language')),
                    is_sample,
                )
                if stream_key not in merged_streams or score > stream_scores[stream_key]:
                    merged_streams[stream_key] = stream
                    stream_scores[stream_key] = score
        return list(merged_streams.values())


    def __buildAudioTrackTimeline(
        self,
        streams: list[FFprobeAudioStream],
        tracks: list[schemas.AudioTrack],
        duration: float,
    ) -> list[schemas.AudioTrackTimelineEntry]:
        """FFprobe が列挙したストリームの有効期間から音声構成タイムラインを組み立てる。"""

        if not tracks:
            return [{'start_time': 0.0, 'end_time': duration, 'tracks': []}]
        if self.recorded_file_path.suffix.lower() in ['.ts', '.mts', '.m2ts']:
            pid_tracks = {
                pid: cast(schemas.AudioTrackTimelineTrack, track)
                for track in tracks if (pid := track.get('pid')) is not None
            }
            if pid_tracks:
                first_packet: dict[int, int | None] = {pid: None for pid in pid_tracks}
                last_packet: dict[int, int | None] = {pid: None for pid in pid_tracks}
                packet_index = 0
                with self.recorded_file_path.open('rb') as file:
                    while packet := file.read(ts.PACKET_SIZE):
                        if len(packet) < ts.PACKET_SIZE:
                            break
                        if packet[0] != 0x47:
                            packet_index += 1
                            continue
                        packet_pid = ((packet[1] & 0x1F) << 8) | packet[2]
                        if packet_pid in pid_tracks:
                            if first_packet[packet_pid] is None:
                                first_packet[packet_pid] = packet_index
                            last_packet[packet_pid] = packet_index
                        packet_index += 1
                if packet_index > 0:
                    active_ranges: list[tuple[float, float, schemas.AudioTrackTimelineTrack]] = []
                    boundaries = {0.0, duration}
                    for pid, track in pid_tracks.items():
                        first_packet_index = first_packet[pid]
                        last_packet_index = last_packet[pid]
                        if first_packet_index is None or last_packet_index is None:
                            continue
                        start = duration * first_packet_index / packet_index
                        end = min(duration, duration * (last_packet_index + 1) / packet_index)
                        # TSパケット位置換算の微小誤差で先頭/末尾に隙間を作らない。
                        if start < 0.5:
                            start = 0.0
                        if duration - end < 0.5:
                            end = duration
                        active_ranges.append((start, end, track))
                        boundaries.update((start, end))
                    if active_ranges:
                        timeline: list[schemas.AudioTrackTimelineEntry] = []
                        ordered_boundaries = sorted(boundaries)
                        for index, start in enumerate(ordered_boundaries[:-1]):
                            end = ordered_boundaries[index + 1]
                            active_tracks = [track for track_start, track_end, track in active_ranges
                                             if track_start <= start < track_end]
                            if timeline and timeline[-1]['tracks'] == active_tracks:
                                timeline[-1]['end_time'] = end
                            else:
                                timeline.append({'start_time': start, 'end_time': end, 'tracks': active_tracks})
                        return timeline

        # MP4/MKV 等のトラックは全時間有効。PIDを取得できないTSはstart_time/durationへフォールバックする。
        if len(streams) != len(tracks) or all(stream.start_time is None for stream in streams):
            return [{'start_time': 0.0, 'end_time': duration, 'tracks': cast(list[schemas.AudioTrackTimelineTrack], tracks)}]

        source_base = min(stream.start_time or 0.0 for stream in streams)
        active_ranges: list[tuple[float, float, schemas.AudioTrackTimelineTrack]] = []
        boundaries = {0.0, duration}
        for stream, track in zip(streams, tracks, strict=True):
            start = max(0.0, min(duration, (stream.start_time or source_base) - source_base))
            end = min(duration, start + stream.duration) if stream.duration is not None else duration
            end = max(start, end)
            typed_track = cast(schemas.AudioTrackTimelineTrack, track)
            active_ranges.append((start, end, typed_track))
            boundaries.update((start, end))

        timeline: list[schemas.AudioTrackTimelineEntry] = []
        ordered_boundaries = sorted(boundaries)
        for index, start in enumerate(ordered_boundaries[:-1]):
            end = ordered_boundaries[index + 1]
            active_tracks = [track for track_start, track_end, track in active_ranges
                             if track_start <= start < track_end]
            if timeline and timeline[-1]['tracks'] == active_tracks:
                timeline[-1]['end_time'] = end
            else:
                timeline.append({'start_time': start, 'end_time': end, 'tracks': active_tracks})
        return timeline


    def __calculateFileHash(self, end_ts_offset: int | None, chunk_size: int = 1024 * 1024, num_chunks: int = 3) -> str:
        """
        録画ファイルのハッシュを計算する
        録画ファイル全体をハッシュ化すると時間がかかるため、ファイルの複数箇所のみをハッシュ化する
        MPEG-TS 形式で `end_ts_offset` が指定されている場合は、末尾のゼロ埋め領域を含まない有効 TS データ領域のみを対象とする

        Args:
            end_ts_offset (int | None): 有効な TS データの終了位置 (バイト単位) (None の場合はファイルサイズ全体を対象とする)
            chunk_size (int, optional): チャンクのサイズ (デフォルト: 1MB)
            num_chunks (int, optional): チャンクの数 (デフォルト: 3)

        Raises:
            ValueError: 有効データ領域が小さく十分な数のチャンクが取得できない場合

        Returns:
            str: 録画ファイルのハッシュ
        """

        # 実際のファイルサイズを取得する
        file_size = self.recorded_file_path.stat().st_size

        # end_ts_offset が有効な場合は、有効 TS データ領域のサイズとして優先的に利用する
        if end_ts_offset is not None and end_ts_offset > 0:
            effective_size = min(end_ts_offset, file_size)
        else:
            effective_size = file_size

        # 有効データ領域が `chunk_size * num_chunks` より小さい場合は十分な数のチャンクが取得できないため例外を発生させる
        if effective_size < chunk_size * num_chunks:
            raise ValueError(f'File size must be at least {chunk_size * num_chunks} bytes.')

        with self.recorded_file_path.open('rb') as file:

            # SHA-256 だとハッシュ化に時間がかかるため、高速化のために MD5 を使用する
            ## 録画ファイルのハッシュを取りたいだけなのでセキュリティの考慮は不要
            hash_obj = hashlib.md5(usedforsecurity=False)

            # 指定された数のチャンクを読み込み、ハッシュを計算する
            for chunk_index in range(num_chunks):

                # 現在のチャンクのバイトオフセットを計算する
                ## (num_chunks + 1) で分割した位置 (1/4, 2/4, 3/4 など) からチャンクを取得する
                offset = (effective_size // (num_chunks + 1)) * (chunk_index + 1)
                file.seek(offset)

                # チャンクを読み込み、ハッシュオブジェクトを更新する
                ## end_ts_offset が指定されている場合でも有効データ領域を超えないように読み込みサイズを制限する
                remaining = effective_size - offset
                if remaining <= 0:
                    break
                read_size = min(chunk_size, remaining)
                chunk = file.read(read_size)
                if not chunk:
                    break
                hash_obj.update(chunk)

        # ハッシュの16進数表現を返す
        return hash_obj.hexdigest()


    def __calculateTSFileDuration(self, search_block_size: int = 1024 * 1024) -> tuple[float, int] | None:
        """
        TS ファイル内の最初と最後の有効な PCR タイムスタンプから再生時間（秒）を算出する (written with o3-mini)
        FFprobe から再生時間を取得できなかった場合のフォールバックとして利用する
        録画ファイルは録画時にスパースファイル（ゼロ埋めされた領域を含む）となる可能性があるため、
        ファイル末尾はゼロ埋め領域を高速に検出し、実際にデータが存在する部分と区別している

        Args:
            search_block_size (int): PCR 抽出時に読み込むブロックサイズ (バイト単位). デフォルト: 1MB

        Returns:
            tuple[float, int] | None: (再生時間（秒）, 有効な TS データの終了位置) のタプル
                （抽出できなかった場合は None を返す）
        """

        try:
            # ファイルサイズを取得
            file_size = self.recorded_file_path.stat().st_size

            with self.recorded_file_path.open('rb') as f:
                # --- 先頭ブロックからの PCR 抽出 ---
                # 基本的にはファイル先頭の最初の TS パケットから PCR を取得する
                f.seek(0)
                head_data = f.read(search_block_size)
                # 先頭ブロックが TS 同期バイト (0x47) で始まっていない場合、1バイトずつ探索して
                # 最初の同期バイトの位置を特定する
                if head_data and head_data[0] != ts.SYNC_BYTE[0]:
                    logging.info('Head data is not aligned; searching for sync byte...')
                    corrected_offset = None
                    for idx in range(len(head_data)):
                        if head_data[idx] == ts.SYNC_BYTE[0]:
                            corrected_offset = idx
                            break
                    if corrected_offset is not None:
                        head_data = head_data[corrected_offset:]
                    else:
                        logging.error('Failed to find sync byte in head data.')
                        return None

                first_timestamp: float | None = None
                for i in range(0, len(head_data), ts.PACKET_SIZE):
                    packet = head_data[i : i + ts.PACKET_SIZE]
                    if len(packet) < ts.PACKET_SIZE:
                        break
                    if packet[0] != ts.SYNC_BYTE[0]:
                        continue
                    pcr_val = ts.pcr(packet)
                    if pcr_val is not None:
                        # PCR 値を ts.HZ (90000Hz) で割り、秒単位に変換する
                        first_timestamp = pcr_val / ts.HZ
                        break

                if first_timestamp is None:
                    logging.error('Failed to extract first PCR timestamp.')
                    return None

                # --- 末尾のゼロ埋め領域の境界をバイナリサーチで検出 ---
                # TS ファイルは録画後にゼロ埋め領域が存在する場合があるため、
                # 正常なデータが存在する最後のオフセット (valid_data_end) を求める
                block_check_size = 4096  # ゼロ埋め判定用の小ブロックサイズ (4KB)
                low = 0
                high = file_size
                zero_boundary = file_size  # もしゼロブロックが見つからなければ有効データはファイル全体とする

                while low <= high:
                    mid = (low + high) // 2
                    f.seek(mid)
                    candidate = f.read(block_check_size)
                    if candidate and all(byte == 0 for byte in candidate):
                        # candidate が全て 0x00 ならば、ゼロ埋め領域の一部と見なし、境界を mid に更新
                        zero_boundary = mid
                        high = mid - 1
                    else:
                        low = mid + 1

                # valid_data_end はゼロ埋めが始まる境界、つまり有効データの終了位置
                valid_data_end = zero_boundary if zero_boundary < file_size else file_size

                # --- 末尾領域から最後の有効な PCR の取得 ---
                # 有効データ領域の終端から search_block_size 分の範囲を読み込み、TS パケット単位で同期を取る
                start_offset = valid_data_end - search_block_size
                if start_offset < 0:
                    start_offset = 0
                # TS パケット境界に合わせるため、start_offset を ts.PACKET_SIZE の倍数に補正
                start_offset = (start_offset // ts.PACKET_SIZE) * ts.PACKET_SIZE
                f.seek(start_offset)
                tail_chunk = f.read(valid_data_end - start_offset)

                # --- TS パケット同期の調整 ---
                # 読み込んだ tail_chunk の先頭が TS 同期バイト (0x47) でない場合、同期位置を調整する
                offset_in_chunk = 0
                if tail_chunk and tail_chunk[0] != ts.SYNC_BYTE[0]:
                    for idx in range(len(tail_chunk)):
                        if tail_chunk[idx] == ts.SYNC_BYTE[0]:
                            offset_in_chunk = idx
                            break

                # --- tail_chunk 内の TS パケットから有効な PCR 値を収集 ---
                valid_pcrs: list[float] = []
                for j in range(offset_in_chunk, len(tail_chunk) - ts.PACKET_SIZE + 1, ts.PACKET_SIZE):
                    packet = tail_chunk[j : j + ts.PACKET_SIZE]
                    if packet[0] != ts.SYNC_BYTE[0]:
                        continue
                    pcr_val = ts.pcr(packet)
                    if pcr_val is not None:
                        valid_pcrs.append(pcr_val / ts.HZ)

                if not valid_pcrs:
                    logging.error('Failed to extract last PCR in tail region.')
                    return None

                last_timestamp = valid_pcrs[-1]

                # --- PCR ラップアラウンドの補正 ---
                # もし末尾の PCR が先頭の PCR より小さい場合は、PCR のラップアラウンドが発生しているとみなし、
                # PCR の最大値に相当する秒数を加算する
                if last_timestamp < first_timestamp:
                    PCR_MAX_SECONDS = ts.PCR_CYCLE / ts.HZ
                    last_timestamp += PCR_MAX_SECONDS

                # --- 再生時間の算出 ---
                # 先頭と末尾の PCR 値から再生時間（秒）を算出する
                duration = last_timestamp - first_timestamp
                logging.debug(f'{self.recorded_file_path}: Duration calculated: {duration} seconds.')

                return (duration, valid_data_end)

        except Exception as ex:
            logging.error('Error in fallback duration calculation:', exc_info=ex)
            return None


    def __detectTSVideoStreamChanges(self, end_ts_offset: int | None) -> bool:
        """
        録画 TS 内で映像ストリーム構成が変化しているかを軽量に検出する

        Args:
            end_ts_offset (int | None): 有効な TS データの終了位置 (ゼロ埋め領域を除外する場合に指定)

        Returns:
            bool: 映像 PID または映像コーデックが途中で変化している場合は True
        """

        file_size = self.recorded_file_path.stat().st_size
        effective_size = min(end_ts_offset, file_size) if end_ts_offset is not None else file_size
        if effective_size < ts.PACKET_SIZE * 100:
            return False

        stream_infos: list[tuple[float, TSStreamInfo]] = []
        selected_sample_offsets: set[int] = set()
        for sample_ratio in (0.0, 0.25, 0.98):
            # 先頭・本編付近・末尾付近だけを見ることで、録画マージン由来の PID 切替を低コストで拾う
            ## 正常録画では PMT が同一のため、追加 I/O は数 MB の局所読み取りだけで終わる
            ## 末尾側は割合指定だと小さめの録画で探索範囲が短くなるため、最後の数 MB を固定で読む
            if sample_ratio == 0.98:
                sample_offset = ClosestMultiple(
                    max(effective_size - self.MAX_STREAM_SCAN_BYTES, 0),
                    ts.PACKET_SIZE,
                )
            else:
                sample_offset = ClosestMultiple(int(effective_size * sample_ratio), ts.PACKET_SIZE)
            if sample_offset > effective_size - ts.PACKET_SIZE:
                sample_offset = ClosestMultiple(max(effective_size - ts.PACKET_SIZE, 0), ts.PACKET_SIZE)
            if sample_offset > effective_size - ts.PACKET_SIZE:
                sample_offset = max(sample_offset - ts.PACKET_SIZE, 0)

            # 小さな録画では先頭サンプルだけでファイル全体を読めるため、末尾サンプルが同じ offset になることがある
            ## 同じ位置を重複して読むと検出力は増えず、判定材料の件数だけが水増しされるのでスキップする
            if sample_offset in selected_sample_offsets:
                continue
            selected_sample_offsets.add(sample_offset)

            try:
                stream_info = TSKeyFrameSeeker.findStreamInfo(
                    self.recorded_file_path,
                    start_offset = sample_offset,
                    max_scan_bytes = self.MAX_STREAM_SCAN_BYTES,
                )
            except Exception as ex:
                # PMT がサンプル範囲で拾えない TS でもメタデータ解析自体は継続する
                ## 検出不能な位置は判定材料から外し、取れた位置だけで保守的に判断する
                logging.warning(
                    f'{self.recorded_file_path}: Failed to inspect TS video stream info. '
                    f'[sample_ratio: {sample_ratio}, sample_offset: {sample_offset}]',
                    exc_info=ex,
                )
                continue

            stream_infos.append((sample_ratio, stream_info))

        if len(stream_infos) < 2:
            return False

        base_stream_info = stream_infos[0][1]
        for sample_ratio, stream_info in stream_infos[1:]:
            # 映像 PID や映像ストリーム構成が途中で変わる録画（マルチ編成開始/終了での解像度変更時など）に関して、
            # HWEncC 系エンコーダーは --avhw だと録画マージン区間 -> 本編での解像度切り替えに対応できずクラッシュし、
            # --avsw の場合はエラーこそ出ないがデコードがめちゃくちゃになる問題がある
            ## 録画ファイル自体の代表解像度は 25% 位置の FFprobe サンプルから取得済みなので、ここではエンコーダー選択用のフラグのみ決める
            ## この関数の戻り値が RecordedVideo.has_video_stream_changes に反映され、True の場合は再生時エンコーダーが FFmpeg に固定される
            if (
                stream_info.video_pid != base_stream_info.video_pid or
                stream_info.codec != base_stream_info.codec
            ):
                logging.info(
                    f'{self.recorded_file_path}: Video stream changes were detected. '
                    f'[base_video_pid: {base_stream_info.video_pid:#x}, base_codec: {base_stream_info.codec}, '
                    f'sample_ratio: {sample_ratio}, video_pid: {stream_info.video_pid:#x}, codec: {stream_info.codec}]'
                )
                return True

        return False


    def __analyzeFFprobe(self) -> tuple[FFprobeResult, FFprobeSampleResult, int | None] | None:
        """
        録画ファイルのメディア情報を FFprobe を使って解析する
        全体解析と部分解析の2段階で解析を行う
        全体解析では全般情報（コンテナ情報、録画時間など）を、部分解析では映像・音声情報を取得する

        Returns:
            tuple[FFprobeResult, FFprobeSampleResult, int | None] | None: 全体解析と部分解析の結果、有効な TS データの終了位置のタプル
                (KonomiTV で再生可能なファイルではない場合は None が返される)
        """

        # 全体解析: 録画ファイル全体のメディア情報を取得する
        args_full = [
            '-hide_banner',
            '-loglevel', 'error',
            '-analyzeduration', self.FFPROBE_ANALYZE_DURATION_US,
            '-probesize', self.FFPROBE_PROBESIZE,
            '-show_format',
            '-show_streams',
            '-show_programs',
            '-of', 'json',
            str(self.recorded_file_path),
        ]
        full_json = self.__runFFprobe(args_full)
        if full_json is None:
            return None

        try:
            full_probe = FFprobeResult(**full_json)
        except Exception as ex:
            logging.warning(f'{self.recorded_file_path}: Failed to parse full ffprobe result:', exc_info=ex)
            return None

        # FFprobe から再生時間を取得できない場合のフォールバック処理
        duration_result: tuple[float, int] | None = None
        if full_probe.format.duration is None and full_probe.format.format_name == 'mpegts':
            # MPEG-TS 形式の場合のみ、フォールバックとして自前で長さを算出する
            duration_result = self.__calculateTSFileDuration()
            if duration_result is not None:
                duration, _ = duration_result
                # FFprobe の結果を更新
                full_probe.format.duration = str(duration)
                logging.debug(f'{self.recorded_file_path}: Duration fallback completed: {duration} seconds.')
            else:
                # 再生時間を算出できなかった (ファイルが破損しているなど)
                logging.warning(f'{self.recorded_file_path}: Duration is missing and fallback failed.')
                return None

        # 部分解析: 録画ファイルの25%位置から30秒程度のデータを取得し、メディア情報を解析する
        sample_json: dict[str, Any] | None = None
        sample_data = b''
        sample_size = ClosestMultiple(18 * 1024 * 1024 * 30 // 8, ts.PACKET_SIZE)
        if full_probe.format.bit_rate is not None:
            try:
                # Use roughly 30 seconds of the actual TS bitrate. BS8K is around 80Mbps,
                # so the old fixed 18Mbps sample often covered only a few seconds.
                actual_sample_size = int(int(full_probe.format.bit_rate) * 30 // 8)
                sample_size = ClosestMultiple(max(sample_size, actual_sample_size), ts.PACKET_SIZE)
                sample_size = min(sample_size, ClosestMultiple(512 * 1024 * 1024, ts.PACKET_SIZE))
            except ValueError:
                pass
        try:
            # MPEG-TS 形式の場合のみ実行
            if 'mpegts' in (full_probe.format.format_name or '').lower():
                # ファイルを開く
                with open(self.recorded_file_path, 'rb') as f:
                    # 25%位置にシーク (TS パケットサイズに合わせて切り出す)
                    file_size = self.recorded_file_path.stat().st_size
                    offset = ClosestMultiple(int(file_size * 0.25), ts.PACKET_SIZE)
                    f.seek(offset)
                    # 30秒程度のデータを読み込む
                    ## サンプルとして FFprobe に渡すデータが30秒より短いと正確に解析できないことがある
                    sample_data = f.read(sample_size)
                    # サンプルデータが全てゼロ埋めされているかチェック
                    if sample_data and all(byte == 0 for byte in sample_data):
                        # ゼロ埋め領域の境界を取得するため calculateTSFileDuration を実行
                        duration_result = self.__calculateTSFileDuration()
                        if duration_result is None:
                            logging.warning(f'{self.recorded_file_path}: Failed to calculate duration.')
                            return None
                        # ゼロ埋め領域を除いた有効データ範囲から再度サンプルを取得
                        # 有効データ範囲の25%位置にシーク
                        _, end_ts_offset = duration_result
                        offset = ClosestMultiple(int(end_ts_offset * 0.25), ts.PACKET_SIZE)
                        f.seek(offset)
                        # 30秒程度のデータを読み込む (ビットレートを 18Mbps と仮定)
                        sample_size = min(sample_size, end_ts_offset - offset)  # 有効データ範囲を超えないようにする
                        sample_data = f.read(sample_size)

                    # 録画ファイルから切り出したサンプルを標準入力から FFprobe に渡して解析
                    args_sample = [
                        '-hide_banner',
                        '-loglevel', 'error',
                        '-analyzeduration', self.FFPROBE_ANALYZE_DURATION_US,
                        '-probesize', self.FFPROBE_PROBESIZE,
                        '-f', 'mpegts',
                        '-i', 'pipe:0',
                        '-show_streams',
                        '-of', 'json',
                    ]
                    sample_json = self.__runFFprobe(args_sample, input_bytes=sample_data)
            else:
                # MPEG-TS 形式でない場合は部分解析はせず、全体解析の結果をそのまま設定
                sample_json = full_json
        except Exception as ex:
            logging.warning(f'{self.recorded_file_path}: Failed to analyze sample via ffprobe:', exc_info=ex)
            return None

        if sample_json is None:
            return None

        # 部分解析の映像ストリームに field_order が含まれない場合のみ、同じサンプルの先頭2秒程度から
        # 実フレームの interlaced_frame を取得する。通常ケースでは追加解析を行わず、録画スキャンの負荷を抑える。
        ## HEVC では Progressive 映像でも field_order 自体が省略されることがあり、欠落を Interlaced とみなすと
        ## インターレース解除済み録画が一律で誤判定されるため、フレーム単位の情報をフォールバックに用いる。
        sample_video_stream = next(
            (stream for stream in sample_json.get('streams', []) if stream.get('codec_type') == 'video'),
            None,
        )
        if (sample_video_stream is not None and sample_video_stream.get('field_order') is None and
            full_probe.format.format_name == 'mpegts'):
            args_frames = [
                '-hide_banner',
                '-loglevel', 'error',
                '-analyzeduration', self.FFPROBE_ANALYZE_DURATION_US,
                '-probesize', self.FFPROBE_PROBESIZE,
                '-f', 'mpegts',
                '-i', 'pipe:0',
                '-select_streams', 'v:0',
                '-read_intervals', '%+2',
                '-show_frames',
                '-show_entries', 'frame=interlaced_frame',
                '-of', 'json',
            ]
            frames_json = self.__runFFprobe(args_frames, input_bytes=sample_data)
            if frames_json is not None:
                sample_json['frames'] = frames_json.get('frames', [])

        try:
            sample_probe = FFprobeSampleResult(**sample_json)
        except Exception as ex:
            logging.warning(f'{self.recorded_file_path}: Failed to parse sample ffprobe result:', exc_info=ex)
            return None

        # 解析処理中に calculateTSFileDuration() を実行した場合は、有効な TS データの終了位置も一緒に返す
        ## calculateTSFileDuration() が実行されている時点で、当該録画ファイルの後半部分にゼロ埋めデータが存在することを示す
        ## この値は TSInfoAnalyzer で番組情報を解析する際に参照される
        if duration_result is not None:
            _, end_ts_offset = duration_result
            return (full_probe, sample_probe, end_ts_offset)
        else:
            return (full_probe, sample_probe, None)


    def __runFFprobe(self, args: list[str], input_bytes: bytes | None = None) -> dict[str, Any] | None:
        """
        FFprobe を実行する

        Args:
            args (list[str]): FFprobe の引数
            input_bytes (bytes | None): FFprobe に渡すデータ

        Returns:
            dict[str, Any] | None: FFprobe の結果
        """

        try:
            cmd = [LIBRARY_PATH['FFprobe'], *args]
            if input_bytes is None:
                proc = subprocess.run(cmd, capture_output=True)
            else:
                proc = subprocess.run(cmd, input=input_bytes, capture_output=True)
            if proc.returncode != 0:
                logging.warning(f'{self.recorded_file_path}: ffprobe failed with return code {proc.returncode}: {proc.stderr.decode("utf-8", errors="ignore").strip()}')
                return None
            return cast(dict[str, Any], json.loads(proc.stdout.decode('utf-8')))
        except Exception as ex:
            logging.warning(f'{self.recorded_file_path}: Failed to run ffprobe:', exc_info=ex)
            return None


if __name__ == '__main__':
    # デバッグ用: 録画ファイルのパスを引数に取り、そのファイルのメタデータを解析する
    # Usage: poetry run python -m app.metadata.MetadataAnalyzer /path/to/recorded_file.ts
    def main(recorded_file_path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, readable=True, resolve_path=True)):
        LoadConfig(bypass_validation=True)  # 一度実行しておかないと設定値を参照できない
        metadata_analyzer = MetadataAnalyzer(recorded_file_path)
        result = metadata_analyzer.analyze()
        if result is not None:
            print(result)
        else:
            logging.error('Not a KonomiTV playable TS file.')
    typer.run(main)
