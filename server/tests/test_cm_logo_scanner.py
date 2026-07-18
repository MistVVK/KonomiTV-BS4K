import struct
from pathlib import Path

import pytest
from PIL import Image

from app.metadata.CMLogoScanner import CMLogoScanner, CMLogoUnsupportedError


_FILE_HEADER = struct.Struct('>28sI')
_LOGO_HEADER = struct.Struct('<32s8h')
_PIXEL = struct.Struct('<6h')
_EXTENDED_HEADER = struct.Struct('<10i255sx i60i')


def BuildLogo(
    *,
    name: str = 'テスト局',
    width: int = 8,
    height: int = 4,
    service_id: int | None = None,
    logo_count: int = 1,
) -> bytes:
    """標準単一ロゴと、完全な拡張v1ロゴを作る。"""

    x = 100
    y = 50
    content = bytearray(_FILE_HEADER.pack(b'<logo data file ver0.1>', logo_count))
    content.extend(_LOGO_HEADER.pack(
        name.encode('cp932')[:31].ljust(32, b'\0'),
        x,
        y,
        height,
        width,
        0,
        0,
        0,
        0,
    ))
    for _ in range(width * height):
        content.extend(_PIXEL.pack(1000, 4096, 1000, 0, 1000, 0))
    if service_id is None:
        return bytes(content)

    encoded_name = name.encode('cp932')[:254].ljust(255, b'\0')
    content.extend(_EXTENDED_HEADER.pack(
        0x12345,
        1,
        width,
        height,
        1,
        1,
        1920,
        1080,
        x,
        y,
        encoded_name,
        service_id,
        *([0] * 60),
    ))
    uv_width = width >> 1
    uv_height = height >> 1
    float_count = (width * height + uv_width * uv_height * 2) * 2
    content.extend(bytes(float_count * 4))
    return bytes(content)


def test_read_standard_aviutl_single_logo_without_sid(tmp_path: Path) -> None:
    """標準AviUtl v0.1はSIDを推測せず未割り当てとして読む。"""

    logo_path = tmp_path / 'standard.lgd'
    logo_path.write_bytes(BuildLogo())

    metadata = CMLogoScanner.ReadMetadata(logo_path)

    assert metadata.file_format == 'AviUtlV0.1'
    assert metadata.service_id is None
    assert metadata.logo_name == 'テスト局'
    assert metadata.logo_width == 8
    assert metadata.logo_height == 4
    assert metadata.image_width is None
    assert metadata.image_height is None
    assert metadata.image_x == 100
    assert metadata.image_y == 50


def test_read_amatsukaze_extended_v1_metadata(tmp_path: Path) -> None:
    """既存の拡張v1はSIDと生成元キャンバスを保持して読む。"""

    logo_path = tmp_path / 'SID101-test.lgd'
    logo_path.write_bytes(BuildLogo(service_id=101))

    metadata = CMLogoScanner.ReadMetadata(logo_path)

    assert metadata.file_format == 'AmatsukazeExtendedV1'
    assert metadata.service_id == 101
    assert metadata.image_width == 1920
    assert metadata.image_height == 1080


def test_render_standard_logo_as_transparent_png(tmp_path: Path) -> None:
    """外部実行ファイルなしでAviUtl互換画素をPNGへ変換する。"""

    logo_path = tmp_path / 'standard.lgd'
    preview_path = tmp_path / 'preview.png'
    logo_path.write_bytes(BuildLogo())

    CMLogoScanner.RenderPreview(logo_path, preview_path)

    with Image.open(preview_path) as image:
        assert image.format == 'PNG'
        assert image.mode == 'RGBA'
        assert image.size == (8, 4)
        assert image.getpixel((0, 0)) == (255, 255, 255, 255)


def test_reject_multi_logo_and_lgd2_explicitly(tmp_path: Path) -> None:
    """複数ロゴと.lgd2を単一ロゴとして誤読しない。"""

    multi_path = tmp_path / 'multi.lgd'
    multi_path.write_bytes(BuildLogo(logo_count=2))
    with pytest.raises(CMLogoUnsupportedError, match='Multi-logo'):
        CMLogoScanner.ReadMetadata(multi_path)

    lgd2_path = tmp_path / 'multi.lgd2'
    lgd2_path.write_bytes(BuildLogo())
    with pytest.raises(CMLogoUnsupportedError, match='lgd2'):
        CMLogoScanner.ReadMetadata(lgd2_path)


def test_reject_truncated_extended_logo(tmp_path: Path) -> None:
    """拡張ヘッダーだけが残った破損ファイルを受理しない。"""

    logo_path = tmp_path / 'truncated.lgd'
    logo_path.write_bytes(BuildLogo(service_id=101)[:-16])

    with pytest.raises(ValueError, match='extended pixel data size'):
        CMLogoScanner.ReadMetadata(logo_path)
