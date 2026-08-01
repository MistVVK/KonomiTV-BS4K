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
    pixel_opacities: list[int] | None = None,
    pixel_dp_channels: list[tuple[int, int, int]] | None = None,
) -> bytes:
    """標準単一ロゴと、完全な拡張v1ロゴを作る。

    pixel_opacities は各画素の dp_y/dp_cb/dp_cr に共通で使う不透明度(0-1000)。
    pixel_dp_channels は画素ごとの (dp_y, dp_cb, dp_cr) を個別指定する。
    両方省略時は全画素 1000（完全不透明）。同時指定は不可。
    """

    if pixel_opacities is not None and pixel_dp_channels is not None:
        raise ValueError('pixel_opacities and pixel_dp_channels are mutually exclusive')

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
    pixel_count = width * height
    if pixel_dp_channels is not None:
        if len(pixel_dp_channels) != pixel_count:
            raise ValueError('pixel_dp_channels length must equal width * height')
        for dp_y, dp_cb, dp_cr in pixel_dp_channels:
            content.extend(_PIXEL.pack(dp_y, 4096, dp_cb, 0, dp_cr, 0))
    else:
        if pixel_opacities is None:
            opacities = [1000] * pixel_count
        else:
            if len(pixel_opacities) != pixel_count:
                raise ValueError('pixel_opacities length must equal width * height')
            opacities = pixel_opacities
        for opacity in opacities:
            content.extend(_PIXEL.pack(opacity, 4096, opacity, 0, opacity, 0))
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
        assert image.getpixel((0, 0)) == (255, 0, 255, 255)


def test_render_preview_preserves_alpha_with_fixed_magenta(tmp_path: Path) -> None:
    """RGBはマゼンタ固定、alphaはdp値から既存式で計算されることを検証する。"""

    # alpha = round(clamp(dp, 0, 1000) * 255 / 1000)
    # 0 → 0, 500 → 128, 1000 → 255, 2000 は 1000 にクランプ → 255
    logo_path = tmp_path / 'alpha.lgd'
    preview_path = tmp_path / 'alpha.png'
    logo_path.write_bytes(BuildLogo(
        width=2,
        height=2,
        pixel_opacities=[0, 500, 1000, 2000],
    ))

    CMLogoScanner.RenderPreview(logo_path, preview_path)

    with Image.open(preview_path) as image:
        assert image.mode == 'RGBA'
        assert image.size == (2, 2)
        assert image.getpixel((0, 0)) == (255, 0, 255, 0)
        assert image.getpixel((1, 0)) == (255, 0, 255, 128)
        assert image.getpixel((0, 1)) == (255, 0, 255, 255)
        assert image.getpixel((1, 1)) == (255, 0, 255, 255)


def test_render_preview_alpha_uses_max_of_dp_channels(tmp_path: Path) -> None:
    """alpha は max(dp_y, dp_cb, dp_cr) と 0..1000 クランプを使う。

    3チャンネルを常に同値にすると max 選択が退行しても検知できないため、
    各チャンネル単独が最大になるケースと下限クランプを拘束する。
    """

    # alpha = round(clamp(max(dp_y, dp_cb, dp_cr), 0, 1000) * 255 / 1000)
    logo_path = tmp_path / 'max-channel.lgd'
    preview_path = tmp_path / 'max-channel.png'
    logo_path.write_bytes(BuildLogo(
        width=3,
        height=2,
        pixel_dp_channels=[
            (500, 0, 0),          # max = dp_y  → 128
            (0, 500, 0),          # max = dp_cb → 128
            (0, 0, 500),          # max = dp_cr → 128
            (-1000, -500, -200),  # max = -200 → 下限 0 → 0（クランプ無しだと負 alpha）
            (100, 800, 200),      # max = 800 → 204
            (2000, 100, 50),      # max = 2000 → 上限 1000 → 255
        ],
    ))

    CMLogoScanner.RenderPreview(logo_path, preview_path)

    with Image.open(preview_path) as image:
        assert image.mode == 'RGBA'
        assert image.size == (3, 2)
        assert image.getpixel((0, 0)) == (255, 0, 255, 128)
        assert image.getpixel((1, 0)) == (255, 0, 255, 128)
        assert image.getpixel((2, 0)) == (255, 0, 255, 128)
        assert image.getpixel((0, 1)) == (255, 0, 255, 0)
        assert image.getpixel((1, 1)) == (255, 0, 255, 204)
        assert image.getpixel((2, 1)) == (255, 0, 255, 255)


def test_render_preview_clamps_alpha_before_pillow_putdata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pillow putdata 前の alpha を検査し、明示クランプ削除を検知する。

    Pillow は putdata / PNG 化時に alpha を 0..255 へ飽和するため、
    生成後の PNG 画素だけでは max(0, min(1000, ...)) を外してもテストが通る。
    クランプ無しだと下限ケースは -51、上限ケースは 510 になる。
    """

    captured: list[list[tuple[int, int, int, int]]] = []
    original_putdata = Image.Image.putdata

    def spy_putdata(self: Image.Image, data: list[tuple[int, int, int, int]], *args, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(list(data))
        return original_putdata(self, data, *args, **kwargs)

    monkeypatch.setattr(Image.Image, 'putdata', spy_putdata)

    logo_path = tmp_path / 'clamp.lgd'
    preview_path = tmp_path / 'clamp.png'
    logo_path.write_bytes(BuildLogo(
        width=2,
        height=1,
        pixel_dp_channels=[
            # max=-200 → clamp 後 0 → alpha 0（クランプ無し: round(-200*255/1000)=-51）
            (-1000, -500, -200),
            # max=2000 → clamp 後 1000 → alpha 255（クランプ無し: round(2000*255/1000)=510）
            (2000, 100, 50),
        ],
    ))

    CMLogoScanner.RenderPreview(logo_path, preview_path)

    assert len(captured) == 1
    # Pillow 飽和前の値。クランプ無しだと [..., -51] / [..., 510] になりここで落ちる。
    assert captured[0] == [
        (255, 0, 255, 0),
        (255, 0, 255, 255),
    ]


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
