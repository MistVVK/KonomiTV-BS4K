import math
import subprocess
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.config import ClientSettings, ServerSettings, _ServerSettingsGeneral


class _FakeMirakurunResponse:
    """設定検証へ返す Mirakurun API 応答の代替。"""

    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = {'server': 'Mirakurun/4.1.0'}

    def json(self) -> object:
        """構築時に指定された JSON payload を返す。"""

        return self._payload


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


def test_tlv_transport_requires_dedicated_mirakurun_url() -> None:
    """BS4K TLV を選択した場合は専用 Mirakurun URL を必須にする。"""

    with pytest.raises(ValidationError, match='TLV 専用 Mirakurun'):
        _ServerSettingsGeneral.model_validate({
            'backend': 'Mirakurun',
            'konomitv_bs4k_live_transport': 'Tlv',
        })


def test_tlv_transport_accepts_edcb_for_non_bs4k_live_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """BS4K だけを専用 TLV 経路にして、ほかのライブは EDCB から受信できる。"""

    def Get(*, url: str, **_kwargs: object) -> _FakeMirakurunResponse:
        if url.endswith('/api/services'):
            return _FakeMirakurunResponse([{
                'networkId': 0x000B,
                'serviceId': 101,
                'channel': {'type': 'BS4K', 'channel': '45328'},
            }])
        return _FakeMirakurunResponse([{'types': ['BS4K']}])

    monkeypatch.setattr('app.config.httpx.get', Get)
    settings = _ServerSettingsGeneral.model_validate({
        'backend': 'EDCB',
        'always_receive_tv_from_mirakurun': False,
        'konomitv_bs4k_live_transport': 'Tlv',
        'konomitv_bs4k_tlv_mirakurun_url': 'http://tlv.invalid',
    })

    assert settings.live_stream_backend == 'EDCB'
    assert settings.konomitv_bs4k_live_transport == 'Tlv'


def test_tlv_mirakurun_validation_checks_tuners_and_bs4k_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """専用 URL の tuner 一覧と BS4K service を検証し、末尾 slash を正規化する。"""

    requested_urls: list[str] = []

    def Get(*, url: str, **_kwargs: object) -> _FakeMirakurunResponse:
        requested_urls.append(url)
        if url.endswith('/api/services'):
            return _FakeMirakurunResponse([{
                'networkId': 0x000B,
                'serviceId': 101,
                'channel': {'type': 'BS4K', 'channel': '45328'},
            }])
        return _FakeMirakurunResponse([{'types': ['BS4K']}])

    monkeypatch.setattr('app.config.httpx.get', Get)
    settings = _ServerSettingsGeneral.model_validate({
        'backend': 'Mirakurun',
        'mirakurun_url': 'http://metadata.invalid',
        'konomitv_bs4k_live_transport': 'Tlv',
        'konomitv_bs4k_tlv_mirakurun_url': 'http://tlv.invalid/base',
    })

    assert str(settings.konomitv_bs4k_tlv_mirakurun_url) == 'http://tlv.invalid/base/'
    assert 'http://tlv.invalid/base/api/tuners' in requested_urls
    assert 'http://tlv.invalid/base/api/services' in requested_urls


def test_tlv_mirakurun_temporary_outage_does_not_block_server_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BS4K 専用 TLV 入力元の一時停止中も、通常チャンネル向け設定は読み込める。"""

    def Get(**_kwargs: object) -> _FakeMirakurunResponse:
        raise httpx.ConnectError('temporarily unavailable')

    monkeypatch.setattr('app.config.httpx.get', Get)
    settings = _ServerSettingsGeneral.model_validate({
        'backend': 'EDCB',
        'always_receive_tv_from_mirakurun': False,
        'konomitv_bs4k_live_transport': 'Tlv',
        'konomitv_bs4k_tlv_mirakurun_url': 'http://tlv.invalid',
    })

    assert settings.live_stream_backend == 'EDCB'
    assert settings.konomitv_bs4k_live_transport == 'Tlv'


def test_tlv_mirakurun_validation_rejects_inventory_without_bs4k(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API が正常でも networkId 0x000B の service がなければ拒否する。"""

    def Get(*, url: str, **_kwargs: object) -> _FakeMirakurunResponse:
        if url.endswith('/api/services'):
            return _FakeMirakurunResponse([{'networkId': 0x0004, 'serviceId': 101}])
        return _FakeMirakurunResponse([{'types': ['BS']}])

    monkeypatch.setattr('app.config.httpx.get', Get)
    with pytest.raises(ValidationError, match='BS4K サービス'):
        _ServerSettingsGeneral.model_validate({
            'backend': 'Mirakurun',
            'mirakurun_url': 'http://metadata.invalid',
            'konomitv_bs4k_live_transport': 'Tlv',
            'konomitv_bs4k_tlv_mirakurun_url': 'http://tlv.invalid',
        })


def test_tlv_mirakurun_validation_rejects_bs4k_service_without_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BS4K service があっても Channel Stream API 用情報がなければ拒否する。"""

    def Get(*, url: str, **_kwargs: object) -> _FakeMirakurunResponse:
        if url.endswith('/api/services'):
            return _FakeMirakurunResponse([{'networkId': 0x000B, 'serviceId': 101}])
        return _FakeMirakurunResponse([{'types': ['BS4K']}])

    monkeypatch.setattr('app.config.httpx.get', Get)
    with pytest.raises(ValidationError, match='Channel Stream API'):
        _ServerSettingsGeneral.model_validate({
            'backend': 'Mirakurun',
            'konomitv_bs4k_live_transport': 'Tlv',
            'konomitv_bs4k_tlv_mirakurun_url': 'http://tlv.invalid',
        })


def test_tlv_mirakurun_validation_hides_url_on_network_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """専用 URL の接続失敗を許容しつつ、警告ログへ URL や認証情報を混入させない。"""

    calls = 0

    def Get(*, url: str, **_kwargs: object) -> _FakeMirakurunResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _FakeMirakurunResponse([{'types': ['BS']}])
        raise httpx.ConnectError('secret-user:secret-password@tlv.invalid')

    monkeypatch.setattr('app.config.httpx.get', Get)
    settings = _ServerSettingsGeneral.model_validate({
        'backend': 'Mirakurun',
        'mirakurun_url': 'http://metadata.invalid',
        'konomitv_bs4k_live_transport': 'Tlv',
        'konomitv_bs4k_tlv_mirakurun_url': 'http://secret-user:secret-password@tlv.invalid',
    })

    assert settings.konomitv_bs4k_live_transport == 'Tlv'
    assert 'secret-user' not in caplog.text
    assert 'secret-password' not in caplog.text


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
