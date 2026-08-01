#!/usr/bin/env python3
"""ACP Runtime Dependencies セクションを最終ライセンス文書向けに組み立てる。

npm package 文書へ Node.js LICENSE を結合する。Grok は公式 npm の main package と
platform package を lockfile の integrity 付きで収集するため、個別資料を推測で結合しない。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


CONTROL_CHARACTERS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def read_text(path: Path) -> str:
    text = path.read_text(encoding='utf-8')
    if '\ufffd' in text:
        raise SystemExit(f'Unicode replacement character in {path}')
    text = text.replace('\f', '\n')
    if CONTROL_CHARACTERS.search(text):
        raise SystemExit(f'Unsupported control character in {path}')
    return text.replace('\r\n', '\n').strip() + '\n'


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--npm-document', type=Path, required=True)
    parser.add_argument('--node-license', type=Path, required=True)
    parser.add_argument('--node-version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()

    npm_document = read_text(args.npm_document)
    if not npm_document.startswith('## ACP Runtime Dependencies\n'):
        raise SystemExit('npm document must start with H2 "ACP Runtime Dependencies"')

    node_license = read_text(args.node_license)
    if 'Node.js' not in node_license and 'node.js' not in node_license.lower():
        # Node 公式 LICENSE は "Node.js is licensed for use as follows" で始まる。
        if 'Permission is hereby granted' not in node_license and 'Apache' not in node_license:
            raise SystemExit(f'Node LICENSE does not look like a Node.js license file: {args.node_license}')

    node_version = args.node_version.lstrip('v')
    if node_version != '20.16.0':
        raise SystemExit(f'Unexpected Node.js version for license section: {args.node_version!r}')

    sections = [npm_document.rstrip(), '']

    sections.extend([
        '### Node.js 20.16.0',
        '',
        '- Component: `Node.js`',
        '- Version: `20.16.0`',
        '- Binary path: `/usr/local/bin/node`',
        '- License source: official Node.js distribution LICENSE (`/usr/local/LICENSE` in node:20.16.0)',
        '',
        '#### LICENSE',
        '',
        '```text',
        node_license.rstrip(),
        '```',
        '',
    ])

    output = '\n'.join(sections).rstrip() + '\n'
    if not output.startswith('## ACP Runtime Dependencies\n'):
        raise SystemExit('Assembled document must start with H2 ACP Runtime Dependencies')
    # H1 を含めない（assemble-runtime-license-document の fragment 規約）。
    # LICENSE / NOTICE 本文内の `#`（コードフェンス内）は構造見出しではないので除外する。
    outside_fences: list[str] = []
    in_fence = False
    for line in output.splitlines():
        if line.startswith('```'):
            in_fence = not in_fence
            continue
        if not in_fence:
            outside_fences.append(line)
    structure_text = '\n'.join(outside_fences) + '\n'
    if re.search(r'^# ', structure_text, re.MULTILINE):
        raise SystemExit('ACP license fragment must not contain H1 headings outside license bodies')

    args.output.write_text(output, encoding='utf-8', newline='\n')
    print(f'Wrote ACP license section: {args.output}', file=sys.stderr)


if __name__ == '__main__':
    main()
