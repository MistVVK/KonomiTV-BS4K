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


def test_client_settings_series_sort_defaults_to_episode_ascending() -> None:
    settings = ClientSettings.model_validate({})
    assert settings.video_series_sort_key == 'SeasonEpisode'
    assert settings.video_series_sort_direction == 'Asc'


@pytest.mark.parametrize('sort_key', ['SeasonEpisode', 'BroadcastDate', 'Title'])
def test_client_settings_accepts_all_series_sort_keys(sort_key: str) -> None:
    assert ClientSettings.model_validate({'video_series_sort_key': sort_key}).video_series_sort_key == sort_key


@pytest.mark.parametrize('direction', ['Asc', 'Desc'])
def test_client_settings_accepts_both_series_sort_directions(direction: str) -> None:
    settings = ClientSettings.model_validate({'video_series_sort_direction': direction})
    assert settings.video_series_sort_direction == direction


@pytest.mark.parametrize('theme', THEMES)
def test_client_settings_accepts_all_supported_themes(theme: str) -> None:
    assert ClientSettings.model_validate({'ui_theme': theme}).ui_theme == theme


def test_client_settings_rejects_unknown_theme() -> None:
    with pytest.raises(ValidationError):
        ClientSettings.model_validate({'ui_theme': 'UnknownTheme'})
