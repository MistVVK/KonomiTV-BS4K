#!/usr/bin/env python3
"""opencode-ai のライセンス断片を最終 THIRD_PARTY 文書向け H2 セクションへ整形する。"""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    """CLI エントリポイント。

    Args:
        なし（argparse 経由）。

    Returns:
        None
    """

    parser = argparse.ArgumentParser(description='Assemble OpenCode license section.')
    parser.add_argument('--license', required=True, type=Path)
    parser.add_argument('--version', required=True)
    parser.add_argument('--platform-package', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()

    license_text = args.license.read_text(encoding='utf-8').strip()
    if license_text == '':
        raise SystemExit('OpenCode LICENSE is empty.')

    lines = [
        '## OpenCode Runtime Dependencies',
        '',
        'KonomiTV-BS4K embeds a pinned Single Executable Application (SEA) binary of',
        f'opencode-ai {args.version} ({args.platform_package}). The final image copies only',
        'the executable and this license fragment; npm node_modules are not retained.',
        '',
        f'### opencode-ai {args.version}',
        '',
        '```',
        license_text,
        '```',
        '',
        f'### {args.platform_package} {args.version}',
        '',
        'Platform binary package distributed with opencode-ai. License terms follow',
        f'opencode-ai {args.version} (MIT).',
        '',
    ]
    args.output.write_text('\n'.join(lines), encoding='utf-8')


if __name__ == '__main__':
    main()
