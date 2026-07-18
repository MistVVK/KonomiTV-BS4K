from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path


_LOGO_FILE_HEADER = struct.Struct('<28s4s')
_LOGO_HEADER = struct.Struct('<32s8h')
_LOGO_PIXEL = struct.Struct('<6h')
_LOGO_FILE_MAGIC = b'<logo data file ver0.1>'


@dataclass(frozen=True, slots=True)
class CMLogoCandidate:
    """自動生成された.lgd候補の比較に必要な基礎情報。"""

    path: Path
    x: int
    y: int
    width: int
    height: int
    alpha: tuple[int, ...]
    active_ratio: float
    opaque_ratio: float


class CMLogoCandidateValidator:
    """同一録画の独立区間から生成した候補を、正式登録前に検査する。"""

    _ACTIVE_ALPHA = 20

    @classmethod
    def load(cls, path: Path) -> CMLogoCandidate:
        """AviUtl互換部を読み、壊れた候補と明白な非ロゴ候補を拒否する。"""

        content = path.read_bytes()
        minimum_size = _LOGO_FILE_HEADER.size + _LOGO_HEADER.size
        if len(content) < minimum_size:
            raise ValueError('CandidateLogoTruncated')
        file_header_text, _ = _LOGO_FILE_HEADER.unpack_from(content)
        if file_header_text.rstrip(b'\0') != _LOGO_FILE_MAGIC:
            raise ValueError('CandidateLogoHeaderInvalid')
        _, x, y, height, width, _, _, _, _ = _LOGO_HEADER.unpack_from(content, _LOGO_FILE_HEADER.size)
        if width < 8 or height < 8 or width > 1024 or height > 1024:
            raise ValueError('CandidateLogoDimensionsAbnormal')
        pixel_count = width * height
        expected_size = minimum_size + pixel_count * _LOGO_PIXEL.size
        if len(content) < expected_size:
            raise ValueError('CandidateLogoPixelsTruncated')

        alpha = tuple(
            max(0, min(1000, _LOGO_PIXEL.unpack_from(content, minimum_size + index * _LOGO_PIXEL.size)[0]))
            for index in range(pixel_count)
        )
        active = [value for value in alpha if value >= cls._ACTIVE_ALPHA]
        if len(active) < max(8, math.ceil(pixel_count * 0.005)):
            raise ValueError('CandidateLogoTooSparse')
        active_ratio = len(active) / pixel_count
        opaque_ratio = sum(value >= 850 for value in active) / len(active)
        if active_ratio > 0.75:
            raise ValueError('CandidateLogoMaskTooDense')
        # QRコードなど「領域の大半が不透明」な固定矩形だけを保守的に排除する。
        if active_ratio > 0.55 and opaque_ratio > 0.65:
            raise ValueError('CandidateLogoOpaqueRectangle')
        if max(width / height, height / width) > 10:
            raise ValueError('CandidateLogoAspectRatioAbnormal')
        return CMLogoCandidate(path, x, y, width, height, alpha, active_ratio, opaque_ratio)

    @classmethod
    def areSimilar(cls, first: CMLogoCandidate, second: CMLogoCandidate) -> bool:
        """位置ずれを少量許容し、透過マスクの重なりから同一候補か判定する。"""

        best_similarity = 0.0
        for offset_y in range(-2, 3):
            for offset_x in range(-2, 3):
                weighted_intersection = 0
                weighted_union = 0
                coordinates: set[tuple[int, int]] = set()
                first_values: dict[tuple[int, int], int] = {}
                second_values: dict[tuple[int, int], int] = {}
                for index, value in enumerate(first.alpha):
                    if value < cls._ACTIVE_ALPHA:
                        continue
                    coordinate = (first.x + index % first.width, first.y + index // first.width)
                    first_values[coordinate] = value
                    coordinates.add(coordinate)
                for index, value in enumerate(second.alpha):
                    if value < cls._ACTIVE_ALPHA:
                        continue
                    coordinate = (
                        second.x + index % second.width + offset_x,
                        second.y + index // second.width + offset_y,
                    )
                    second_values[coordinate] = value
                    coordinates.add(coordinate)
                for coordinate in coordinates:
                    first_value = first_values.get(coordinate, 0)
                    second_value = second_values.get(coordinate, 0)
                    weighted_intersection += min(first_value, second_value)
                    weighted_union += max(first_value, second_value)
                if weighted_union > 0:
                    best_similarity = max(best_similarity, weighted_intersection / weighted_union)
        return best_similarity >= 0.45
