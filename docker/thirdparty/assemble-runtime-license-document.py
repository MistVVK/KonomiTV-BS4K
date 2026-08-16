#!/usr/bin/env python3

from __future__ import annotations

import argparse
import re
from pathlib import Path


NONFREE_WARNING_START = '<!-- NONFREE_RUNTIME_WARNING_START -->'
NONFREE_WARNING_END = '<!-- NONFREE_RUNTIME_WARNING_END -->'
INTEL_NONFREE_WARNING_START = '<!-- INTEL_NONFREE_WARNING_START -->'
INTEL_NONFREE_WARNING_END = '<!-- INTEL_NONFREE_WARNING_END -->'
AMD_NONFREE_WARNING_START = '<!-- AMD_NONFREE_WARNING_START -->'
AMD_NONFREE_WARNING_END = '<!-- AMD_NONFREE_WARNING_END -->'
NONFREE_PROFILES = ('nonfree', 'free', 'intel-nonfree', 'amd-nonfree')
DOCUMENT_TITLE = '# Third-Party Software Licenses'
CONTROL_CHARACTERS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
ATX_HEADING = re.compile(r'^(#{1,6})[ \t]+(.+?)\s*$')
FENCE_OPEN = re.compile(r'^ {0,3}(`{3,}|~{3,})')


def readDocument(path: Path) -> str:
    document = path.read_text(encoding='utf-8')
    if '\ufffd' in document:
        raise ValueError(f'Document contains a Unicode replacement character: {path}')

    # GNU ライセンス原文などでは U+000C が印刷時の改ページとして使われる。
    # HTML/Markdown に安全に埋め込めるよう改行へ正規化し、意味のある本文は変更しない。
    document = document.replace('\f', '\n')
    control_character = CONTROL_CHARACTERS.search(document)
    if control_character is not None:
        offset = control_character.start()
        line = document.count('\n', 0, offset) + 1
        line_start = document.rfind('\n', 0, offset) + 1
        column = offset - line_start + 1
        codepoint = ord(control_character.group())
        raise ValueError(
            'Document contains an unsupported control character '
            f'U+{codepoint:04X} at line {line}, column {column}: {path}'
        )
    return document


def DocumentHeadings(document: str) -> list[tuple[int, str]]:
    """
    コードフェンス内を除外して Markdown の ATX 見出しを抽出する。

    Args:
        document (str): 検査する Markdown 文書

    Returns:
        list[tuple[int, str]]: 見出しレベルと表示テキストの組
    """

    headings: list[tuple[int, str]] = []
    fence_character: str | None = None
    fence_length = 0
    for line in document.splitlines():
        if fence_character is not None:
            closing_fence = re.match(
                rf'^ {{0,3}}{re.escape(fence_character)}{{{fence_length},}}[ \t]*$',
                line,
            )
            if closing_fence is not None:
                fence_character = None
                fence_length = 0
            continue

        opening_fence = FENCE_OPEN.match(line)
        if opening_fence is not None:
            marker = opening_fence.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            continue

        heading = ATX_HEADING.match(line)
        if heading is not None:
            title = re.sub(r'[ \t]+#+[ \t]*$', '', heading.group(2)).rstrip()
            headings.append((len(heading.group(1)), title))
    return headings


def ValidateDocumentHierarchy(path: Path, document: str, *, contains_title: bool) -> None:
    """
    結合対象の見出しが H1 から H5 までの所定階層を飛ばさず構成されているか検査する。

    Args:
        path (Path): エラー表示に用いる文書パス
        document (str): 検査する Markdown 文書
        contains_title (bool): 文書全体の H1 を持つ基礎文書なら True

    Returns:
        None.
    """

    headings = DocumentHeadings(document)
    if not headings:
        raise ValueError(f'Document does not contain any Markdown headings: {path}')
    if contains_title:
        if headings[0] != (1, DOCUMENT_TITLE.removeprefix('# ')):
            raise ValueError(f'Base document must start with {DOCUMENT_TITLE!r}: {path}')
        if sum(level == 1 for level, _ in headings) != 1:
            raise ValueError(f'Base document must contain exactly one H1 heading: {path}')
    elif headings[0][0] != 2 or any(level == 1 for level, _ in headings):
        raise ValueError(f'Document fragment must start with H2 and must not contain H1: {path}')

    previous_level = headings[0][0]
    for level, title in headings[1:]:
        if level > 5:
            raise ValueError(f'Heading level H{level} is deeper than the H5 license-material level: {path}')
        if level > previous_level + 1:
            raise ValueError(
                f'Heading hierarchy jumps from H{previous_level} to H{level} before {title!r}: {path}'
            )
        previous_level = level


