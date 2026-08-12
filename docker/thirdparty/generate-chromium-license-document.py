#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


CHROMIUM_CREDITS_URL = 'chrome://credits/'
CHROMIUM_LICENSE_SOURCE_URL = 'https://chromium.googlesource.com/chromium/src/+/main/LICENSE'
CHROMIUM_VERSION_PATTERN = re.compile(r'^Chromium ([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)(?: .*)?$')
PACKAGE_VERSION_PATTERN = re.compile(r'^([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)(?:[~+].*)?$')
UNSAFE_CONTROL_CHARACTERS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
MAX_WEBSOCKET_MESSAGE_SIZE = 64 * 1024 * 1024
MAX_CHROMIUM_LICENSE_SIZE = 1024 * 1024
MINIMUM_BUNDLED_PROJECT_COUNT = 100
CDP_EVALUATION_TIMEOUT_SECONDS = 60.0

# Chromium 150.0.7871.124 / 150.0.7871.181 / 150.0.7871.186 / 151.0.7922.108 の
# chrome://credits に埋め込まれた一部の一次配布 notice は、
# 元の引用符と copyright sign が U+FFFD に変換された状態で収録されている。
# 別バージョンや別 project へ推測で置換を広げず、一次ソースと照合した固定文字列だけを修復する。
ENCODING_REPAIR_CHROMIUM_VERSIONS = frozenset({
    '150.0.7871.124',
    '150.0.7871.181',
    '150.0.7871.186',
    '151.0.7922.108',
})
ANDROID_NOTICE_REPAIR_PROJECTS = (
    'common',
    'core-common',
    'feature-delivery',
    'googleid',
    'play-services-auth',
    'play-services-auth-api-phone',
    'play-services-auth-base',
    'play-services-auth-blockstore',
    'play-services-cast',
    'play-services-cast-framework',
    'play-services-cloud-messaging',
    'play-services-instantapps',
    'play-services-location',
    'play-services-time',
    'review',
)
ANDROID_BROKEN_LICENSE_LINE = '    terms of the _____ license (the �[___] License�), in which case'
ANDROID_REPAIRED_LICENSE_LINE = '    terms of the _____ license (the "[___] License"), in which case'
ANDROID_BROKEN_MULTIPLE_LICENSED_LINE = (
    '    �Multiple-Licensed�.  �Multiple-Licensed� means that the Initial'
)
ANDROID_REPAIRED_MULTIPLE_LICENSED_LINE = (
    '    "Multiple-Licensed".  "Multiple-Licensed" means that the Initial'
)
FREETYPE_BROKEN_COPYRIGHT_LINE = '    Portions of this software are copyright � <year> The FreeType'
FREETYPE_REPAIRED_COPYRIGHT_LINE = '    Portions of this software are copyright © <year> The FreeType'
MOZILLA_MPL_1_1_SOURCE = 'https://www.mozilla.org/en-US/MPL/1.1/'
FREETYPE_LICENSE_SOURCE = (
    'https://chromium.googlesource.com/chromium/src/third_party/freetype2/+/'
    'b08a2eb0dd37f4a6c886fa5b0ecf5b3e1d27aac7/docs/FTL.TXT?format=TEXT'
)

# chrome://credits は Chromium のビルド時に生成され、実際に同梱された third-party コンポーネントを列挙する。
# DOM の見た目に依存せず、各 product の名称・ホームページ・ライセンス本文だけを JSON として取得する。
CREDITS_EXTRACTION_EXPRESSION = r'''JSON.stringify(
    [...document.querySelectorAll('.product')].map((project) => ({
        name: project.querySelector(':scope > .title')?.textContent.trim() ?? '',
        homepage: project.querySelector(':scope > .homepage a')?.href ?? '',
        license: project.querySelector(':scope > .license')?.textContent.trim() ?? '',
    })),
)'''


