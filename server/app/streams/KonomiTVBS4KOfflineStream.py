from __future__ import annotations

import re
import struct
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from app import logging, schemas
from app.streams.RecordedFMP4Stream import RecordedFMP4Segment, RecordedFMP4Stream


@dataclass(frozen=True, slots=True)
class KonomiTVBS4KOfflineAsset:
    """オフライン保存応答に格納する1ファイルを表す。"""

    path: str
    media_type: str
    data: bytes


class KonomiTVBS4KOfflineStream:
    """RecordedFMP4Stream の生成物を自己完結したオフライン保存応答へ変換する。"""

    MAGIC = b'KTVBS4KODL1\n'
    MAX_PATH_BYTES = 1024
    MAX_MEDIA_TYPE_BYTES = 255
    MAX_ASSET_BYTES = 128 * 1024 * 1024
    TERMINATOR_PATH_LENGTH = 0xFFFF

    def __init__(
        self,
        video_stream: RecordedFMP4Stream,
        metadata: schemas.KonomiTVBS4KOfflineStreamMetadata,
    ) -> None:
        """オフライン保存ストリームを初期化する。

        Args:
            video_stream: 通常録画再生と生成条件・キャッシュを共有する fMP4 セッション。
            metadata: クライアントが保存ジョブとの一致を検証する応答メタデータ。

        Returns:
            None
        """

        # 全アセット生成と切断時 cleanup で参照する同一録画セッション。
        self.video_stream = video_stream
        # 応答冒頭へ一度だけ書き、録画ID・ファイルハッシュ・exact生成条件を固定するメタデータ。
        self.metadata = metadata

    @staticmethod
    def _ValidateAssetPath(path: str) -> None:
        """保存先から逸脱しない正規化済み相対パスか検査する。

        Args:
            path: CacheStorage の保存世代直下へ配置する相対パス。

        Returns:
            None
        """

        if (
            path == '' or
            path.startswith('/') or
            '\\' in path or
            '?' in path or
            '#' in path or
            any(part in ('', '.', '..') for part in path.split('/')) or
            re.fullmatch(r'[0-9A-Za-z._/-]+', path) is None
        ):
            raise ValueError(f'Invalid offline asset path: {path}')

    @classmethod
    def EncodeAsset(cls, asset: KonomiTVBS4KOfflineAsset) -> bytes:
        """1アセットを長さ付きバイナリレコードへ変換する。

        Args:
            asset: 相対パス・MIME・データを持つ保存アセット。

        Returns:
            bytes: `path length / MIME length / data length / payload` のレコード。
        """

        cls._ValidateAssetPath(asset.path)
        path = asset.path.encode('utf-8')
        media_type = asset.media_type.encode('ascii')
        if len(path) == 0 or len(path) > cls.MAX_PATH_BYTES:
            raise ValueError('Offline asset path length is invalid')
        if len(media_type) == 0 or len(media_type) > cls.MAX_MEDIA_TYPE_BYTES:
            raise ValueError('Offline asset media type length is invalid')
        if len(asset.data) == 0 or len(asset.data) > cls.MAX_ASSET_BYTES:
            raise ValueError('Offline asset data length is invalid')
        return struct.pack('>HHQ', len(path), len(media_type), len(asset.data)) + path + media_type + asset.data

    @classmethod
    def EncodeTerminator(cls, asset_count: int, total_asset_bytes: int) -> bytes:
        """応答末尾の件数・総バイト数レコードを返す。

        Args:
            asset_count: 直前までに書き出したアセット数。
            total_asset_bytes: アセットデータ本体の合計バイト数。

        Returns:
            bytes: 途中切断を検出する終端レコード。
        """

        return struct.pack('>HIQ', cls.TERMINATOR_PATH_LENGTH, asset_count, total_asset_bytes)

    @staticmethod
    def _GetQueryValue(uri: str, key: str) -> str:
        """オンライン HLS URI から必須 query 値を1つ取得する。

        Args:
            uri: RecordedFMP4Stream が生成した相対 URI。
            key: 取得する query 名。

        Returns:
            str: query の先頭値。
        """

        values = parse_qs(urlsplit(uri).query).get(key)
        if values is None or len(values) != 1:
            raise ValueError(f'Offline playlist URI has no {key}: {uri}')
        return values[0]

    @classmethod
    def BuildMasterPlaylist(cls, playlist: str) -> str:
        """オンライン master からセッション・字幕依存を除いた保存用 master を返す。

        Args:
            playlist: RecordedFMP4Stream が生成したオンライン master。

        Returns:
            str: 保存世代内の相対 URI だけを参照する master。
        """

        output: list[str] = []
        for line in playlist.splitlines():
            # 字幕は初版の保存対象外。variant の SUBTITLES 属性も同時に除去する。
            if line.startswith('#EXT-X-MEDIA:TYPE=SUBTITLES'):
                continue
            line = line.replace(',SUBTITLES="subtitles"', '')
            if line.startswith('#EXT-X-MEDIA:TYPE=AUDIO'):
                line = re.sub(
                    r'URI="audio/([^/]+)/playlist\?[^\"]+"',
                    r'URI="audio/\1/playlist.m3u8"',
                    line,
                )
            elif line.startswith('video/playlist?'):
                line = 'video/playlist.m3u8'
            elif line.startswith('audio/') and '/playlist?' in line:
                rendition_id = line.split('/')[1]
                line = f'audio/{rendition_id}/playlist.m3u8'
            output.append(line)
        return '\n'.join(output) + '\n'

    @classmethod
    def BuildVideoPlaylist(cls, playlist: str) -> str:
        """オンライン映像 playlist を保存用 init / fragment URI へ変換する。

        Args:
            playlist: RecordedFMP4Stream が生成した映像 media playlist。

        Returns:
            str: 保存世代内の映像アセットだけを参照する playlist。
        """

        output: list[str] = []
        for line in playlist.splitlines():
            if line.startswith('#EXT-X-MAP:URI="'):
                uri = line.removeprefix('#EXT-X-MAP:URI="').removesuffix('"')
                generation = cls._GetQueryValue(uri, 'generation')
                line = f'#EXT-X-MAP:URI="init/{generation}.mp4"'
            elif line.startswith('segment?'):
                sequence = cls._GetQueryValue(line, 'sequence')
                line = f'segments/{sequence}.m4s'
            output.append(line)
        return '\n'.join(output) + '\n'

    @classmethod
    def BuildAudioPlaylist(cls, playlist: str) -> str:
        """オンライン音声 playlist を保存用 init / fragment URI へ変換する。

        Args:
            playlist: RecordedFMP4Stream が生成した音声 media playlist。

        Returns:
            str: 保存世代内の音声アセットだけを参照する playlist。
        """

        output: list[str] = []
        for line in playlist.splitlines():
            if line.startswith('#EXT-X-MAP:URI="'):
                uri = line.removeprefix('#EXT-X-MAP:URI="').removesuffix('"')
                sequence = cls._GetQueryValue(uri, 'sequence')
                line = f'#EXT-X-MAP:URI="init/{sequence}.mp4"'
            elif line.startswith('segment?'):
                sequence = cls._GetQueryValue(line, 'sequence')
                line = f'segments/{sequence}.m4s'
            output.append(line)
        return '\n'.join(output) + '\n'

    @staticmethod
    def _GetVideoInitializationSegments(
        segments: tuple[RecordedFMP4Segment, ...],
    ) -> dict[int, RecordedFMP4Segment]:
        """映像 generation ごとの先頭セグメントを返す。

        Args:
            segments: 固定済みセグメント計画。

        Returns:
            dict[int, RecordedFMP4Segment]: generation から先頭セグメントへの対応。
        """

        result: dict[int, RecordedFMP4Segment] = {}
        for segment in segments:
            result.setdefault(segment.generation, segment)
        return result

    @staticmethod
    def _GetAudioInitializationSegments(
        segments: tuple[RecordedFMP4Segment, ...],
    ) -> tuple[RecordedFMP4Segment, ...]:
        """音声 playlist が MAP を更新する各構成境界の先頭セグメントを返す。

        Args:
            segments: 固定済みセグメント計画。

        Returns:
            tuple[RecordedFMP4Segment, ...]: 映像・音声 generation 境界の先頭セグメント。
        """

        result: list[RecordedFMP4Segment] = []
        previous_generation_key: tuple[int, int] | None = None
        for segment in segments:
            transcoded_audio_generation = segment.transcoded_audio_generation \
                if segment.transcoded_audio_generation is not None else segment.audio_generation
            generation_key = (segment.generation, transcoded_audio_generation)
            if generation_key != previous_generation_key:
                result.append(segment)
                previous_generation_key = generation_key
        return tuple(result)

    async def IterateAssets(self) -> AsyncGenerator[KonomiTVBS4KOfflineAsset]:
        """プレイリスト、init、fragment を再生に必要な順で生成する。

        Returns:
            AsyncGenerator[KonomiTVBS4KOfflineAsset]: 保存アセット列。
        """

        segments = self.video_stream.getSegments()
        if len(segments) == 0:
            raise RuntimeError('Offline stream has no segments')
        master = await self.video_stream.getMasterPlaylist('offline')
        yield KonomiTVBS4KOfflineAsset(
            'playlist.m3u8',
            'application/vnd.apple.mpegurl',
            self.BuildMasterPlaylist(master).encode('utf-8'),
        )

        has_video = self.video_stream.recorded_program.recorded_video.has_video
        if has_video is True:
            video_playlist = self.video_stream.getVideoPlaylist('offline')
            yield KonomiTVBS4KOfflineAsset(
                'video/playlist.m3u8',
                'application/vnd.apple.mpegurl',
                self.BuildVideoPlaylist(video_playlist).encode('utf-8'),
            )
            for generation, segment in self._GetVideoInitializationSegments(segments).items():
                init = await self.video_stream.getVideoInitSegment(generation, segment.sequence)
                if init is None or len(init) == 0:
                    raise RuntimeError(f'Offline video initialization segment failed: {generation}')
                yield KonomiTVBS4KOfflineAsset(f'video/init/{generation}.mp4', 'video/mp4', init)
            for segment in segments:
                data = await self.video_stream.getVideoSegment(segment.sequence)
                if data is None or len(data) == 0:
                    raise RuntimeError(f'Offline video segment failed: {segment.sequence}')
                yield KonomiTVBS4KOfflineAsset(
                    f'video/segments/{segment.sequence}.m4s',
                    'video/mp4',
                    data,
                )

        # 全レンディションを保存し、通常 HLS と同じ既定音声・切替候補をオフラインでも維持する。
        for rendition in self.video_stream.getAudioRenditions():
            audio_playlist = self.video_stream.getAudioPlaylist(rendition.id, 'offline')
            yield KonomiTVBS4KOfflineAsset(
                f'audio/{rendition.id}/playlist.m3u8',
                'application/vnd.apple.mpegurl',
                self.BuildAudioPlaylist(audio_playlist).encode('utf-8'),
            )
            for segment in self._GetAudioInitializationSegments(segments):
                init = await self.video_stream.getAudioInitSegment(rendition.id, segment.sequence)
                if init is None or len(init) == 0:
                    raise RuntimeError(
                        f'Offline audio initialization segment failed: {rendition.id}/{segment.sequence}'
                    )
                yield KonomiTVBS4KOfflineAsset(
                    f'audio/{rendition.id}/init/{segment.sequence}.mp4',
                    'audio/mp4',
                    init,
                )
            for segment in segments:
                data = await self.video_stream.getAudioSegment(rendition.id, segment.sequence)
                if data is None or len(data) == 0:
                    raise RuntimeError(f'Offline audio segment failed: {rendition.id}/{segment.sequence}')
                yield KonomiTVBS4KOfflineAsset(
                    f'audio/{rendition.id}/segments/{segment.sequence}.m4s',
                    'audio/mp4',
                    data,
                )

    async def Generate(self) -> AsyncGenerator[bytes]:
        """独自バイナリ形式の応答本体を生成する。

        Returns:
            AsyncGenerator[bytes]: StreamingResponse へ渡す応答チャンク。
        """

        metadata = self.metadata.model_dump_json().encode('utf-8')
        yield self.MAGIC
        yield struct.pack('>I', len(metadata))
        yield metadata

        asset_count = 0
        total_asset_bytes = 0
        async for asset in self.IterateAssets():
            record = self.EncodeAsset(asset)
            asset_count += 1
            total_asset_bytes += len(asset.data)
            yield record
        logging.info(
            '[KonomiTVBS4KOfflineStream] Offline stream generation completed. '
            f'[video_id: {self.metadata.video_id}, assets: {asset_count}, bytes: {total_asset_bytes}]'
        )
        yield self.EncodeTerminator(asset_count, total_asset_bytes)
