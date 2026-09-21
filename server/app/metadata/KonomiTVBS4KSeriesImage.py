"""TMDb / Bangumi のシリーズ表紙をローカル WebP として管理する。"""

from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import SplitResult, urlsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from app import logging
from app.constants import (
    KONOMITV_BS4K_SERIES_IMAGE_HTTPX_CLIENT,
    KONOMITV_BS4K_SERIES_IMAGES_DIR,
)
from app.models.Series import Series, TmdbMediaType


@dataclass(frozen=True, slots=True)
class _SeriesPosterCandidate:
    """1つの外部作品 ID に対応する表紙取得候補。"""

    source: Literal['Bangumi', 'Tmdb']
    external_id: int
    media_type: TmdbMediaType | None
    source_identifier: str
    source_url: str
    destination_path: Path

    @property
    def lock_key(self) -> tuple[str, str]:
        """
        同じ外部作品の取得を直列化するキーを返す。

        Returns:
            tuple[str, str]: 取得元と、作品種別を含む外部 ID。
        """

        identifier = (
            f'{self.media_type}:{self.external_id}'
            if self.media_type is not None
            else str(self.external_id)
        )
        return (self.source, identifier)


class KonomiTVBS4KSeriesImage:
    """外部表紙の取得、WebP 変換、原子的公開、孤児回収を担う。"""

    TMDB_IMAGE_ORIGIN = 'https://image.tmdb.org'
    BANGUMI_IMAGE_ORIGIN = 'https://lain.bgm.tv'
    MAX_POSTER_WIDTH = 500
    WEBP_QUALITY = 80
    MAX_SOURCE_BYTES = 20 * 1024 * 1024
    MAX_SOURCE_PIXELS = 40_000_000
    DOWNLOAD_CONCURRENCY = 4

    # 同じ外部作品 ID の GET が重なっても、ダウンロードと置換は1本だけ実行する。
    _download_locks: dict[tuple[str, str], asyncio.Lock] = {}
    # 異なる作品の同時取得も CDN と Pillow を圧迫しない数へ制限する。
    _download_semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
    # 公開世代を守る錠。ダウンロードロックとは別にし、invalidation が外部通信を待たないようにする。
    ## 作業スレッドとイベントループの双方から触るため、asyncio ではなく threading の錠を使う。
    _generation_lock = threading.Lock()
    # 外部作品キーごとの公開世代。invalidation のたびに加算し、古い世代の取得成果は公開しない。
    _poster_generations: dict[tuple[str, str], int] = {}

    @classmethod
    async def ensurePoster(cls, series: Series) -> bytes | None:
        """
        優先順にローカル表紙を確認し、欠損時だけ取得して内容を返す。

        Args:
            series (Series): 表紙の外部 ID とソース識別子を持つ Series。

        Returns:
            bytes | None: 公開できる WebP のバイト列。全候補を取得できない場合は None。
        """

        # Bangumi を常に第1候補にし、取得不能な場合だけ TMDb へフォールバックする。
        for candidate in cls._buildCandidates(series):
            poster_bytes = await cls._ensureCandidatePoster(candidate)
            if poster_bytes is not None:
                return poster_bytes
        return None

    @classmethod
    async def _ensureCandidatePoster(cls, candidate: _SeriesPosterCandidate) -> bytes | None:
        """
        同一キーの取得を直列化し、安定した表紙バイト列だけを返す。

        Args:
            candidate (_SeriesPosterCandidate): 許可済み CDN と保存先を含む候補。

        Returns:
            bytes | None: ロック保持中に読み取った WebP。取得不能時は None。
        """

        lock = cls._download_locks.setdefault(candidate.lock_key, asyncio.Lock())
        async with lock:
            start_generation = cls._currentGeneration(candidate.lock_key)
            # ロック待ちの間に先行リクエストが公開済みなら、外部通信を繰り返さない。
            ## 生成元識別子が現在 DB 値と一致する公開だけを信用し、陳腐化した残存は取り直す。
            stored_bytes = await asyncio.to_thread(cls._readVerifiedPoster, candidate)
            if stored_bytes is not None:
                return stored_bytes
            if await cls._downloadAndStore(candidate, start_generation) is False:
                return None
            # 公開直後の読み取りも同じロック内で行い、invalidation の unlink との競合を 404 に畳む。
            ## パスではなくバイト列を返すため、配信開始前の削除で 500 や旧画像応答にならない。
            return await asyncio.to_thread(cls._readOwnedFile, candidate.destination_path)

    @classmethod
    def _readVerifiedPoster(cls, candidate: _SeriesPosterCandidate) -> bytes | None:
        """
        生成元識別子が現在の候補と一致する公開だけを読み取る。

        Args:
            candidate (_SeriesPosterCandidate): 許可済み CDN と保存先を含む候補。

        Returns:
            bytes | None: 一致した WebP の内容。不一致・欠損・所有外は None。
        """

        if cls._ensureOwnedParent(candidate.destination_path, create=False) is False:
            # 所有外は欠損として扱う。運用者向けの警告は保存拒否側で出す。
            return None
        # 公開時に原子的に置いた生成元と候補の識別子を比べ、再起動後も陳腐化を検出する。
        try:
            stored_identifier = cls._posterSourcePath(candidate.destination_path).read_bytes()
        except OSError:
            return None
        if stored_identifier != candidate.source_identifier.encode('utf-8'):
            return None
        return cls._readOwnedFile(candidate.destination_path)

    @staticmethod
    def _posterSourcePath(destination_path: Path) -> Path:
        """
        公開 WebP と対になる生成元識別子の保存パスを返す。

        Args:
            destination_path (Path): 外部 ID 配下の `poster.webp` / `cover.webp`。

        Returns:
            Path: 同一ディレクトリの `poster.source` / `cover.source`。
        """

        return destination_path.with_name(f'{destination_path.stem}.source')

    @classmethod
    def _currentGeneration(cls, lock_key: tuple[str, str]) -> int:
        """
        外部作品キーの現在の公開世代を返す。

        Args:
            lock_key (tuple[str, str]): 取得元と外部 ID から作ったキー。

        Returns:
            int: invalidation 回数。未登録は 0。
        """

        with cls._generation_lock:
            return cls._poster_generations.get(lock_key, 0)

    @classmethod
    async def invalidateTmdbPoster(cls, media_type: TmdbMediaType, tmdb_id: int) -> None:
        """
        TMDb のソース識別子が変わった作品のローカル表紙を破棄する。

        Args:
            media_type (TmdbMediaType): TMDb の作品種別。
            tmdb_id (int): TMDb 作品 ID。

        Returns:
            None
        """

        lock_key = ('Tmdb', f'{media_type}:{tmdb_id}')
        destination_path = cls._tmdbPosterPath(media_type, tmdb_id)
        # ダウンロードロックは待たず、世代加算と unlink だけを速やかに行う。
        ## 照合パイプラインを外部ダウンロードの完了待ちに載せないための分離である。
        ## unlink の失敗は警告に畳み、GET 時の生成元照合に回復を委ねる。
        with cls._generation_lock:
            cls._poster_generations[lock_key] = cls._poster_generations.get(lock_key, 0) + 1
            cls._unlinkValidatedFiles(destination_path, source='Tmdb', external_id=tmdb_id)

    @classmethod
    async def invalidateBangumiPoster(cls, subject_id: int) -> None:
        """
        Bangumi のソース識別子が変わった条目のローカル表紙を破棄する。

        Args:
            subject_id (int): Bangumi 条目 ID。

        Returns:
            None
        """

        lock_key = ('Bangumi', str(subject_id))
        destination_path = cls._bangumiPosterPath(subject_id)
        # ダウンロードロックは待たず、世代加算と unlink だけを速やかに行う。
        ## 照合パイプラインを外部ダウンロードの完了待ちに載せないための分離である。
        ## unlink の失敗は警告に畳み、GET 時の生成元照合に回復を委ねる。
        with cls._generation_lock:
            cls._poster_generations[lock_key] = cls._poster_generations.get(lock_key, 0) + 1
            cls._unlinkValidatedFiles(destination_path, source='Bangumi', external_id=subject_id)

    @classmethod
    def _unlinkValidatedFiles(cls, destination_path: Path, source: str, external_id: int) -> None:
        """
        所有連鎖を検証した上で公開 WebP と生成元識別子を削除する。

        Args:
            destination_path (Path): 外部 ID 配下の `poster.webp` / `cover.webp`。
            source (str): ログ用の取得元名。
            external_id (int): ログ用の外部作品 ID。

        Returns:
            None
        """

        # symlink を含む親経路では参照先ファイルへ触れない。
        if cls._isOwnedDirectory(destination_path.parent, create=False) is False:
            logging.warning(
                f'[KonomiTVBS4KSeriesImage] Refused to delete outside the owned directory. '
                f'[source: {source}, external_id: {external_id}]',
            )
            return
        try:
            destination_path.unlink(missing_ok=True)
            cls._posterSourcePath(destination_path).unlink(missing_ok=True)
        except OSError as ex:
            # 削除失敗は次回 GET の生成元照合で検出して再取得するため、呼び出し元へ伝播させない。
            logging.warning(
                f'[KonomiTVBS4KSeriesImage] Failed to delete series poster. '
                f'[source: {source}, external_id: {external_id}, error: {type(ex).__name__}]',
            )

    @classmethod
    async def cleanupOrphanedImages(cls) -> None:
        """
        DB に存在しない外部作品 ID の画像ディレクトリを起動時に回収する。

        Returns:
            None
        """

        tmdb_rows = await Series.filter(
            tmdb_id__not_isnull=True,
            tmdb_media_type__not_isnull=True,
        ).values('tmdb_id', 'tmdb_media_type')
        bangumi_rows = await Series.filter(
            bangumi_subject_id__not_isnull=True,
        ).values('bangumi_subject_id')
        tmdb_keys = {
            (str(row['tmdb_media_type']), int(row['tmdb_id']))
            for row in tmdb_rows
        }
        removed_count = await asyncio.to_thread(
            cls._cleanupOrphanedDirectories,
            tmdb_keys,
            {int(row['bangumi_subject_id']) for row in bangumi_rows},
        )
        if removed_count > 0:
            logging.info(
                f'[KonomiTVBS4KSeriesImage] Removed orphaned series image directories. '
                f'[removed: {removed_count}]',
            )

    @staticmethod
    def normalizeBangumiImageIdentifier(raw_url: str) -> str | None:
        """
        Bangumi API の画像 URL から、ホストに依存しない画像パスだけを取り出す。

        Args:
            raw_url (str): Bangumi API が返した表紙 URL。

        Returns:
            str | None: `/pic/cover/` 以下の画像パス。不正な URL は None。
        """

        parsed_url = KonomiTVBS4KSeriesImage._splitSourceURL(raw_url)
        if parsed_url is None:
            return None
        try:
            source_port = parsed_url.port
        except ValueError:
            # 非数値 port は SplitResult.port が ValueError を送出するため、不正 URL として拒否する。
            return None
        if (
            parsed_url.scheme not in {'http', 'https'}
            or parsed_url.hostname != 'lain.bgm.tv'
            or parsed_url.username is not None
            or parsed_url.password is not None
            or source_port not in {None, 80, 443}
            or parsed_url.path.startswith('/pic/cover/') is False
            or KonomiTVBS4KSeriesImage._isSafeSourcePath(parsed_url.path) is False
        ):
            return None
        return parsed_url.path

    @classmethod
    def _buildCandidates(cls, series: Series) -> list[_SeriesPosterCandidate]:
        """
        Series の内部ソース識別子から、許可済み CDN の取得候補を組み立てる。

        Args:
            series (Series): DB から取得した Series。

        Returns:
            list[_SeriesPosterCandidate]: Bangumi、TMDb の優先順に並んだ候補。
        """

        candidates: list[_SeriesPosterCandidate] = []
        if series.bangumi_subject_id is not None and series.bangumi_subject_image_url is not None:
            bangumi_url = cls._buildBangumiImageURL(series.bangumi_subject_image_url)
            if bangumi_url is not None:
                candidates.append(_SeriesPosterCandidate(
                    source='Bangumi',
                    external_id=series.bangumi_subject_id,
                    media_type=None,
                    source_identifier=series.bangumi_subject_image_url,
                    source_url=bangumi_url,
                    destination_path=cls._bangumiPosterPath(series.bangumi_subject_id),
                ))
        if (
            series.tmdb_id is not None
            and series.tmdb_media_type is not None
            and series.tmdb_poster_url is not None
        ):
            tmdb_url = cls._buildTmdbImageURL(series.tmdb_poster_url)
            if tmdb_url is not None:
                candidates.append(_SeriesPosterCandidate(
                    source='Tmdb',
                    external_id=series.tmdb_id,
                    media_type=series.tmdb_media_type,
                    source_identifier=series.tmdb_poster_url,
                    source_url=tmdb_url,
                    destination_path=cls._tmdbPosterPath(series.tmdb_media_type, series.tmdb_id),
                ))
        return candidates

    @classmethod
    async def _downloadAndStore(cls, candidate: _SeriesPosterCandidate, start_generation: int) -> bool:
        """
        候補画像を上限付きで取得し、世代が進んでいなければ WebP へ変換して原子的に公開する。

        Args:
            candidate (_SeriesPosterCandidate): 許可済み CDN と保存先を含む候補。
            start_generation (int): 取得開始時に観測した公開世代。

        Returns:
            bool: 現在の DB ソースと一致する WebP を公開できた場合は True。
        """

        # 所有外への書込みは通信前に拒否し、CDN への無駄な取得と警告の連発を避ける。
        if cls._ensureOwnedParent(candidate.destination_path, create=True) is False:
            logging.warning(
                f'[KonomiTVBS4KSeriesImage] Refused to write outside the owned directory. '
                f'[source: {candidate.source}, external_id: {candidate.external_id}]',
            )
            return False
        try:
            async with cls._download_semaphore:
                source_bytes = await cls._downloadSource(candidate)
                if source_bytes is None:
                    return False
                published = await asyncio.to_thread(
                    cls._convertAndStoreWebP,
                    source_bytes,
                    candidate.destination_path,
                    candidate.lock_key,
                    start_generation,
                    candidate.source_identifier,
                )
                if published is False:
                    return False
        except (httpx.HTTPError, TimeoutError, OSError, UnidentifiedImageError, Image.DecompressionBombError, ValueError) as ex:
            # 外部 URL はログへ出さず、利用者が再現箇所を特定できる最小限の ID と例外型だけを残す。
            logging.warning(
                f'[KonomiTVBS4KSeriesImage] Failed to store series poster. '
                f'[source: {candidate.source}, external_id: {candidate.external_id}, error: {type(ex).__name__}]',
            )
            return False

        # メタデータ更新と取得が競合した場合、古い識別子から作った画像を公開しない。
        ## 同一キーの並行公開は呼び出し元のロックで直列化されており、ここの unlink が
        ## 新しい置換を消すことはない。削除を担う invalidation との二重 unlink は missing_ok で安全である。
        ## 生成元識別子も同時に消し、次回 GET の照合が残存 sidecar に惑わされないようにする。
        if await cls._isCurrentSource(candidate) is False:
            await asyncio.to_thread(
                cls._unlinkValidatedFiles,
                candidate.destination_path,
                candidate.source,
                candidate.external_id,
            )
            return False
        return True

    @classmethod
    async def _downloadSource(cls, candidate: _SeriesPosterCandidate) -> bytes | None:
        """
        Content-Length と実受信量の両方を制限して画像バイト列を取得する。

        Args:
            candidate (_SeriesPosterCandidate): 取得元 URL を持つ候補。

        Returns:
            bytes | None: 上限内の HTTP 200 応答。取得不能時は None。
        """

        async with KONOMITV_BS4K_SERIES_IMAGE_HTTPX_CLIENT() as httpx_client:
            # read timeout が小刻みな受信で更新され続けても、GET 全体を20秒で打ち切る。
            async with asyncio.timeout(20):
                async with httpx_client.stream('GET', candidate.source_url) as response:
                    if response.status_code != 200:
                        logging.warning(
                            f'[KonomiTVBS4KSeriesImage] Series poster CDN returned an error. '
                            f'[source: {candidate.source}, external_id: {candidate.external_id}, '
                            f'status: {response.status_code}]',
                        )
                        return None
                    content_length = response.headers.get('Content-Length')
                    if content_length is not None:
                        try:
                            if int(content_length) > cls.MAX_SOURCE_BYTES:
                                return None
                        except ValueError:
                            return None
                    source_data = bytearray()
                    async for chunk in response.aiter_bytes():
                        source_data.extend(chunk)
                        if len(source_data) > cls.MAX_SOURCE_BYTES:
                            return None
                    return bytes(source_data)

    @classmethod
    async def _isCurrentSource(cls, candidate: _SeriesPosterCandidate) -> bool:
        """
        ダウンロード完了時にも同じ外部 ID とソース識別子が DB に残るか確認する。

        Args:
            candidate (_SeriesPosterCandidate): ダウンロードに用いた候補。

        Returns:
            bool: 同じソースを参照する Series が存在する場合は True。
        """

        if candidate.source == 'Bangumi':
            return await Series.filter(
                bangumi_subject_id=candidate.external_id,
                bangumi_subject_image_url=candidate.source_identifier,
            ).exists()
        assert candidate.media_type is not None
        return await Series.filter(
            tmdb_id=candidate.external_id,
            tmdb_media_type=candidate.media_type,
            tmdb_poster_url=candidate.source_identifier,
        ).exists()

    @classmethod
    def _convertAndStoreWebP(
        cls,
        source_bytes: bytes,
        destination_path: Path,
        lock_key: tuple[str, str],
        start_generation: int,
        source_identifier: str,
    ) -> bool:
        """
        入力画像を幅500以下の WebP に変換し、世代が進んでいなければ対となる sidecar と共に公開する。

        Args:
            source_bytes (bytes): CDN から取得した JPEG / PNG / WebP。
            destination_path (Path): 外部 ID 配下の `poster.webp`。
            lock_key (tuple[str, str]): 取得元と外部 ID から作ったキー。
            start_generation (int): 取得開始時に観測した公開世代。
            source_identifier (str): 生成元として sidecar へ記録する内部識別子。

        Returns:
            bool: 公開できた場合は True。待機中に世代が進んだ場合は一時ファイルだけ破棄して False。

        Raises:
            OSError: 所有連鎖の検証失敗、画像デコード、保存、fsync、rename に失敗した場合。
            ValueError: 画像寸法が不正または大きすぎる場合。
        """

        # 検証と置換の間の symlink 差し替えは残るが、常設の symlink 所有ルートはここで拒否する。
        if cls._ensureOwnedParent(destination_path, create=True) is False:
            raise OSError('Series poster destination is outside the owned directory.')
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix='.poster-',
            suffix='.webp',
            dir=destination_path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with Image.open(io.BytesIO(source_bytes), formats=['JPEG', 'PNG', 'WEBP']) as source_image:
                if (
                    source_image.width <= 0
                    or source_image.height <= 0
                    or source_image.width * source_image.height > cls.MAX_SOURCE_PIXELS
                ):
                    raise ValueError('Series poster dimensions are invalid.')
                poster_image = ImageOps.exif_transpose(source_image)
                output_mode = (
                    'RGBA'
                    if poster_image.mode in {'RGBA', 'LA'} or 'transparency' in poster_image.info
                    else 'RGB'
                )
                converted_image = poster_image.convert(output_mode)
                try:
                    if converted_image.width > cls.MAX_POSTER_WIDTH:
                        converted_image.thumbnail(
                            (cls.MAX_POSTER_WIDTH, converted_image.height),
                            Image.Resampling.LANCZOS,
                        )
                    with os.fdopen(temporary_fd, 'wb') as temporary_file:
                        temporary_fd = -1
                        converted_image.save(
                            temporary_file,
                            format='WEBP',
                            quality=cls.WEBP_QUALITY,
                        )
                        temporary_file.flush()
                        os.fsync(temporary_file.fileno())
                finally:
                    converted_image.close()
            # 世代確認から置換・fsync までを invalidation と同じ錠で括り、古い成果の蘇生を防ぐ。
            ## 置換より先に世代が進んでいたら公開せず、置換より後に進んだ場合は invalidation の
            ## unlink が古い公開を消す。どちらの順序でも次回 GET が取り直す。
            with cls._generation_lock:
                if cls._poster_generations.get(lock_key, 0) != start_generation:
                    logging.debug(
                        f'[KonomiTVBS4KSeriesImage] Discarded a stale series poster. '
                        f'[destination: {destination_path.name}]',
                    )
                    return False
                # 対の対応を先に無効化する。旧 sidecar を削除し親を fsync して「対応無」を
                ## 永続化してから WebP、sidecar の順で公開するため、どの中断点でも状態は
                ## 「sidecar 欠損による再取得」か「一致した対」のいずれかになり、
                ## 旧 sidecar が新しい WebP の生成元として再利用されることはない。
                cls._posterSourcePath(destination_path).unlink(missing_ok=True)
                cls._fsyncParentDirectory(destination_path)
                os.replace(temporary_path, destination_path)
                cls._fsyncParentDirectory(destination_path)
                cls._replaceSourceIdentifier(destination_path, source_identifier)
            return True
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
            temporary_path.unlink(missing_ok=True)

    @classmethod
    def _fsyncParentDirectory(cls, destination_path: Path) -> None:
        """
        置換をディレクトリへ永続化するため、親ディレクトリを fsync する。

        Args:
            destination_path (Path): 置換直後の公開パス。

        Returns:
            None

        Raises:
            OSError: ディレクトリの open または fsync に失敗した場合。
        """

        directory_fd = os.open(destination_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @classmethod
    def _replaceSourceIdentifier(cls, destination_path: Path, source_identifier: str) -> None:
        """
        生成元識別子ファイルを一時ファイルから原子的に置換する。

        Args:
            destination_path (Path): 対になる公開 WebP のパス。
            source_identifier (str): 記録する内部ソース識別子。

        Returns:
            None

        Raises:
            OSError: 一時ファイルの作成、fsync、rename に失敗した場合。
        """

        # WebP 置換後の最終段。呼び出し側が旧 sidecar を先に消しているため、ここまでのどの
        ## 中断点でも sidecar は欠損か旧 WebP との一致対であり、不一致の旧対は残らない。
        source_target = cls._posterSourcePath(destination_path)
        source_fd, source_name = tempfile.mkstemp(
            prefix='.poster-src-',
            suffix='.source',
            dir=destination_path.parent,
        )
        source_temporary = Path(source_name)
        try:
            with os.fdopen(source_fd, 'wb') as source_file:
                source_fd = -1
                source_file.write(source_identifier.encode('utf-8'))
                source_file.flush()
                os.fsync(source_file.fileno())
            os.replace(source_temporary, source_target)
            cls._fsyncParentDirectory(destination_path)
        finally:
            if source_fd >= 0:
                os.close(source_fd)
            source_temporary.unlink(missing_ok=True)

    @classmethod
    def _buildTmdbImageURL(cls, source_identifier: str) -> str | None:
        """
        TMDb の poster_path または旧保存 URL から w500 の取得 URL を組み立てる。

        Args:
            source_identifier (str): poster_path、または移行前に保存された TMDb CDN URL。

        Returns:
            str | None: 許可済み TMDb CDN の URL。不正な識別子は None。
        """

        poster_path = source_identifier
        parsed_url = cls._splitSourceURL(source_identifier)
        if parsed_url is None:
            return None
        if parsed_url.scheme != '' or parsed_url.netloc != '':
            try:
                source_port = parsed_url.port
            except ValueError:
                # 非数値 port は SplitResult.port が ValueError を送出するため、不正 URL として拒否する。
                return None
            if (
                parsed_url.scheme != 'https'
                or parsed_url.hostname != 'image.tmdb.org'
                or parsed_url.username is not None
                or parsed_url.password is not None
                or source_port not in {None, 443}
                or parsed_url.query != ''
                or parsed_url.fragment != ''
            ):
                return None
            legacy_match = re.fullmatch(r'/t/p/(?:w[0-9]+|original)(/.*)', parsed_url.path)
            if legacy_match is None:
                return None
            poster_path = legacy_match.group(1)
        if cls._isSafeSourcePath(poster_path) is False:
            return None
        return f'{cls.TMDB_IMAGE_ORIGIN}/t/p/w500{poster_path}'

    @classmethod
    def _buildBangumiImageURL(cls, source_identifier: str) -> str | None:
        """
        Bangumi の画像パスまたは旧保存 URL から取得 URL を組み立てる。

        Args:
            source_identifier (str): `/pic/cover/` 以下のパス、または移行前の CDN URL。

        Returns:
            str | None: 許可済み Bangumi CDN の URL。不正な識別子は None。
        """

        image_path = source_identifier
        parsed_url = cls._splitSourceURL(source_identifier)
        if parsed_url is None:
            return None
        if parsed_url.scheme != '' or parsed_url.netloc != '':
            normalized_identifier = cls.normalizeBangumiImageIdentifier(source_identifier)
            if normalized_identifier is None:
                return None
            image_path = normalized_identifier
        if image_path.startswith('/pic/cover/') is False or cls._isSafeSourcePath(image_path) is False:
            return None
        return f'{cls.BANGUMI_IMAGE_ORIGIN}{image_path}'

    @staticmethod
    def _splitSourceURL(raw_url: str) -> SplitResult | None:
        """
        不正入力で ValueError を送出し得る urlsplit を検証境界で包む。

        Args:
            raw_url (str): 外部 API 由来または DB 保存済みの URL 表現。

        Returns:
            SplitResult | None: 分解結果。分解不能な不正入力は None。
        """

        try:
            return urlsplit(raw_url)
        except ValueError:
            return None

    @staticmethod
    def _isSafeSourcePath(source_path: str) -> bool:
        """
        CDN origin に連結できる絶対パス表現だけを許可する。

        Args:
            source_path (str): 外部 API 由来の画像パス。

        Returns:
            bool: scheme・query・親参照を含まない場合は True。
        """

        parsed_path = KonomiTVBS4KSeriesImage._splitSourceURL(source_path)
        if parsed_path is None:
            return False
        return (
            source_path.startswith('/')
            and source_path.startswith('//') is False
            and parsed_path.scheme == ''
            and parsed_path.netloc == ''
            and parsed_path.query == ''
            and parsed_path.fragment == ''
            and '..' not in PurePosixPath(parsed_path.path).parts
        )

    @classmethod
    def _ensureOwnedParent(cls, destination_path: Path, create: bool) -> bool:
        """
        所有ルート配下の親連鎖に symlink が無いことを追従なしで検証する。

        Args:
            destination_path (Path): `series-images` 配下の保存先パス。
            create (bool): 欠損した親ディレクトリを実ディレクトリとして作る場合は True。

        Returns:
            bool: 保存先の親まで実ディレクトリ連鎖で到達できる場合は True。
        """

        return cls._isOwnedDirectory(destination_path.parent, create=create)

    @classmethod
    def _isOwnedDirectory(cls, directory: Path, create: bool) -> bool:
        """
        所有ルート配下のディレクトリ連鎖に symlink が無いことを追従なしで検証する。

        Args:
            directory (Path): `series-images` 配下のディレクトリ。
            create (bool): 欠損した要素を実ディレクトリとして作る場合は True。

        Returns:
            bool: 指定要素まで実ディレクトリ連鎖で到達できる場合は True。
        """

        # series-images 自体も lstat で実ディレクトリと確認する。ルートが symlink の系は全体を拒否する。
        try:
            relative_parts = directory.relative_to(KONOMITV_BS4K_SERIES_IMAGES_DIR).parts
        except ValueError:
            return False
        try:
            base_status = os.lstat(KONOMITV_BS4K_SERIES_IMAGES_DIR)
        except FileNotFoundError:
            if create is False:
                return False
            try:
                KONOMITV_BS4K_SERIES_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            except OSError:
                return False
            try:
                base_status = os.lstat(KONOMITV_BS4K_SERIES_IMAGES_DIR)
            except OSError:
                return False
        except OSError:
            return False
        if stat.S_ISLNK(base_status.st_mode) or not stat.S_ISDIR(base_status.st_mode):
            return False
        current = KONOMITV_BS4K_SERIES_IMAGES_DIR
        for part in relative_parts:
            if part in {'', '.', '..'}:
                return False
            current = current / part
            try:
                file_status = os.lstat(current)
            except FileNotFoundError:
                if create is False:
                    return False
                try:
                    os.mkdir(current)
                except FileExistsError:
                    try:
                        file_status = os.lstat(current)
                    except OSError:
                        return False
                except OSError:
                    return False
                else:
                    continue
            except OSError:
                return False
            if stat.S_ISLNK(file_status.st_mode) or not stat.S_ISDIR(file_status.st_mode):
                return False
        return True

    @classmethod
    def _readOwnedFile(cls, destination_path: Path) -> bytes | None:
        """
        所有連鎖を検証した上で表紙ファイルを読み取る。

        Args:
            destination_path (Path): `series-images` 配下の WebP パス。

        Returns:
            bytes | None: ファイル内容。未作成または所有外の場合は None。
        """

        if cls._ensureOwnedParent(destination_path, create=False) is False:
            # 所有外は欠損として扱う。運用者向けの警告は保存拒否側で出す。
            return None
        try:
            return destination_path.read_bytes()
        except FileNotFoundError:
            return None

    @staticmethod
    def _tmdbPosterPath(media_type: TmdbMediaType, tmdb_id: int) -> Path:
        """
        TMDb 作品のローカル表紙パスを返す。

        Args:
            media_type (TmdbMediaType): TMDb の作品種別。
            tmdb_id (int): TMDb 作品 ID。

        Returns:
            Path: `tmdb/{tv|movie}/{id}/poster.webp`。
        """

        return KONOMITV_BS4K_SERIES_IMAGES_DIR / 'tmdb' / media_type / str(tmdb_id) / 'poster.webp'

    @staticmethod
    def _bangumiPosterPath(subject_id: int) -> Path:
        """
        Bangumi 条目のローカル表紙パスを返す。

        Args:
            subject_id (int): Bangumi 条目 ID。

        Returns:
            Path: `bangumi/{id}/cover.webp`。
        """

        return KONOMITV_BS4K_SERIES_IMAGES_DIR / 'bangumi' / str(subject_id) / 'cover.webp'

    @classmethod
    def _cleanupOrphanedDirectories(
        cls,
        tmdb_keys: set[tuple[str, int]],
        bangumi_subject_ids: set[int],
    ) -> int:
        """
        DB の外部 ID 集合に無いディレクトリを series-images 以下から削除する。

        Args:
            tmdb_keys (set[tuple[str, int]]): 現在 DB に存在する作品種別と TMDb ID。
            bangumi_subject_ids (set[int]): 現在 DB に存在する Bangumi 条目 ID。

        Returns:
            int: 削除した外部 ID ディレクトリ数。
        """

        removed_count = 0
        for media_type in ('tv', 'movie'):
            media_root = KONOMITV_BS4K_SERIES_IMAGES_DIR / 'tmdb' / media_type
            # 所有ルート自体が symlink の場合は列挙せず、参照先に触れない。
            if cls._isOwnedDirectory(media_root, create=False) is False:
                continue
            if media_root.is_dir() is False:
                continue
            for identifier_path in media_root.iterdir():
                try:
                    tmdb_id = int(identifier_path.name)
                except ValueError:
                    tmdb_id = -1
                if (media_type, tmdb_id) in tmdb_keys:
                    continue
                cls._removeOwnedPath(identifier_path)
                removed_count += 1

        bangumi_root = KONOMITV_BS4K_SERIES_IMAGES_DIR / 'bangumi'
        # 所有ルート自体が symlink の場合は列挙せず、参照先に触れない。
        if cls._isOwnedDirectory(bangumi_root, create=False) and bangumi_root.is_dir():
            for identifier_path in bangumi_root.iterdir():
                try:
                    subject_id = int(identifier_path.name)
                except ValueError:
                    subject_id = -1
                if subject_id in bangumi_subject_ids:
                    continue
                cls._removeOwnedPath(identifier_path)
                removed_count += 1
        return removed_count

    @staticmethod
    def _removeOwnedPath(path: Path) -> None:
        """
        series-images 直下の孤児を symlink を辿らず削除する。

        Args:
            path (Path): モジュール所有ディレクトリ内の孤児パス。

        Returns:
            None
        """

        # 呼び出し元が所有外を示した場合は何もしない。abspath は語彙的に正規化するだけである。
        try:
            Path(os.path.abspath(path)).relative_to(KONOMITV_BS4K_SERIES_IMAGES_DIR)
        except ValueError:
            return
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
            return
        shutil.rmtree(path)