class WebSocketClient:
    """Chromium DevTools Protocol に必要な最小限の WebSocket クライアント。"""

    def __init__(self, url: str) -> None:
        """WebSocket 接続を確立する。

        Args:
            url: Chromium が返した loopback 上の DevTools WebSocket URL。
        """

        parsed_url = urllib.parse.urlsplit(url)
        if parsed_url.scheme != 'ws' or parsed_url.hostname not in {'127.0.0.1', 'localhost'}:
            raise ValueError(f'Unexpected DevTools WebSocket URL: {url!r}')
        if parsed_url.port is None:
            raise ValueError(f'DevTools WebSocket URL does not contain a port: {url!r}')

        # Chrome 自身が通知した loopback endpoint 以外には接続しない。
        self.socket = socket.create_connection((parsed_url.hostname, parsed_url.port), timeout=15.0)
        self.socket.settimeout(15.0)
        websocket_key = base64.b64encode(os.urandom(16)).decode('ascii')
        path = urllib.parse.urlunsplit(('', '', parsed_url.path or '/', parsed_url.query, ''))
        request = (
            f'GET {path} HTTP/1.1\r\n'
            f'Host: {parsed_url.hostname}:{parsed_url.port}\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {websocket_key}\r\n'
            'Sec-WebSocket-Version: 13\r\n'
            '\r\n'
        ).encode('ascii')
        self.socket.sendall(request)

        # HTTP Upgrade 応答に後続フレームが混ざる前にヘッダーだけを厳密に検証する。
        response = bytearray()
        while b'\r\n\r\n' not in response:
            response.extend(self.socket.recv(4096))
            if len(response) > 64 * 1024:
                raise RuntimeError('DevTools WebSocket handshake response is unexpectedly large.')
        header, buffered_payload = bytes(response).split(b'\r\n\r\n', maxsplit=1)
        if buffered_payload:
            raise RuntimeError('DevTools sent a WebSocket frame before the handshake completed.')
        header_lines = header.decode('iso-8859-1').split('\r\n')
        if not re.fullmatch(r'HTTP/1\.[01] 101(?: .*)?', header_lines[0]):
            raise RuntimeError(f'DevTools WebSocket handshake failed: {header_lines[0]}')
        response_headers = {
            name.strip().lower(): value.strip()
            for line in header_lines[1:]
            if ':' in line
            for name, value in [line.split(':', maxsplit=1)]
        }
        expected_accept = base64.b64encode(hashlib.sha1(
            (websocket_key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('ascii'),
        ).digest()).decode('ascii')
        if response_headers.get('sec-websocket-accept') != expected_accept:
            raise RuntimeError('DevTools WebSocket handshake returned an invalid accept key.')

    def close(self) -> None:
        """WebSocket 接続を閉じる。

        Returns:
            None.
        """

        self.socket.close()

    def sendText(self, text: str) -> None:
        """UTF-8 のテキストメッセージを送信する。

        Args:
            text: 送信する JSON テキスト。

        Returns:
            None.
        """

        self._sendFrame(0x1, text.encode('utf-8'))

    def receiveText(self, deadline: float) -> str:
        """断片化を考慮して1件のテキストメッセージを受信する。

        Args:
            deadline: monotonic clock による受信期限。

        Returns:
            受信した UTF-8 テキスト。
        """

        fragments: list[bytes] = []
        message_opcode: int | None = None
        total_size = 0
        while True:
            self.socket.settimeout(max(0.001, deadline - time.monotonic()))
            first_byte, second_byte = self._receiveExactly(2)
            if first_byte & 0x70:
                raise RuntimeError('DevTools returned a WebSocket frame with an unsupported extension bit.')
            final = bool(first_byte & 0x80)
            opcode = first_byte & 0x0f
            masked = bool(second_byte & 0x80)
            payload_length = second_byte & 0x7f
            if payload_length == 126:
                payload_length = struct.unpack('!H', self._receiveExactly(2))[0]
            elif payload_length == 127:
                payload_length = struct.unpack('!Q', self._receiveExactly(8))[0]
            if payload_length > MAX_WEBSOCKET_MESSAGE_SIZE:
                raise RuntimeError('DevTools WebSocket frame is unexpectedly large.')
            mask = self._receiveExactly(4) if masked else b''
            payload = self._receiveExactly(payload_length)
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))

            # ping へ応答し、非同期イベントを運ぶ接続を維持する。
            if opcode == 0x9:
                if not final or payload_length > 125:
                    raise RuntimeError('DevTools returned an invalid WebSocket ping frame.')
                self._sendFrame(0xA, payload)
                continue
            if opcode == 0xA:
                if not final or payload_length > 125:
                    raise RuntimeError('DevTools returned an invalid WebSocket pong frame.')
                continue
            if opcode == 0x8:
                raise RuntimeError('DevTools closed the WebSocket connection unexpectedly.')
            if opcode in {0x1, 0x2}:
                if message_opcode is not None:
                    raise RuntimeError('DevTools started a new message before finishing the previous one.')
                message_opcode = opcode
            elif opcode != 0x0 or message_opcode is None:
                raise RuntimeError(f'DevTools returned an unsupported WebSocket opcode: {opcode}')

            fragments.append(payload)
            total_size += len(payload)
            if total_size > MAX_WEBSOCKET_MESSAGE_SIZE:
                raise RuntimeError('DevTools WebSocket message is unexpectedly large.')
            if final:
                if message_opcode != 0x1:
                    raise RuntimeError('DevTools returned a non-text WebSocket message.')
                return b''.join(fragments).decode('utf-8')

    def _sendFrame(self, opcode: int, payload: bytes) -> None:
        """RFC 6455 のマスク済みクライアントフレームを送信する。

        Args:
            opcode: WebSocket opcode。
            payload: フレームの payload。

        Returns:
            None.
        """

        frame = bytearray([0x80 | opcode])
        if len(payload) < 126:
            frame.append(0x80 | len(payload))
        elif len(payload) <= 0xffff:
            frame.append(0x80 | 126)
            frame.extend(struct.pack('!H', len(payload)))
        else:
            frame.append(0x80 | 127)
            frame.extend(struct.pack('!Q', len(payload)))
        mask = os.urandom(4)
        frame.extend(mask)
        frame.extend(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.socket.sendall(frame)

    def _receiveExactly(self, length: int) -> bytes:
        """指定バイト数を WebSocket から読み取る。

        Args:
            length: 読み取るバイト数。

        Returns:
            読み取った bytes。
        """

        chunks = bytearray()
        while len(chunks) < length:
            chunk = self.socket.recv(length - len(chunks))
            if not chunk:
                raise RuntimeError('DevTools closed the WebSocket connection while receiving data.')
            chunks.extend(chunk)
        return bytes(chunks)


def extractPackageChromiumVersion(package_version: str) -> str:
    """Linux Mint パッケージ版から upstream Chromium バージョンを取り出す。

    Args:
        package_version: dpkg が返す Chromium パッケージバージョン。

    Returns:
        4要素の upstream Chromium バージョン。
    """

    match = PACKAGE_VERSION_PATTERN.fullmatch(package_version)
    if match is None:
        raise ValueError(f'Unsupported Chromium package version: {package_version!r}')
    return match.group(1)


def readChromiumBinaryVersion(chromium_path: Path) -> str:
    """実際に実行する Chromium binary のバージョンを取得する。

    Args:
        chromium_path: Chromium launcher のパス。

    Returns:
        4要素の upstream Chromium バージョン。
    """

    completed = subprocess.run(
        [str(chromium_path), '--version'],
        check=True,
        capture_output=True,
        text=True,
        timeout=15.0,
    )
    version_output = completed.stdout.strip()
    match = CHROMIUM_VERSION_PATTERN.fullmatch(version_output)
    if match is None:
        raise ValueError(f'Unsupported Chromium version output: {version_output!r}')
    return match.group(1)


def readChromiumLicense(path: Path) -> str:
    """リポジトリへ同梱した Chromium 本体の LICENSE を厳密に読む。

    Args:
        path: Chromium 本体の LICENSE を同梱したパス。

    Returns:
        正規化済みの UTF-8 本文。
    """

    license_bytes = path.read_bytes()
    if len(license_bytes) > MAX_CHROMIUM_LICENSE_SIZE:
        raise RuntimeError(f'Vendored Chromium LICENSE is unexpectedly large: {path}')
    try:
        license_text = license_bytes.decode('utf-8')
    except UnicodeDecodeError as ex:
        raise RuntimeError(f'Vendored Chromium LICENSE is not valid UTF-8: {path}') from ex
    license_text = normalizeAndValidateText(license_text, 'Vendored Chromium LICENSE')
    if not license_text.startswith('// Copyright ') or 'Redistribution and use in source and binary forms' not in license_text:
        raise RuntimeError('Vendored Chromium LICENSE does not have the expected license text structure.')
    return license_text


def readPackageCopyright(path: Path) -> tuple[str, str]:
    """Linux Mint Chromium package が同梱する copyright file を厳密に読む。

    Args:
        path: `/usr/share/doc/chromium/copyright` の実パス。

    Returns:
        UTF-8 本文と、配布ファイル bytes の SHA-256。
    """

    package_copyright_bytes = path.read_bytes()
    try:
        package_copyright = package_copyright_bytes.decode('utf-8')
    except UnicodeDecodeError as ex:
        raise RuntimeError(f'Linux Mint package copyright file is not valid UTF-8: {path}') from ex
    package_copyright = normalizeAndValidateText(
        package_copyright,
        'Linux Mint package copyright file',
    )
    if not package_copyright:
        raise RuntimeError(f'Linux Mint package copyright file is empty: {path}')
    return package_copyright, hashlib.sha256(package_copyright_bytes).hexdigest()


def extractChromiumCredits(
    chromium_path: Path,
    chromium_version: str,
) -> tuple[list[dict[str, str]], list[tuple[str, int]]]:
    """実 Chromium binary の chrome://credits から全ライセンス通知を抽出する。

    Args:
        chromium_path: Chromium launcher のパス。
        chromium_version: 実 binary から取得した4要素のバージョン。

    Returns:
        表示順を保持した credits と、適用した検証済み文字修復の一覧。
    """

    # root で動く Docker build 中でも専用 profile だけを使い、通常の Chromium 設定には触れない。
    with tempfile.TemporaryDirectory(
        prefix='konomitv-bs4k-chromium-credits-',
        ignore_cleanup_errors=True,
    ) as temporary_directory:
        temporary_path = Path(temporary_directory)
        stderr_path = temporary_path / 'chromium-stderr.log'
        with stderr_path.open('wb') as stderr_file:
            process = subprocess.Popen(
                [
                    str(chromium_path),
                    '--headless=new',
                    '--no-sandbox',
                    '--disable-gpu',
                    '--disable-dev-shm-usage',
                    '--disable-background-networking',
                    '--no-first-run',
                    '--no-default-browser-check',
                    '--remote-debugging-port=0',
                    f'--user-data-dir={temporary_path / "profile"}',
                    'about:blank',
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
            )

            client: WebSocketClient | None = None
            try:
                devtools_url = waitForDevToolsEndpoint(process, stderr_path)
                parsed_devtools_url = urllib.parse.urlsplit(devtools_url)
                if parsed_devtools_url.hostname != '127.0.0.1' or parsed_devtools_url.port is None:
                    raise RuntimeError(f'Chromium exposed an unexpected DevTools endpoint: {devtools_url!r}')
                encoded_credits_url = urllib.parse.quote(CHROMIUM_CREDITS_URL, safe='')
                target_request = urllib.request.Request(
                    f'http://127.0.0.1:{parsed_devtools_url.port}/json/new?{encoded_credits_url}',
                    method='PUT',
                )
                with urllib.request.urlopen(target_request, timeout=15.0) as response:
                    target = json.load(response)
                if target.get('type') != 'page' or target.get('url') != CHROMIUM_CREDITS_URL:
                    raise RuntimeError(f'Chromium did not open {CHROMIUM_CREDITS_URL}: {target!r}')
                websocket_url = target.get('webSocketDebuggerUrl')
                if not isinstance(websocket_url, str):
                    raise RuntimeError('Chromium did not return a DevTools page WebSocket URL.')
                client = WebSocketClient(websocket_url)

                # credits ページのロードと product 生成が完了するまで短い評価を繰り返す。
                load_deadline = time.monotonic() + 30.0
                while time.monotonic() < load_deadline:
                    loaded = evaluateCDPExpression(
                        client,
                        "document.readyState === 'complete' && document.title === 'Credits' && "
                        "document.querySelectorAll('.product').length > 0",
                        int((load_deadline - time.monotonic()) * 1000),
                    )
                    if loaded is True:
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError(f'{CHROMIUM_CREDITS_URL} did not finish loading.')

                raw_credits = evaluateCDPExpression(
                    client,
                    CREDITS_EXTRACTION_EXPRESSION,
                    int(CDP_EVALUATION_TIMEOUT_SECONDS * 1000),
                )
                if not isinstance(raw_credits, str):
                    raise RuntimeError('Chromium credits extraction did not return JSON text.')
                credits = json.loads(raw_credits)
                repaired_credits, encoding_repairs = repairKnownCreditsEncoding(chromium_version, credits)
                return (
                    validateCredits(repaired_credits, minimum_count=MINIMUM_BUNDLED_PROJECT_COUNT),
                    encoding_repairs,
                )
            finally:
                if client is not None:
                    client.close()
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)


def waitForDevToolsEndpoint(process: subprocess.Popen[bytes], stderr_path: Path) -> str:
    """Chromium が DevTools endpoint を通知するまで待機する。

    Args:
        process: 起動した Chromium process。
        stderr_path: Chromium の stderr 保存先。

    Returns:
        DevTools browser WebSocket URL。
    """

    endpoint_pattern = re.compile(r'DevTools listening on (ws://127\.0\.0\.1:\d+/devtools/browser/[A-Za-z0-9-]+)')
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        stderr_text = stderr_path.read_text(encoding='utf-8', errors='replace')
        match = endpoint_pattern.search(stderr_text)
        if match is not None:
            return match.group(1)
        if process.poll() is not None:
            raise RuntimeError(f'Chromium exited before exposing DevTools. stderr:\n{stderr_text[-4000:]}')
        time.sleep(0.05)
    stderr_text = stderr_path.read_text(encoding='utf-8', errors='replace')
    raise RuntimeError(f'Chromium did not expose DevTools within 30 seconds. stderr:\n{stderr_text[-4000:]}')


def evaluateCDPExpression(client: WebSocketClient, expression: str, timeout_milliseconds: int) -> Any:
    """CDP Runtime.evaluate を実行して JSON 化可能な結果を得る。

    Args:
        client: 接続済み DevTools WebSocket client。
        expression: 評価する固定 JavaScript 式。
        timeout_milliseconds: Chromium 内での評価期限。

    Returns:
        Runtime.evaluate の return-by-value 値。
    """

    request_id = 1
    client.sendText(json.dumps({
        'id': request_id,
        'method': 'Runtime.evaluate',
        'params': {
            'expression': expression,
            'returnByValue': True,
            'awaitPromise': True,
            'timeout': timeout_milliseconds,
        },
    }, separators=(',', ':')))
    deadline = time.monotonic() + max(1.0, timeout_milliseconds / 1000)
    while True:
        response = json.loads(client.receiveText(deadline))
        if response.get('id') != request_id:
            continue
        if 'error' in response:
            raise RuntimeError(f'DevTools Runtime.evaluate failed: {response["error"]!r}')
        evaluation_result = response.get('result', {})
        if 'exceptionDetails' in evaluation_result:
            raise RuntimeError(f'Chromium credits extraction raised an exception: {evaluation_result["exceptionDetails"]!r}')
        remote_object = evaluation_result.get('result', {})
        if 'value' not in remote_object:
            raise RuntimeError(f'DevTools Runtime.evaluate did not return a value: {remote_object!r}')
        return remote_object['value']


def repairKnownCreditsEncoding(
    chromium_version: str,
    raw_credits: Any,
) -> tuple[Any, list[tuple[str, int]]]:
    """固定 Chromium 版で一次ソースと照合済みの U+FFFD だけを修復する。

    Args:
        chromium_version: 実 binary から取得した4要素のバージョン。
        raw_credits: CDP から JSON decode した credits。

    Returns:
        修復済み credits と project ごとの修復文字数。
    """

    if not isinstance(raw_credits, list):
        return raw_credits, []
    affected_projects = {
        credit.get('name')
        for credit in raw_credits
        if isinstance(credit, dict) and any(
            isinstance(value, str) and '\ufffd' in value
            for value in credit.values()
        )
    }
    if not affected_projects:
        return raw_credits, []
    if chromium_version not in ENCODING_REPAIR_CHROMIUM_VERSIONS:
        raise ValueError(
            f'Chromium {chromium_version} credits contains Unicode replacement characters; '
            f'known repairs apply only to Chromium {sorted(ENCODING_REPAIR_CHROMIUM_VERSIONS)!r}.',
        )

    # 修復を許可する project は一次ソース照合済みの固定集合だけ。
    # Chromium 版によって損傷 project が減ることはあるが、未知 project への推測修復はしない。
    repairable_projects = set(ANDROID_NOTICE_REPAIR_PROJECTS) | {'FreeType'}
    unexpected_projects = {
        project_name
        for project_name in affected_projects
        if not isinstance(project_name, str) or project_name not in repairable_projects
    }
    if unexpected_projects:
        unexpected_project_labels = sorted(repr(project_name) for project_name in unexpected_projects)
        raise ValueError(
            'Chromium credits Unicode replacement characters occur in unexpected projects: '
            f'{unexpected_project_labels!r}.',
        )

    repaired_credits: list[Any] = []
    repairs: list[tuple[str, int]] = []
    repaired_project_names: set[str] = set()
    for index, credit in enumerate(raw_credits, start=1):
        if not isinstance(credit, dict) or credit.get('name') not in repairable_projects or not any(
            isinstance(value, str) and '\ufffd' in value
            for value in credit.values()
        ):
            repaired_credits.append(credit)
            continue
        project_name = credit['name']
        license_text = credit.get('license')
        if not isinstance(project_name, str) or not isinstance(license_text, str):
            raise ValueError(f'Chromium credit #{index} cannot apply a verified encoding repair.')
        if project_name in repaired_project_names:
            raise ValueError(f'Chromium credits contains duplicate repair project: {project_name!r}.')

        if project_name in ANDROID_NOTICE_REPAIR_PROJECTS:
            if license_text.count(ANDROID_BROKEN_LICENSE_LINE) != 2:
                raise ValueError(
                    f'Chromium credit {project_name!r} does not contain exactly two expected broken license lines.',
                )
            if license_text.count(ANDROID_BROKEN_MULTIPLE_LICENSED_LINE) != 1:
                raise ValueError(
                    f'Chromium credit {project_name!r} does not contain the expected broken Multiple-Licensed line.',
                )
            if license_text.count('\ufffd') != 8:
                raise ValueError(f'Chromium credit {project_name!r} does not contain exactly 8 replacement characters.')
            license_text = license_text.replace(ANDROID_BROKEN_LICENSE_LINE, ANDROID_REPAIRED_LICENSE_LINE)
            license_text = license_text.replace(
                ANDROID_BROKEN_MULTIPLE_LICENSED_LINE,
                ANDROID_REPAIRED_MULTIPLE_LICENSED_LINE,
            )
            repaired_character_count = 8
        else:
            if license_text.count(FREETYPE_BROKEN_COPYRIGHT_LINE) != 1 or license_text.count('\ufffd') != 1:
                raise ValueError('Chromium credit \'FreeType\' does not contain its one expected broken copyright line.')
            license_text = license_text.replace(FREETYPE_BROKEN_COPYRIGHT_LINE, FREETYPE_REPAIRED_COPYRIGHT_LINE)
            repaired_character_count = 1

        repaired_credit = dict(credit)
        repaired_credit['license'] = license_text
        repaired_credits.append(repaired_credit)
        repairs.append((project_name, repaired_character_count))
        repaired_project_names.add(project_name)

    # 損傷していた既知 project はすべて修復できていること（部分集合でも可）
    if repaired_project_names != affected_projects:
        raise ValueError(
            'Not all Chromium credits encoding repairs were applied: '
            f'{sorted(repr(name) for name in repaired_project_names)!r}.',
        )
    if any(
        isinstance(value, str) and '\ufffd' in value
        for credit in repaired_credits
        if isinstance(credit, dict)
        for value in credit.values()
    ):
        raise ValueError('Chromium credits still contains Unicode replacement characters after verified repairs.')
    return repaired_credits, repairs


def validateCredits(raw_credits: Any, *, minimum_count: int = 1) -> list[dict[str, str]]:
    """CDP から受け取った credits が欠落のない単純テキストか検証する。

    Args:
        raw_credits: JSON decode 後の値。
        minimum_count: selector 変更や途中欠落を検知するために必要な最小項目数。

    Returns:
        検証・改行正規化済みの credits。
    """

    if not isinstance(raw_credits, list) or not raw_credits:
        raise ValueError('Chromium credits must contain at least one project.')
    if len(raw_credits) < minimum_count:
        raise ValueError(
            f'Chromium credits contains only {len(raw_credits)} projects; '
            f'at least {minimum_count} are required.',
        )
    credits: list[dict[str, str]] = []
    for index, raw_credit in enumerate(raw_credits, start=1):
        if not isinstance(raw_credit, dict) or set(raw_credit) != {'name', 'homepage', 'license'}:
            raise ValueError(f'Chromium credit #{index} has an unexpected structure.')
        name = normalizeAndValidateText(raw_credit['name'], f'Chromium credit #{index} name')
        homepage = normalizeAndValidateText(raw_credit['homepage'], f'Chromium credit #{index} homepage')
        license_text = normalizeAndValidateText(raw_credit['license'], f'Chromium credit #{index} license')
        if not name or '\n' in name:
            raise ValueError(f'Chromium credit #{index} has an empty or multi-line name.')
        if not homepage or '\n' in homepage:
            raise ValueError(f'Chromium credit #{index} has an empty or multi-line homepage.')
        homepage_url = urllib.parse.urlsplit(homepage)
        external_homepage = homepage_url.scheme in {'http', 'https'} and bool(homepage_url.netloc)
        internal_homepage = homepage_url.scheme == 'chrome' and homepage_url.netloc == 'credits'
        if not external_homepage and not internal_homepage:
            raise ValueError(f'Chromium credit #{index} has an unsupported homepage URL: {homepage!r}')
        if not license_text:
            raise ValueError(f'Chromium credit #{index} has an empty license text.')
        credits.append({'name': name, 'homepage': homepage, 'license': license_text})
    return credits


def normalizeAndValidateText(value: Any, label: str) -> str:
    """ライセンス文書用テキストを正規化し危険な制御文字を拒否する。

    Args:
        value: 検証対象。
        label: エラー表示用ラベル。

    Returns:
        LF 改行へ正規化したテキスト。
    """

    if not isinstance(value, str):
        raise ValueError(f'{label} is not text.')
    # form feed はライセンス原稿の改ページ記号なので、HTML 応答へ制御文字を残さず改行境界として保持する。
    normalized = value.replace('\r\n', '\n').replace('\r', '\n').replace('\f', '\n').strip()
    if '\ufffd' in normalized:
        raise ValueError(f'{label} contains a Unicode replacement character.')
    if UNSAFE_CONTROL_CHARACTERS.search(normalized):
        raise ValueError(f'{label} contains unsafe control characters.')
    return normalized


def buildLicenseDocument(
    package_version: str,
    upstream_version: str,
    license_url: str,
    chromium_license: str,
    package_copyright: str,
    package_copyright_sha256: str,
    credits: list[dict[str, str]],
    encoding_repairs: list[tuple[str, int]],
) -> str:
    """Chromium 本体と bundled components の通知を独立した Markdown 文書へまとめる。

    Args:
        package_version: Linux Mint Chromium パッケージ版。
        upstream_version: 対応する upstream Chromium 版。
        license_url: 同梱した公式 LICENSE の出典 URL。
        chromium_license: Chromium 本体の LICENSE 本文。
        package_copyright: Linux Mint package 同梱 copyright file の本文。
        package_copyright_sha256: 同梱 copyright file bytes の SHA-256。
        credits: 実 binary の chrome://credits から抽出した通知。
        encoding_repairs: 一次ソースと照合して適用した project ごとの文字修復。

    Returns:
        専用 API から提供する完全な Markdown 文書。
    """

    separator = '=' * 96
    entry_separator = '-' * 96
    bundled_notice_lines = [separator]
    for index, credit in enumerate(credits, start=1):
        bundled_notice_lines.extend([
            f'Project {index}: {credit["name"]}',
            f'Homepage: {credit["homepage"]}',
            'License:',
            credit['license'],
            '',
            entry_separator,
        ])
    bundled_notices = '\n'.join(bundled_notice_lines).rstrip()
    repaired_character_count = sum(character_count for _, character_count in encoding_repairs)
    repair_lines = [
        f'- `{project_name}`: {character_count} {"character" if character_count == 1 else "characters"}'
        for project_name, character_count in encoding_repairs
    ]
    if encoding_repairs:
        repair_document = [
            f'The installed Chromium {upstream_version} credits page contains U+FFFD in fixed third-party notice text.',
            'The generator repairs only the exact project names, occurrence counts, and broken lines registered for this',
            'Chromium version; every other U+FFFD fails the Docker build.',
            '',
            f'- Repaired characters: {repaired_character_count} across {len(encoding_repairs)} projects',
            f'- Mozilla MPL 1.1 reference for ASCII quotation marks: <{MOZILLA_MPL_1_1_SOURCE}>',
            f'- Fixed FreeType FTL.TXT reference for the copyright sign: <{FREETYPE_LICENSE_SOURCE}>',
            *repair_lines,
        ]
    else:
        repair_document = ['No Unicode replacement character repairs were required.']

    # ライセンス本文はすべて fenced code block に隔離し、HTML として解釈されないようにする。
    # fence は本文中の最長 backtick 列より長くするため、将来本文に Markdown が増えても内容を変更しない。
    document_lines = [
        '# Chromium Third-Party Software Licenses',
        '',
        'This document is generated from the Chromium binary installed in this Docker image.',
        '',
        f'- Installed Linux Mint package version: `{package_version}`',
        f'- Installed upstream Chromium version: `{upstream_version}`',
        f'- Chromium main license source: <{license_url}>',
        f'- Chromium main license SHA-256: `{hashlib.sha256(chromium_license.encode("utf-8")).hexdigest()}`',
        '- Linux Mint package copyright path: `/usr/share/doc/chromium/copyright`',
        f'- Linux Mint package copyright SHA-256: `{package_copyright_sha256}`',
        f'- Bundled notice source: `{CHROMIUM_CREDITS_URL}` from the installed binary',
        f'- Bundled project count: {len(credits)}',
        '',
        '## Chromium main license',
        '',
        markdownCodeBlock(chromium_license),
        '',
        '## Linux Mint package copyright file',
        '',
        'This is the complete Debian-style copyright metadata file shipped by the Linux Mint `chromium` package.',
        'It is preserved as package metadata and is not the Chromium upstream license or a substitute for it.',
        '',
        markdownCodeBlock(package_copyright),
        '',
        '## Verified encoding repairs in bundled notices',
        '',
        *repair_document,
        '',
        '## Bundled third-party notices',
        '',
        markdownCodeBlock(bundled_notices),
    ]
    return '\n'.join(document_lines).rstrip() + '\n'


def markdownCodeBlock(text: str) -> str:
    """本文を改変せず安全な Markdown fenced code block にする。

    Args:
        text: コードブロックへ格納する本文。

    Returns:
        本文中の backtick 列では閉じない fenced code block。
    """

    longest_backtick_run = max((len(run) for run in re.findall(r'`+', text)), default=0)
    fence = '`' * max(4, longest_backtick_run + 1)
    return f'{fence}text\n{text}\n{fence}'


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--chromium', type=Path, required=True)
    parser.add_argument('--package-version', required=True)
    parser.add_argument('--chromium-license', type=Path, required=True)
    parser.add_argument('--package-copyright', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()

    if not args.chromium.is_file():
        raise FileNotFoundError(f'Chromium launcher does not exist: {args.chromium}')
    package_upstream_version = extractPackageChromiumVersion(args.package_version)
    binary_version = readChromiumBinaryVersion(args.chromium)
    if binary_version != package_upstream_version:
        raise RuntimeError(
            f'Chromium binary version {binary_version!r} does not match package version '
            f'{args.package_version!r}.',
        )

    chromium_license = readChromiumLicense(args.chromium_license)
    package_copyright, package_copyright_sha256 = readPackageCopyright(args.package_copyright)
    credits, encoding_repairs = extractChromiumCredits(args.chromium, binary_version)
    document = buildLicenseDocument(
        args.package_version,
        binary_version,
        CHROMIUM_LICENSE_SOURCE_URL,
        chromium_license,
        package_copyright,
        package_copyright_sha256,
        credits,
        encoding_repairs,
    )
    args.output.write_text(document, encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
