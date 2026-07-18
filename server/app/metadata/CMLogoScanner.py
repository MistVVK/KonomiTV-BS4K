from __future__ import annotations

import asyncio
import hashlib
import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from PIL import Image

from app import logging
from app.constants import DATA_DIR, JST
from app.metadata.CMAnalysisPaths import ResolveCMHostPath
from app.models.CMAnalysis import (
    CMAnalysisSettings,
    CMLogo,
    CMLogoFileFormat,
)


_AVIUTL_LOGO_FILE_HEADER = struct.Struct('>28sI')
_AVIUTL_LOGO_HEADER = struct.Struct('<32s8h')
_AVIUTL_LOGO_PIXEL = struct.Struct('<6h')
_AVIUTL_LOGO_MAGIC = b'<logo data file ver0.1>'
_AMATSUKAZE_LOGO_HEADER = struct.Struct('<10i255sx i60i')
_AMATSUKAZE_LOGO_MAGIC = 0x12345
_AMATSUKAZE_LOGO_VERSION = 1
_MAX_LOGO_PIXELS = 4_194_304


class CMLogoUnsupportedError(ValueError):
    """構造は判別できるがKonomiTVでは扱わないロゴ形式を表す。"""


@dataclass(frozen=True, slots=True)
class CMLogoFileMetadata:
    """単一ロゴ.lgdから読み取った解析・表示用メタデータ。"""

    service_id: int | None
    logo_name: str
    file_format: CMLogoFileFormat
    logo_width: int
    logo_height: int
    image_width: int | None
    image_height: int | None
    image_x: int
    image_y: int


@dataclass(frozen=True, slots=True)
class _CMLogoFile:
    metadata: CMLogoFileMetadata
    pixels: bytes


