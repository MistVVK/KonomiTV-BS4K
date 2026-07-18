#!/usr/bin/env python3

from __future__ import annotations

import argparse
import email
import re
import subprocess
from pathlib import Path


LICENSE_PATTERN = re.compile(r'^(?:licen[cs]e|copying|notices?|copyright|third[-_ ]party)(?:$|[._ -])', re.IGNORECASE)
COMMON_LICENSE_PATTERN = re.compile(r'/usr/share/common-licenses/([A-Za-z0-9.+-]+)')
COMMON_LICENSE_ALIASES = {
    'GFDL-3': 'GFDL-1.3',
    'GPL-1.0': 'GPL-1',
    'GPL-2.0': 'GPL-2',
    'GPL-3.0': 'GPL-3',
    'LGPL-2.0': 'LGPL-2',
    'LGPL-3.0': 'LGPL-3',
}
REJECTED_SUFFIXES = {'.py', '.pyc', '.pyo', '.so', '.a', '.o', '.class'}
MAX_SIZE = 4 * 1024 * 1024
MIT_TEXT = '''Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.'''


def readText(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_SIZE:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    for encoding in ('utf-8-sig', 'cp932', 'latin-1'):
        try:
            return data.decode(encoding).replace('\r\n', '\n').strip()
        except UnicodeDecodeError:
            pass
    return None


def licenseFiles(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob('*') if path.is_file() and path.suffix.lower() not in REJECTED_SUFFIXES and LICENSE_PATTERN.match(path.name))


def isLicenseFile(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() not in REJECTED_SUFFIXES and LICENSE_PATTERN.match(path.name) is not None


def isPackagePayload(path: Path) -> bool:
    if not path.is_file():
        return False
    value = str(path)
    if value.startswith(('/usr/share/doc/', '/usr/share/lintian/')):
        return False
    # ROCm の依存専用 metapackage が持つバージョン印は実行コードやデータではない。
    if '/.info/version-' in value:
        return False
    return True


def appendLicense(lines: list[str], title: str, path: Path) -> bool:
    content = readText(path)
    if not content:
        return False
    lines.extend([f'### {title}', '', '````text', content, '````', ''])
    return True


def referencedCommonLicenses(documents: list[Path]) -> list[Path]:
    referenced: list[Path] = []
    for document in documents:
        for name in COMMON_LICENSE_PATTERN.findall(readText(document) or ''):
            name = name.rstrip('.,;:)')
            name = COMMON_LICENSE_ALIASES.get(name, name)
            path = Path('/usr/share/common-licenses') / name
            if not path.is_file():
                raise RuntimeError(f'Referenced common license document does not exist: {path}')
            referenced.append(path)
    return list(dict.fromkeys(referenced))


def dpkgPackages() -> list[tuple[str, str]]:
    result = subprocess.run(
        ['dpkg-query', '--show', '--showformat=${binary:Package}\t${Version}\n'],
        check=True, capture_output=True, text=True,
    )
    return sorted(tuple(line.split('\t', 1)) for line in result.stdout.splitlines() if '\t' in line)


def dpkgPackageFiles(package: str) -> list[Path]:
    result = subprocess.run(['dpkg-query', '--listfiles', package], check=True, capture_output=True, text=True)
    return [Path(line) for line in result.stdout.splitlines() if line.startswith('/')]


def pythonPackages(root: Path) -> list[tuple[str, str, str, list[Path]]]:
    packages = []
    for metadataPath in sorted(root.rglob('*.dist-info/METADATA')):
        metadata = email.message_from_string(metadataPath.read_text(encoding='utf-8', errors='replace'))
        expression = metadata.get('License-Expression') or metadata.get('License') or 'not declared'
        if expression == 'not declared':
            classifiers = [value.removeprefix('License :: ').strip() for value in metadata.get_all('Classifier', []) if value.startswith('License :: ')]
            expression = '; '.join(classifiers) or expression
        packageRoot = metadataPath.parent.parent
        local = [path for path in licenseFiles(metadataPath.parent) if path != metadataPath]
        packages.append((metadata.get('Name', metadataPath.parent.name), metadata.get('Version', 'unknown'), expression, local or licenseFiles(packageRoot / metadataPath.parent.name.removesuffix('.dist-info'))))
    return packages


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--root', type=Path, action='append', default=[])
    parser.add_argument('--python-root', type=Path, action='append', default=[])
    parser.add_argument('--go-module-cache', type=Path)
    parser.add_argument('--dpkg', action='store_true')
    parser.add_argument('--dpkg-prefix', action='append', default=[])
    parser.add_argument('--dpkg-exclude', action='append', default=[])
    args = parser.parse_args()

    lines = [f'# License manifest: {args.stage}', '']
    if args.dpkg:
        packages = dpkgPackages()
        if args.dpkg_prefix:
            packages = [item for item in packages if any(item[0].startswith(prefix) for prefix in args.dpkg_prefix)]
        packages = [item for item in packages if item[0].split(':')[0] not in args.dpkg_exclude]
        lines.extend(['# OS and GPU Package Licenses', ''])
        for name, version in packages:
            package = name.split(':')[0]
            package_files = dpkgPackageFiles(name)
            documents = [path for path in package_files if isLicenseFile(path)]
            if not documents:
                default_document = Path('/usr/share/doc') / package / 'copyright'
                if default_document.is_file():
                    documents = [default_document]
            payload = [path for path in package_files if isPackagePayload(path)]
            if not documents and not payload:
                continue
            if not documents:
                raise RuntimeError(f'No license document found for package with payload: {name} {version}')
            lines.extend([f'## {name} {version}', ''])
            documents = list(dict.fromkeys(documents))
            for path in [*documents, *referencedCommonLicenses(documents)]:
                appendLicense(lines, path.name, path)

    for root in args.root:
        lines.extend(['# Additional Runtime License Documents', ''])
        for path in licenseFiles(root):
            lines.extend([f'## {path.parent.name}: {path.name}', ''])
            appendLicense(lines, path.name, path)

    if args.go_module_cache and args.go_module_cache.exists():
        lines.extend(['# Statically Linked Go Module Licenses', ''])
        for path in licenseFiles(args.go_module_cache):
            lines.extend([f'## {path.parent.name}', ''])
            appendLicense(lines, path.name, path)

    for root in args.python_root:
        lines.extend(['# Python Package Licenses', ''])
        packages = pythonPackages(root)
        for name, version, declaredLicense, paths in packages:
            lines.extend([f'## {name} {version}', '', f'- Declared license: {declaredLicense}', ''])
            if not paths:
                lines.extend(['The installed wheel does not contain a standalone license file. The complete standard license text declared by its package metadata follows.', ''])
                if 'MIT' in declaredLicense:
                    lines.extend(['#### Standard MIT license text', '', '````text', MIT_TEXT, '````', ''])
                else:
                    raise RuntimeError(f'No full license text available for Python package: {name} {version} ({declaredLicense})')
            for path in paths:
                appendLicense(lines, path.name, path)

    args.output.write_text('\n'.join(lines).rstrip() + '\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
