import pytest
from pydantic import ValidationError

from app.config import ClientSettings


THEMES = [
    'KonomiClassic',
    'KonomiNavy',
    'KonomiCharcoal',
    'DeepPlum',
    'NightBlue',
    'DayBlue',
    'KonomiIvory',
    'PearlBlue',
    'WarmCream',
    'CoolGray',
]


def test_client_settings_theme_defaults_to_konomi_classic() -> None:
    assert ClientSettings().ui_theme == 'KonomiClassic'


def test_client_settings_jikkyo_defaults_to_disabled() -> None:
    assert ClientSettings().jikkyo_enabled is False
    assert ClientSettings.model_validate({}).jikkyo_enabled is False


def test_client_settings_oneseg_channels_default_to_visible() -> None:
    assert ClientSettings().show_oneseg_channels is True
    assert ClientSettings.model_validate({}).show_oneseg_channels is True


@pytest.mark.parametrize('theme', THEMES)
def test_client_settings_accepts_all_supported_themes(theme: str) -> None:
    assert ClientSettings.model_validate({'ui_theme': theme}).ui_theme == theme


def test_client_settings_rejects_unknown_theme() -> None:
    with pytest.raises(ValidationError):
        ClientSettings.model_validate({'ui_theme': 'UnknownTheme'})