class CMLogoScanner:
    """共有ロゴフォルダをハッシュ差分で排他的にDBへ同期する。"""

    _scan_lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    @staticmethod
    def ReadMetadata(path: Path) -> CMLogoFileMetadata:
        """AviUtl v0.1単一ロゴまたはAmatsukaze拡張v1を検証して読む。

        AviUtl標準形式にはSIDと生成元キャンバス情報がないため、それらはNoneになる。
        .lgd2・複数ロゴ・v0.2は曖昧に解釈せず明示的に非対応とする。
        """

        return CMLogoScanner._readLogo(path).metadata

    @staticmethod
    def RenderPreview(path: Path, output_path: Path) -> None:
        """AviUtl互換ピクセルを透過PNGへ変換する。"""

        logo = CMLogoScanner._readLogo(path)
        metadata = logo.metadata
        rgba_pixels: list[tuple[int, int, int, int]] = []
        for offset in range(0, len(logo.pixels), _AVIUTL_LOGO_PIXEL.size):
            dp_y, y, dp_cb, cb, dp_cr, cr = _AVIUTL_LOGO_PIXEL.unpack_from(logo.pixels, offset)
            alpha = round(max(0, min(1000, max(dp_y, dp_cb, dp_cr))) * 255 / 1000)
            # AviUtlのYC48値域をBT.601相当のプレビュー色へ変換する。
            y8 = y * 255 / 4096
            cb8 = cb * 255 / 4096
            cr8 = cr * 255 / 4096
            red = round(y8 + 1.402 * cr8)
            green = round(y8 - 0.344136 * cb8 - 0.714136 * cr8)
            blue = round(y8 + 1.772 * cb8)
            rgba_pixels.append((
                max(0, min(255, red)),
                max(0, min(255, green)),
                max(0, min(255, blue)),
                alpha,
            ))

        image = Image.new('RGBA', (metadata.logo_width, metadata.logo_height))
        image.putdata(rgba_pixels)
        image.save(output_path, format='PNG', optimize=True)

    @staticmethod
    def _readLogo(path: Path) -> _CMLogoFile:
        """ファイル全体を境界検証し、先頭の単一AviUtlロゴを返す。"""

        if path.suffix.lower() == '.lgd2':
            raise CMLogoUnsupportedError('LogoFormatUnsupported: .lgd2 files are not supported.')

        content = path.read_bytes()
        minimum_size = _AVIUTL_LOGO_FILE_HEADER.size + _AVIUTL_LOGO_HEADER.size
        if len(content) < minimum_size:
            raise ValueError('LogoFileTruncated: The AviUtl header is incomplete.')

        file_header_text, logo_count = _AVIUTL_LOGO_FILE_HEADER.unpack_from(content)
        file_header_text = file_header_text.rstrip(b'\0')
        if file_header_text.startswith(b'<logo data file ver0.2'):
            raise CMLogoUnsupportedError('LogoFormatUnsupported: AviUtl v0.2 is not supported.')
        if file_header_text != _AVIUTL_LOGO_MAGIC:
            raise ValueError('LogoHeaderInvalid: The AviUtl v0.1 header is invalid.')
        if logo_count != 1:
            raise CMLogoUnsupportedError('LogoFormatUnsupported: Multi-logo files are not supported.')

        base_header_offset = _AVIUTL_LOGO_FILE_HEADER.size
        base_values = _AVIUTL_LOGO_HEADER.unpack_from(content, base_header_offset)
        name_bytes, image_x, image_y, logo_height, logo_width, _, _, _, _ = base_values
        pixel_count = logo_width * logo_height
        if (
            logo_width <= 0 or logo_height <= 0 or
            image_x < 0 or image_y < 0 or
            pixel_count > _MAX_LOGO_PIXELS
        ):
            raise ValueError('LogoDimensionsInvalid: The logo dimensions or coordinates are invalid.')

        pixel_offset = base_header_offset + _AVIUTL_LOGO_HEADER.size
        pixel_size = pixel_count * _AVIUTL_LOGO_PIXEL.size
        base_end = pixel_offset + pixel_size
        if len(content) < base_end:
            raise ValueError('LogoFileTruncated: The AviUtl pixel data is incomplete.')
        pixels = content[pixel_offset:base_end]
        base_name = name_bytes.split(b'\0', maxsplit=1)[0].decode('cp932', errors='replace')

        if len(content) == base_end:
            return _CMLogoFile(
                metadata=CMLogoFileMetadata(
                    service_id=None,
                    logo_name=base_name,
                    file_format='AviUtlV0.1',
                    logo_width=logo_width,
                    logo_height=logo_height,
                    image_width=None,
                    image_height=None,
                    image_x=image_x,
                    image_y=image_y,
                ),
                pixels=pixels,
            )

        if len(content) < base_end + _AMATSUKAZE_LOGO_HEADER.size:
            raise ValueError('LogoFileTruncated: The extended header is incomplete.')
        values = _AMATSUKAZE_LOGO_HEADER.unpack_from(content, base_end)
        magic, version = values[0], values[1]
        extended_width, extended_height = values[2], values[3]
        log_uv_x, log_uv_y = values[4], values[5]
        image_width, image_height, extended_x, extended_y = values[6:10]
        extended_name_bytes = values[10]
        service_id = values[11]
        if magic != _AMATSUKAZE_LOGO_MAGIC or version != _AMATSUKAZE_LOGO_VERSION:
            raise ValueError('LogoExtendedHeaderInvalid: Unknown trailing logo data.')
        if log_uv_x not in (0, 1, 2) or log_uv_y not in (0, 1, 2):
            raise ValueError('LogoExtendedHeaderInvalid: Invalid chroma subsampling.')
        if (
            extended_width != logo_width or extended_height != logo_height or
            extended_x != image_x or extended_y != image_y or
            image_width <= 0 or image_height <= 0 or
            extended_x + extended_width > image_width or extended_y + extended_height > image_height or
            service_id <= 0 or service_id > 0xFFFF
        ):
            raise ValueError('LogoExtendedHeaderInvalid: The extended geometry or service ID is invalid.')

        uv_width = extended_width >> log_uv_x
        uv_height = extended_height >> log_uv_y
        float_count = (extended_width * extended_height + uv_width * uv_height * 2) * 2
        expected_size = base_end + _AMATSUKAZE_LOGO_HEADER.size + float_count * 4
        if len(content) != expected_size:
            raise ValueError('LogoExtendedDataInvalid: The extended pixel data size is invalid.')
        assert isinstance(extended_name_bytes, bytes)
        extended_name = extended_name_bytes.split(b'\0', maxsplit=1)[0].decode('cp932', errors='replace')
        return _CMLogoFile(
            metadata=CMLogoFileMetadata(
                service_id=service_id,
                logo_name=extended_name or base_name,
                file_format='AmatsukazeExtendedV1',
                logo_width=logo_width,
                logo_height=logo_height,
                image_width=image_width,
                image_height=image_height,
                image_x=image_x,
                image_y=image_y,
            ),
            pixels=pixels,
        )

    @classmethod
    async def scan(cls) -> list[CMLogo]:
        """現在の共有フォルダをDBへ同期し、外部削除はmissingとして保持する。"""

        async with cls._scan_lock:
            settings, _ = await CMAnalysisSettings.get_or_create(id=1, defaults={'enabled': False})
            configured_path = Path(settings.logo_directory) if settings.logo_directory else None
            runtime_directory = ResolveCMHostPath(configured_path) if configured_path else DATA_DIR / 'cm-analysis/logos'
            await asyncio.to_thread(runtime_directory.mkdir, parents=True, exist_ok=True)

            entries = await asyncio.to_thread(
                lambda: sorted((*runtime_directory.glob('*.lgd'), *runtime_directory.glob('*.lgd2'))),
            )
            seen_paths: set[str] = set()
            for runtime_path in entries:
                stored_path = str(configured_path / runtime_path.name) if configured_path else str(runtime_path)
                seen_paths.add(stored_path)
                try:
                    stat = await asyncio.to_thread(runtime_path.stat)
                    content = await asyncio.to_thread(runtime_path.read_bytes)
                    file_hash = hashlib.sha256(content).hexdigest()
                    existing = await CMLogo.get_or_none(path=stored_path)
                    if (
                        existing is not None and
                        existing.file_hash == file_hash and
                        existing.file_size == stat.st_size
                    ):
                        if existing.missing or existing.deleted_at is not None:
                            existing.missing = False
                            existing.deleted_at = None
                            await existing.save(update_fields=['missing', 'deleted_at', 'updated_at'])
                        continue
                    metadata = await asyncio.to_thread(cls.ReadMetadata, runtime_path)
                    if existing is None:
                        await CMLogo.create(
                            path=stored_path,
                            filename=runtime_path.name,
                            service_id=metadata.service_id,
                            logo_name=metadata.logo_name,
                            file_format=metadata.file_format,
                            file_hash=file_hash,
                            file_size=stat.st_size,
                        )
                    else:
                        existing.filename = runtime_path.name
                        existing.service_id = metadata.service_id
                        existing.logo_name = metadata.logo_name
                        existing.file_format = metadata.file_format
                        existing.file_hash = file_hash
                        existing.file_size = stat.st_size
                        existing.missing = False
                        existing.deleted_at = None
                        await existing.save()
                except (OSError, ValueError) as ex:
                    logging.warning(f'{runtime_path}: Unsupported or invalid CM logo file. Skipping...', exc_info=ex)

            # 自動削除は行わず、この共有フォルダに属していたファイルの外部削除だけを履歴へ反映する。
            stored_directory = configured_path if configured_path else runtime_directory
            existing_logos = await CMLogo.all()
            now = datetime.now(tz=JST)
            for logo in existing_logos:
                try:
                    belongs_to_directory = Path(logo.path).parent == stored_directory
                except (OSError, ValueError):
                    belongs_to_directory = False
                if belongs_to_directory and logo.path not in seen_paths and logo.missing is False:
                    logo.missing = True
                    logo.deleted_at = now
                    await logo.save(update_fields=['missing', 'deleted_at', 'updated_at'])

            return await CMLogo.filter(missing=False, deleted_at=None).order_by('service_id', 'filename')
