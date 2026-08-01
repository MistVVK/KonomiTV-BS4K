import math
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import ClientSettings, ServerSettings, _ServerSettingsGeneral


@pytest.mark.parametrize(
    ('field_name', 'invalid_value'),
    [
        ('video_watched_history_max_count', 0),
        ('video_watched_history_max_count', -1),
        ('comment_font_size', 0),
        ('comment_font_size', -5),
        ('comment_speed_rate', 0),
        ('comment_speed_rate', -1.0),
        ('comment_speed_rate', math.nan),
        ('comment_speed_rate', math.inf),
        ('comment_speed_rate', -math.inf),
        ('caption_opacity', -0.1),
        ('caption_opacity', 1.1),
        ('caption_opacity', math.nan),
        ('caption_opacity', math.inf),
        ('last_synced_at', -1.0),
        ('last_synced_at', math.nan),
    ],
)
def test_client_settings_reject_out_of_range_values(field_name: str, invalid_value: object) -> None:
    """
    ClientSettings の制約 field が 0・負・NaN・Inf などを拒否することを検証する。

    Args:
        field_name (str): 検証対象フィールド名。
        invalid_value (object): 拒否されるべき値。

    Returns:
        None
    """

    with pytest.raises(ValidationError):
        ClientSettings.model_validate({field_name: invalid_value})


@pytest.mark.parametrize(
    ('field_name', 'invalid_value'),
    [
        ('encoder_bs4k_input_probesize', 0),
        ('encoder_bs4k_input_probesize', -1),
        ('encoder_bs4k_input_analyze', 0.0),
        ('encoder_bs4k_input_analyze', -0.5),
        ('encoder_bs4k_input_analyze', math.nan),
        ('encoder_bs4k_input_analyze', math.inf),
        ('encoder_bs4k_max_interleave_delta', 0),
        ('bs4k_live_startup_discard_seconds', -0.1),
        ('bs4k_live_startup_discard_seconds', math.nan),
        ('program_update_interval', 0.0),
        ('program_update_interval', -1.0),
        ('program_update_interval', math.inf),
    ],
)
def test_server_settings_general_reject_out_of_range_values(field_name: str, invalid_value: object) -> None:
    """
    ServerSettings.general の制約 field が境界外値を拒否することを検証する。

    Args:
        field_name (str): 検証対象フィールド名。
        invalid_value (object): 拒否されるべき値。

    Returns:
        None
    """

    with pytest.raises(ValidationError):
        ServerSettings.model_validate(
            {'general': {field_name: invalid_value}},
            context={'bypass_validation': True},
        )


def test_constrained_settings_accept_valid_boundary_values() -> None:
    """
    制約 field が正常範囲の値を受理することを検証する。

    Args:
        None

    Returns:
        None
    """

    client = ClientSettings.model_validate(
        {
            'last_synced_at': 0.0,
            'video_watched_history_max_count': 1,
            'caption_opacity': 0.0,
            'comment_speed_rate': 0.1,
            'comment_font_size': 1,
        },
    )
    assert client.video_watched_history_max_count == 1
    assert client.caption_opacity == 0.0

    server = ServerSettings.model_validate(
        {
            'general': {
                'encoder_bs4k_input_probesize': 1,
                'encoder_bs4k_input_analyze': 0.1,
                'encoder_bs4k_max_interleave_delta': 1,
                'bs4k_live_startup_discard_seconds': 0.0,
                'program_update_interval': 0.1,
            },
        },
        context={'bypass_validation': True},
    )
    assert server.general.program_update_interval == 0.1
    schema = ClientSettings.model_json_schema()
    assert schema['properties']['comment_font_size']['exclusiveMinimum'] == 0


@pytest.mark.parametrize(
    ('returncode', 'create_output'),
    [
        (127, False),
        (0, False),
        (127, True),
    ],
)
def test_hardware_encoder_validator_rejects_failed_ffmpeg8_probe(
    returncode: int,
    create_output: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非0終了または出力ファイルを生成しない FFmpeg 8 能力検査を拒否する。"""

    def Run(command: list[str], **_kwargs) -> subprocess.CompletedProcess[bytes]:
        if create_output is True:
            Path(command[-1]).write_bytes(b'probe')
        return subprocess.CompletedProcess(command, returncode, b'', b'probe failed')

    monkeypatch.setattr('app.config.subprocess.run', Run)

    with pytest.raises(ValueError):
        _ServerSettingsGeneral._validate_encoder_value('QSV')


def test_hardware_encoder_validator_accepts_successful_capability_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H.264/H.265 実出力と正常な FFmpeg 8 version を返す HW encoder は受理する。"""

    def Run(command: list[str], **_kwargs) -> subprocess.CompletedProcess[bytes]:
        if command[-1] == '-version':
            return subprocess.CompletedProcess(command, 0, b'ffmpeg version n8.1.2\n', b'')
        Path(command[-1]).write_bytes(b'probe')
        return subprocess.CompletedProcess(command, 0, b'', b'')

    monkeypatch.setattr('app.config.subprocess.run', Run)

    assert _ServerSettingsGeneral._validate_encoder_value('QSV') == 'QSV'


def test_hardware_encoder_validator_rejects_probe_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """応答しないHW encoderを設定時に受理しない。"""

    def Run(command: list[str], **_kwargs) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(command, 10)

    monkeypatch.setattr('app.config.subprocess.run', Run)

    with pytest.raises(ValueError):
        _ServerSettingsGeneral._validate_encoder_value('QSV')


@pytest.mark.parametrize(
    ('legacy_encoder', 'canonical_encoder'),
    [
        ('QSVEncC', 'QSV'),
        ('NVEncC', 'NVENC'),
        ('VCEEncC', 'AMF'),
    ],
)
def test_legacy_encoder_names_are_normalized_before_validation(
    legacy_encoder: str,
    canonical_encoder: str,
) -> None:
    """旧設定名を読み込み境界で現在の正規識別子へ変換する。"""

    settings = _ServerSettingsGeneral.model_validate(
        {
            'encoder': legacy_encoder,
            'encoder_bs4k': legacy_encoder,
        },
        context={'bypass_validation': True},
    )

    assert settings.encoder == canonical_encoder
    assert settings.encoder_bs4k == canonical_encoder
