import struct
from pathlib import Path

import pytest

from app.metadata.CMLogoCandidateValidator import CMLogoCandidateValidator


_FILE_HEADER = struct.Struct('<28s4s')
_LOGO_HEADER = struct.Struct('<32s8h')
_PIXEL = struct.Struct('<6h')


def WriteCandidate(path: Path, x: int, y: int, alpha: list[list[int]]) -> None:
    """比較テスト用の最小AviUtlロゴを書き出す。"""

    height = len(alpha)
    width = len(alpha[0])
    content = bytearray(_FILE_HEADER.pack(b'<logo data file ver0.1>', b'\0\0\0\1'))
    content.extend(_LOGO_HEADER.pack(b'test', x, y, height, width, 0, 0, 0, 0))
    for row in alpha:
        for value in row:
            content.extend(_PIXEL.pack(value, 1024, value, 0, value, 0))
    path.write_bytes(content)


def test_candidates_from_shifted_regions_are_recognized_as_same_logo(tmp_path: Path) -> None:
    """生成時の微小な切り出し位置差があっても同じ透過マスクなら合意する。"""

    alpha = [[0] * 12 for _ in range(12)]
    for index in range(2, 10):
        alpha[5][index] = 500
        alpha[index][5] = 500
    first_path = tmp_path / 'first.lgd'
    second_path = tmp_path / 'second.lgd'
    WriteCandidate(first_path, 100, 30, alpha)
    WriteCandidate(second_path, 101, 31, alpha)

    first = CMLogoCandidateValidator.load(first_path)
    second = CMLogoCandidateValidator.load(second_path)

    assert CMLogoCandidateValidator.areSimilar(first, second) is True


def test_dense_opaque_rectangle_is_rejected(tmp_path: Path) -> None:
    """QRコードなどに近い不透明な矩形候補は正式検証へ進めない。"""

    path = tmp_path / 'opaque.lgd'
    WriteCandidate(path, 100, 30, [[1000] * 12 for _ in range(12)])

    with pytest.raises(ValueError, match=r'CandidateLogoMaskTooDense|CandidateLogoOpaqueRectangle'):
        CMLogoCandidateValidator.load(path)
