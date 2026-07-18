#!/usr/bin/env python3

from __future__ import annotations

import argparse
import re
from pathlib import Path


NONFREE_WARNING_START = '<!-- NONFREE_RUNTIME_WARNING_START -->'
NONFREE_WARNING_END = '<!-- NONFREE_RUNTIME_WARNING_END -->'
DOCUMENT_TITLE = '# Third-Party Software Licenses'
CONTROL_CHARACTERS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def readDocument(path: Path) -> str:
    return CONTROL_CHARACTERS.sub('', path.read_text(encoding='utf-8', errors='replace'))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--client', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, action='append', default=[])
    parser.add_argument('--cuda-version', required=True)
    parser.add_argument('--chromium-version', required=True)
    parser.add_argument('--chromium-copyright', type=Path, required=True)
    parser.add_argument('--include-nonfree-runtime', action='store_true')
    args = parser.parse_args()

    base_document = readDocument(args.base)
    base_document = base_document.replace('@CHROMIUM_VERSION@', args.chromium_version)
    base_document = base_document.replace('@CHROMIUM_COPYRIGHT@', readDocument(args.chromium_copyright).strip())
    cuda_version = args.cuda_version.replace('-', '.')
    target = f'cuda{cuda_version}-{"nonfree" if args.include_nonfree_runtime else "free"}'
    if args.include_nonfree_runtime:
        intel_media_driver = 'Full Feature iHD (`ENABLE_NONFREE_KERNELS=ON`)'
        amd_media_runtime = 'AMD proprietary runtime'
    else:
        intel_media_driver = 'Free Kernel iHD (`ENABLE_NONFREE_KERNELS=OFF`)'
        amd_media_runtime = 'Mesa'
    target_document = '\n'.join([
        '> **Docker build target**',
        '>',
        f'> - Target: `{target}`',
        f'> - CUDA package series: `{cuda_version}`',
        f'> - NONFREE runtime included: `{str(args.include_nonfree_runtime).lower()}`',
        f'> - Intel media driver: {intel_media_driver}',
        f'> - AMD media runtime: {amd_media_runtime}',
        '> - NVIDIA driver libraries are supplied by the host at runtime and are not included in this image.',
    ])

    # NONFREE runtime を含む場合は、再配布警告を読んだ直後にビルド条件を確認できるよう警告全体の直後へ配置する
    # NONFREE runtime を含まない場合は警告自体を除去し、文書タイトルの直後へ同じカードを配置する
    if args.include_nonfree_runtime:
        target_insertion_point = base_document.index(NONFREE_WARNING_END) + len(NONFREE_WARNING_END)
    else:
        warning_start = base_document.index(NONFREE_WARNING_START)
        warning_end = base_document.index(NONFREE_WARNING_END) + len(NONFREE_WARNING_END)
        base_document = base_document[:warning_start] + base_document[warning_end:]
        if not base_document.startswith(DOCUMENT_TITLE + '\n'):
            raise ValueError(f'Base document must start with {DOCUMENT_TITLE!r}.')
        target_insertion_point = len(DOCUMENT_TITLE)
    base_document = (
        base_document[:target_insertion_point].rstrip() + '\n\n' + target_document + '\n\n' +
        base_document[target_insertion_point:].lstrip()
    )

    lines = [base_document.rstrip(), '', readDocument(args.client).rstrip(), '']
    for manifest in args.manifest:
        lines.extend([readDocument(manifest).rstrip(), ''])

    args.output.write_text('\n'.join(lines).rstrip() + '\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