def AssembleNonfreeWarning(base_document: str, profile: str) -> tuple[str, int]:
    """
    基礎文書の警告ブロックをプロファイルに合わせて組み立て直す。

    警告ブロックは見出しを共通にして、Intel Full Feature 節と AMD proprietary 節だけを
    プロファイルに応じて出し分ける。マーカーは完成文書へ残さずすべて除去する。

    Args:
        base_document (str): 警告ブロックを含む基礎文書
        profile (str): NONFREE_PROFILES のいずれかのプロファイル名

    Returns:
        tuple[str, int]: 警告ブロックを置換した文書と、警告ブロック末尾の文字位置
    """

    outer_start = base_document.index(NONFREE_WARNING_START)
    outer_end_marker_start = base_document.index(NONFREE_WARNING_END)
    outer_end = outer_end_marker_start + len(NONFREE_WARNING_END)
    if profile == 'free':
        # free プロファイルはブロックごと除去し、プロファイルカードを文書タイトルの直後へ置く
        base_document = base_document[:outer_start] + base_document[outer_end:]
        if not base_document.startswith(DOCUMENT_TITLE + '\n'):
            raise ValueError(f'Base document must start with {DOCUMENT_TITLE!r}.')
        return base_document, len(DOCUMENT_TITLE)

    # 基礎文書は「見出し / Intel 節 / AMD 節 / 共通注記」の順で全節を持ち、
    # 空の ">" 行（段落区切り）を節の外側に置くことで、必要な節だけを正しく連結できるようにする。
    intel_start = base_document.index(INTEL_NONFREE_WARNING_START)
    intel_end_marker_start = base_document.index(INTEL_NONFREE_WARNING_END)
    intel_end = intel_end_marker_start + len(INTEL_NONFREE_WARNING_END)
    amd_start = base_document.index(AMD_NONFREE_WARNING_START)
    amd_end_marker_start = base_document.index(AMD_NONFREE_WARNING_END)
    amd_end = amd_end_marker_start + len(AMD_NONFREE_WARNING_END)

    heading_part = base_document[outer_start + len(NONFREE_WARNING_START):intel_start]
    intel_part = base_document[intel_start + len(INTEL_NONFREE_WARNING_START):intel_end_marker_start]
    between_part = base_document[intel_end:amd_start]
    amd_part = base_document[amd_start + len(AMD_NONFREE_WARNING_START):amd_end_marker_start]
    tail_part = base_document[amd_end:outer_end_marker_start]

    include_intel = profile in ('nonfree', 'intel-nonfree')
    include_amd = profile in ('nonfree', 'amd-nonfree')
    sections = [heading_part.strip('\n')]
    if include_intel:
        sections.append(intel_part.strip('\n'))
        # Intel 節と AMD 節の間の区切り行は、両節とも残る場合だけ必要
        if include_amd:
            sections.append(between_part.strip('\n'))
    if include_amd:
        sections.append(amd_part.strip('\n'))
    sections.append(tail_part.strip('\n'))
    assembled_block = '\n'.join(sections)
    base_document = base_document[:outer_start] + assembled_block + base_document[outer_end:]
    return base_document, outer_start + len(assembled_block)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--client', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, action='append', default=[])
    parser.add_argument('--cuda-version', required=True)
    parser.add_argument('--nonfree-profile', required=True, choices=NONFREE_PROFILES)
    args = parser.parse_args()

    base_document = readDocument(args.base)
    ValidateDocumentHierarchy(args.base, base_document, contains_title=True)
    client_document = readDocument(args.client)
    ValidateDocumentHierarchy(args.client, client_document, contains_title=False)
    manifest_documents: list[tuple[Path, str]] = []
    for manifest in args.manifest:
        manifest_document = readDocument(manifest)
        ValidateDocumentHierarchy(manifest, manifest_document, contains_title=False)
        manifest_documents.append((manifest, manifest_document))
    cuda_version = args.cuda_version.replace('-', '.')
    profile = args.nonfree_profile
    if profile in ('nonfree', 'intel-nonfree'):
        intel_media_driver = 'Full Feature iHD (`ENABLE_NONFREE_KERNELS=ON`)'
    else:
        intel_media_driver = 'Free Kernel iHD (`ENABLE_NONFREE_KERNELS=OFF`)'
    if profile in ('nonfree', 'amd-nonfree'):
        amd_media_runtime = 'AMD proprietary runtime'
    else:
        amd_media_runtime = 'Mesa'
    target_document = '\n'.join([
        '> **Docker image build profile**',
        '>',
        f'> - Profile: `cuda{cuda_version}-{profile}`',
        f'> - CUDA package series: `{cuda_version}`',
        f'> - Intel media driver: {intel_media_driver}',
        f'> - AMD media runtime: {amd_media_runtime}',
        '> - NVIDIA driver libraries are supplied by the host at runtime and are not included in this image.',
    ])

    # 非自由コンポーネントを含む場合は、再配布警告を読んだ直後にビルド条件を確認できるよう警告全体の直後へ配置する
    # 警告ブロックはプロファイルに合わせて節を出し分けてから、プロファイルカードの挿入位置をその直後に決める
    base_document, target_insertion_point = AssembleNonfreeWarning(base_document, profile)
    base_document = (
        base_document[:target_insertion_point].rstrip() + '\n\n' + target_document + '\n\n' +
        base_document[target_insertion_point:].lstrip()
    )

    lines = [base_document.rstrip(), '', client_document.rstrip(), '']
    for _, manifest_document in manifest_documents:
        lines.extend([manifest_document.rstrip(), ''])

    output_document = '\n'.join(lines).rstrip() + '\n'
    ValidateDocumentHierarchy(args.output, output_document, contains_title=True)
    args.output.write_text(output_document, encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
