import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.metadata.CMLogoScanner import CMLogoFileMetadata, CMLogoScanner
from app.metadata.CMLogoSelector import CMLogoSelector


def CreateMetadata(image_width: int, image_height: int) -> CMLogoFileMetadata:
    """解像度互換性テスト用の.lgdメタデータを作る。"""

    return CMLogoFileMetadata(
        service_id=101,
        logo_name='NHK BS',
        file_format='AmatsukazeExtendedV1',
        logo_width=92,
        logo_height=34,
        image_width=image_width,
        image_height=image_height,
        image_x=image_width - 138,
        image_y=26,
    )


def CreateStandardMetadata() -> CMLogoFileMetadata:
    """生成元キャンバスを持たない標準AviUtlロゴを作る。"""

    return CMLogoFileMetadata(
        service_id=None,
        logo_name='手動ロゴ',
        file_format='AviUtlV0.1',
        logo_width=92,
        logo_height=34,
        image_width=None,
        image_height=None,
        image_x=100,
        image_y=26,
    )


def test_logo_resolution_must_match_the_recorded_video_canvas() -> None:
    """同じSIDでもBS4K用ロゴを1440x1080録画へ渡さない。"""

    assert CMLogoSelector._isResolutionCompatible(CreateMetadata(1440, 1080), 1440, 1080) is True
    assert CMLogoSelector._isResolutionCompatible(CreateMetadata(3840, 2160), 1440, 1080) is False


def test_unknown_recorded_resolution_keeps_backward_compatible_selection() -> None:
    """旧DBなど録画解像度が未取得の場合は従来どおり候補を保持する。"""

    assert CMLogoSelector._isResolutionCompatible(CreateMetadata(3840, 2160), None, None) is True


def test_explicitly_assigned_standard_logo_is_not_rejected_by_guessed_canvas() -> None:
    """標準ロゴの矩形だけから放送解像度を推測しない。"""

    assert CMLogoSelector._isResolutionCompatible(CreateStandardMetadata(), 3840, 2160) is True


def test_selection_drops_an_incompatible_logo_before_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    """SID候補が複数あっても入力キャンバスと一致するロゴだけを解析CLIへ渡す。"""

    logo_1440 = SimpleNamespace(path='/logos/1440.lgd')
    logo_4k = SimpleNamespace(path='/logos/4k.lgd')
    metadata = {
        '1440.lgd': CreateMetadata(1440, 1080),
        '4k.lgd': CreateMetadata(3840, 2160),
    }

    async def RunInline(function: object, *args: object) -> object:
        assert callable(function)
        return function(*args)

    monkeypatch.setattr(asyncio, 'to_thread', RunInline)
    monkeypatch.setattr(CMLogoScanner, 'ReadMetadata', lambda path: metadata[Path(path).name])

    selection = asyncio.run(CMLogoSelector._selected(
        (logo_1440, logo_4k),  # type: ignore[arg-type]
        None,
        1440,
        1080,
    ))

    assert selection.status == 'Selected'
    assert selection.logos == (logo_1440,)
    assert selection.runtime_paths == (Path('/logos/1440.lgd'),)
