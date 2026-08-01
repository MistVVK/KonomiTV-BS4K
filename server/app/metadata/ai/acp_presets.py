"""録画シリーズ ACP の固定 CLI プリセット。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class AcpPreset:
    """KonomiTV-BS4K が管理する ACP CLI の実行定義。"""

    backend_kind: str
    display_name: str
    default_command: str
    default_args: list[str] = field(default_factory=list)


# Codex / Grok / Gemini は Docker イメージ内へ固定導入し、Web UI から command を上書きさせない。
ACP_PRESETS: dict[str, AcpPreset] = {
    'AcpCodex': AcpPreset(
        backend_kind='AcpCodex',
        display_name='ACP / Codex',
        default_command='/usr/local/bin/codex-acp',
        default_args=[],
    ),
    'AcpGrok': AcpPreset(
        backend_kind='AcpGrok',
        display_name='ACP / Grok Build',
        default_command='/usr/local/bin/grok',
        default_args=['agent', 'stdio'],
    ),
    'AcpGemini': AcpPreset(
        backend_kind='AcpGemini',
        display_name='ACP / Gemini CLI',
        default_command='/usr/local/bin/gemini',
        default_args=['--acp'],
    ),
}


def get_preset(backend_kind: str) -> AcpPreset | None:
    """バックエンド種別に対応するプリセットを取得する。

    Args:
        backend_kind: ``RecordedSeriesSettings.ai_backend`` の値。

    Returns:
        AcpPreset | None: 対応する固定プリセット。未定義の場合は None。
    """

    return ACP_PRESETS.get(backend_kind)


def resolve_command(
    backend_kind: str,
) -> tuple[str, list[str]]:
    """固定プリセットの command と引数を解決する。

    Args:
        backend_kind: ACP バックエンド種別。

    Returns:
        tuple[str, list[str]]: ランタイム command と固定引数。

    Raises:
        ValueError: 未定義 backend の場合。
    """

    preset = get_preset(backend_kind)
    if preset is None:
        raise ValueError('Unknown ACP backend.')

    # プリセット command はコンテナイメージ内に存在するため /host-rootfs へ変換しない。
    return preset.default_command, list(preset.default_args)
