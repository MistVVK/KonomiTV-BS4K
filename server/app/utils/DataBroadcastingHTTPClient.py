from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import time
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpcore
import httpx


_UPSTREAM_REQUEST_TIMEOUT_SECONDS = 5.0

# データ放送 upstream 応答の本文上限。
# BML ネット接続は小さな HTML / 画像を想定し、未認証の巨大応答によるメモリ枯渇を防ぐ。
MAX_UPSTREAM_RESPONSE_BYTES = 4 * 1024 * 1024

# probe API が受け付ける timeout の下限・上限 (ミリ秒)。
# 下限は誤って 0 に近い値で即失敗するのを防ぎ、上限は event loop 拘束時間を抑える。
PROBE_TIMEOUT_MILLISECONDS_MIN = 100
PROBE_TIMEOUT_MILLISECONDS_MAX = 5000


class UpstreamURLRejected(ValueError):
    """データ放送プロキシの接続先ポリシーで拒否された URL。"""


class UpstreamBodyLimitExceeded(ValueError):
    """データ放送プロキシの上流応答本文が設定上限を超えた。"""


@dataclass(frozen=True, slots=True)
class ResolvedUpstreamURL:
    """接続先検証済みの上流 URL と、接続時に再解決しない IP 一覧。"""

    url: httpx.URL
    host: str
    port: int
    addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DataBroadcastingHTTPResponse:
    """データ放送プロキシが上流から受け取った最小限の HTTP 応答。"""

    status_code: int
    headers: httpx.Headers
    content: bytes


class _AsyncioNetworkStream(httpcore.AsyncNetworkStream):
    """httpcore から検証済みIPへ接続する asyncio ネットワークストリーム。"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """
        Args:
            reader: 接続済みTCPソケットの読み取り側。
            writer: 接続済みTCPソケットの書き込み側。
        """

        self._reader = reader
        self._writer = writer

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        """上流から最大 max_bytes を読み取る。"""

        try:
            read_task = self._reader.read(max_bytes)
            return await asyncio.wait_for(read_task, timeout) if timeout is not None else await read_task
        except TimeoutError as ex:
            raise httpcore.ReadTimeout('Upstream response read timed out') from ex
        except (ConnectionError, OSError) as ex:
            raise httpcore.ReadError('Failed to read upstream response') from ex

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        """上流へ buffer を書き込む。"""

        try:
            self._writer.write(buffer)
            drain_task = self._writer.drain()
            if timeout is not None:
                await asyncio.wait_for(drain_task, timeout)
            else:
                await drain_task
        except TimeoutError as ex:
            raise httpcore.WriteTimeout('Upstream request write timed out') from ex
        except (ConnectionError, OSError) as ex:
            raise httpcore.WriteError('Failed to write upstream request') from ex

    async def aclose(self) -> None:
        """接続を閉じる。"""

        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            # 既に切断された接続の後始末では、元の通信結果を上書きしない。
            pass

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> _AsyncioNetworkStream:
        """元のホスト名をSNIに使ってTLSへ昇格する。"""

        try:
            start_tls_task = self._writer.start_tls(
                ssl_context,
                server_hostname=server_hostname,
                ssl_handshake_timeout=timeout,
            )
            if timeout is not None:
                await asyncio.wait_for(start_tls_task, timeout)
            else:
                await start_tls_task
        except TimeoutError as ex:
            await self.aclose()
            raise httpcore.ConnectTimeout('Upstream TLS handshake timed out') from ex
        except (ConnectionError, OSError, ssl.SSLError) as ex:
            await self.aclose()
            raise httpcore.ConnectError('Failed to establish upstream TLS') from ex
        return self

    def get_extra_info(self, info: str) -> object:
        """httpcore が要求するソケット情報を返す。"""

        return self._writer.get_extra_info(info)


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """あらかじめ検証したIP一覧へだけ接続するhttpcoreバックエンド。"""

    def __init__(self, host: str, addresses: tuple[str, ...]) -> None:
        """
        Args:
            host: HTTP/TLS上で維持する元のホスト名。
            addresses: DNS検証済みで、接続時に試行するIP一覧。
        """

        self._host = host
        self._addresses = addresses

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> _AsyncioNetworkStream:
        """
        検証済みIPへTCP接続する。

        DNSはここで再実行しない。複数の公開IPがある場合は、発行時に検証した
        一覧の範囲だけで接続を試行し、接続失敗時に別のIPへ切り替える。

        Args:
            host: httpcoreが要求する論理ホスト名。
            port: 接続先port。
            timeout: 接続に許された秒数。
            local_address: バインドするローカルアドレス。
            socket_options: 接続ソケットへ適用するオプション。

        Returns:
            検証済みIPへ接続したネットワークストリーム。

        Raises:
            httpcore.ConnectError: すべての検証済みIPへの接続に失敗した場合。
            httpcore.ConnectTimeout: 接続期限を超えた場合。
        """

        if host != self._host:
            raise httpcore.ConnectError('Logical upstream host changed during connection')

        deadline = time.monotonic() + timeout if timeout is not None else None
        last_error: BaseException | None = None
        timed_out = False

        for address in self._addresses:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                timed_out = True
                break

            try:
                connect_task = asyncio.open_connection(
                    host=address,
                    port=port,
                    local_addr=local_address,
                )
                if remaining is not None:
                    reader, writer = await asyncio.wait_for(connect_task, remaining)
                else:
                    reader, writer = await connect_task

                if socket_options is not None:
                    raw_socket = writer.get_extra_info('socket')
                    if raw_socket is not None:
                        for socket_option in socket_options:
                            raw_socket.setsockopt(*socket_option)
                return _AsyncioNetworkStream(reader, writer)
            except TimeoutError as ex:
                last_error = ex
                timed_out = True
            except (ConnectionError, OSError) as ex:
                last_error = ex

        if timed_out:
            raise httpcore.ConnectTimeout('Timed out connecting to upstream') from last_error
        raise httpcore.ConnectError('Failed to connect to upstream') from last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> _AsyncioNetworkStream:
        """Unix domain socketへの接続を拒否する。"""

        raise httpcore.ConnectError('Unix socket upstreams are not allowed')

    async def sleep(self, seconds: float) -> None:
        """httpcoreのバックオフ待機をasyncioで実行する。"""

        await asyncio.sleep(seconds)


def _CreateInsecureTLSContext() -> ssl.SSLContext:
    """既存の期限切れ証明書互換を維持したTLSコンテキストを作成する。"""

    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    return ssl_context


def _IsAllowedGlobalAddress(address: str) -> bool:
    """外部インターネット向け接続先として許可できるIPか判定する。"""

    try:
        parsed_address = ipaddress.ip_address(address)
    except ValueError:
        return False

    # IPv4-mapped IPv6 は内側のIPv4アドレスへ戻して判定する。これにより
    # ::ffff:127.0.0.1 のようなloopback偽装をグローバルIPと誤認しない。
    if isinstance(parsed_address, ipaddress.IPv6Address) and parsed_address.ipv4_mapped is not None:
        parsed_address = parsed_address.ipv4_mapped

    return (
        parsed_address.is_global
        and not parsed_address.is_private
        and not parsed_address.is_loopback
        and not parsed_address.is_link_local
        and not parsed_address.is_reserved
        and not parsed_address.is_multicast
        and not parsed_address.is_unspecified
    )


def _ParseUpstreamURL(request_url: str) -> httpx.URL:
    """URL構文とHTTPプロキシで許可するschemeを検証する。"""

    if any(control_character in request_url for control_character in ('\r', '\n', '\x00')):
        raise UpstreamURLRejected('Upstream URL contains a control character')

    try:
        parsed_url = httpx.URL(request_url)
        parsed_parts = urlsplit(request_url)
    except (httpx.InvalidURL, ValueError) as ex:
        raise UpstreamURLRejected('Upstream URL is invalid') from ex

    if parsed_url.scheme not in ('http', 'https'):
        raise UpstreamURLRejected('Upstream URL scheme is not allowed')
    if not parsed_url.host:
        raise UpstreamURLRejected('Upstream URL host is missing')
    if '@' in parsed_parts.netloc or parsed_url.userinfo != b'':
        raise UpstreamURLRejected('Upstream URL credentials are not allowed')
    if parsed_url.fragment:
        raise UpstreamURLRejected('Upstream URL fragments are not allowed')

    try:
        # httpx.URL はportが省略されたとき None を返すため、接続時の既定値を確定する。
        parsed_url.port
    except (httpx.InvalidURL, ValueError) as ex:
        raise UpstreamURLRejected('Upstream URL port is invalid') from ex

    return parsed_url


async def _ResolveAllowedAddresses(host: str, port: int) -> tuple[str, ...]:
    """hostを一度だけ解決し、全結果がグローバルIPであることを確認する。"""

    loop = asyncio.get_running_loop()
    try:
        address_records = await loop.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as ex:
        raise UpstreamURLRejected('Upstream host could not be resolved') from ex

    resolved_addresses: list[str] = []
    for _family, _socktype, _protocol, _canonname, sockaddr in address_records:
        address = sockaddr[0]
        if isinstance(address, str):
            resolved_addresses.append(address)

    addresses = tuple(dict.fromkeys(resolved_addresses))
    if not addresses or any(not _IsAllowedGlobalAddress(address) for address in addresses):
        raise UpstreamURLRejected('Upstream host resolved to a non-global address')
    return addresses


async def ResolveUpstreamURL(request_url: str) -> ResolvedUpstreamURL:
    """URLを解析し、接続に使う公開IPを固定した上流URLを返す。"""

    parsed_url = _ParseUpstreamURL(request_url)
    port = parsed_url.port or (443 if parsed_url.scheme == 'https' else 80)
    addresses = await _ResolveAllowedAddresses(parsed_url.host, port)
    return ResolvedUpstreamURL(
        url=parsed_url,
        host=parsed_url.host,
        port=port,
        addresses=addresses,
    )


async def ResolveProbeDestination(destination: str) -> tuple[str, ...]:
    """
    probe API の destination を公開IPポリシーで検証し、接続に使うIP一覧を返す。

    hostname の場合は DNS を一度だけ解決し、全結果が公開IPであることを確認する。
    接続時は返したIPへ直接つなぎ、DNS rebinding で private IP へ差し替わらないようにする。

    Args:
        destination: クライアントが指定したホスト名または IP アドレス。

    Returns:
        検証済みの公開IP一覧。順序は DNS 解決結果の出現順を維持する。

    Raises:
        UpstreamURLRejected: destination が空・不正、または非公開IPを含む場合。
    """

    # URL やパス風の入力、制御文字は probe の destination として受け付けない。
    if (
        not destination
        or any(control_character in destination for control_character in ('\r', '\n', '\x00', '\t', ' '))
        or '/' in destination
        or '@' in destination
        or '://' in destination
    ):
        raise UpstreamURLRejected('Probe destination is invalid')

    # リテラル IP は DNS を経由せず、そのアドレス自体を公開IPポリシーで判定する。
    try:
        literal_address = ipaddress.ip_address(destination)
    except ValueError:
        literal_address = None
    if literal_address is not None:
        address_text = str(literal_address)
        if not _IsAllowedGlobalAddress(address_text):
            raise UpstreamURLRejected('Probe destination is not a global address')
        return (address_text,)

    # ホスト名は HTTP proxy と同じ解決・公開IP検査を共有する。
    # getaddrinfo は asyncio 経由で thread に退避され、event loop を塞がない。
    return await _ResolveAllowedAddresses(destination, 80)


def _ExtractContentLength(headers: list[tuple[bytes, bytes]]) -> int | None:
    """HTTP ヘッダーから Content-Length を取り出す。不正値は上限超過扱いにする。"""

    for key, value in headers:
        if key.lower() != b'content-length':
            continue
        try:
            return int(value.decode('ascii').strip())
        except (UnicodeDecodeError, ValueError):
            # 不正な Content-Length は巨大本文と同じく拒否する。
            return MAX_UPSTREAM_RESPONSE_BYTES + 1
    return None


async def _ReadResponseBodyWithLimit(
    response: httpcore.Response,
    max_body_bytes: int = MAX_UPSTREAM_RESPONSE_BYTES,
) -> bytes:
    """
    上流応答本文を Content-Length 先行拒否と stream 累積上限の両方で読み取る。

    Args:
        response: httpcore の上流応答。本文は未読であること。
        max_body_bytes: 許可する最大バイト数。

    Returns:
        上限以内で読み取った本文。

    Raises:
        UpstreamBodyLimitExceeded: Content-Length または累積読取量が上限を超えた場合。
    """

    content_length = _ExtractContentLength(list(response.headers))
    # 304 の Content-Length は message body ではなく、選択された表現のサイズを示す。
    # 実際の本文は存在しないため、ここで上限判定へ使うと正当なキャッシュ応答を誤拒否する。
    if response.status != 304 and content_length is not None and content_length > max_body_bytes:
        raise UpstreamBodyLimitExceeded('Upstream response Content-Length exceeds the limit')

    chunks: list[bytes] = []
    total_bytes = 0
    # aread() は全量を一度に保持するため使わず、chunk ごとに上限を検査する。
    # slow stream は connection 側の read timeout で打ち切る。
    async for chunk in response.aiter_stream():
        if not chunk:
            continue
        total_bytes += len(chunk)
        if total_bytes > max_body_bytes:
            raise UpstreamBodyLimitExceeded('Upstream response body exceeds the limit')
        chunks.append(chunk)
    return b''.join(chunks)


async def _RequestOnce(
    resolved_url: ResolvedUpstreamURL,
    method: str,
    headers: dict[str, str],
    content: bytes | None,
) -> DataBroadcastingHTTPResponse:
    """検証済みIPへ一度だけHTTPリクエストを送り、本文を上限付きで読み切って接続を閉じる。"""

    network_backend = PinnedNetworkBackend(resolved_url.host, resolved_url.addresses)
    connection_pool = httpcore.AsyncConnectionPool(
        ssl_context=_CreateInsecureTLSContext(),
        max_connections=1,
        max_keepalive_connections=0,
        http1=True,
        http2=False,
        network_backend=network_backend,
    )
    try:
        request_headers = [
            (key, value)
            for key, value in headers.items()
            if key.lower() != 'host'
        ]
        # httpcoreの低レベルAPIはHostを自動付与しないため、検証済みURLの
        # authorityを固定する。呼び出し側からHostを差し替える余地も残さない。
        request_headers.append(('Host', resolved_url.url.netloc.decode('ascii')))
        request = httpcore.Request(
            method=method,
            url=httpcore.URL(
                scheme=resolved_url.url.raw_scheme,
                host=resolved_url.url.raw_host,
                port=resolved_url.port,
                target=resolved_url.url.raw_path,
            ),
            headers=request_headers,
            content=content,
            extensions={
                'timeout': {
                    'connect': _UPSTREAM_REQUEST_TIMEOUT_SECONDS,
                    'read': _UPSTREAM_REQUEST_TIMEOUT_SECONDS,
                    'write': _UPSTREAM_REQUEST_TIMEOUT_SECONDS,
                    'pool': _UPSTREAM_REQUEST_TIMEOUT_SECONDS,
                },
            },
        )
        response = await connection_pool.handle_async_request(request)
        try:
            response_content = await _ReadResponseBodyWithLimit(response)
            response_headers = httpx.Headers(response.headers)
            return DataBroadcastingHTTPResponse(
                status_code=response.status,
                headers=response_headers,
                content=response_content,
            )
        finally:
            # 上限超過や途中失敗でも接続を残さず閉じる。
            await response.aclose()
    finally:
        await connection_pool.aclose()


def _PrepareRedirectRequest(
    status_code: int,
    method: str,
    content: bytes | None,
    headers: dict[str, str],
) -> tuple[str, bytes | None, dict[str, str]]:
    """HTTP redirectのmethod/body規則に従い、次のリクエストを準備する。"""

    if status_code == 303 or (status_code in (301, 302) and method == 'POST'):
        next_headers = {
            key: value
            for key, value in headers.items()
            if key.lower() not in ('content-type', 'content-length', 'transfer-encoding')
        }
        return 'GET', None, next_headers
    return method, content, headers


async def RequestWithSafeRedirects(
    method: str,
    request_url: str,
    *,
    headers: dict[str, str],
    content: bytes | None = None,
) -> DataBroadcastingHTTPResponse:
    """
    接続先検証とredirect再検証を行い、上流へHTTPリクエストを送信する。

    Args:
        method: HTTP method。GETまたはPOST。
        request_url: クライアントが指定した上流URL。
        headers: 上流へ転送する限定済みリクエストヘッダー。
        content: 上流へ転送するraw本文。

    Returns:
        最終上流応答。本文は読み取り済み。

    Raises:
        UpstreamURLRejected: 初期URLまたはredirect先が通信ポリシーに違反した場合。
        httpcore.NetworkError: 上流接続または読み取りに失敗した場合。
    """

    current_url = request_url
    current_method = method
    current_content = content
    current_headers = dict(headers)

    for redirect_count in range(4):
        resolved_url = await ResolveUpstreamURL(current_url)
        response = await _RequestOnce(
            resolved_url,
            current_method,
            current_headers,
            current_content,
        )

        location = response.headers.get('location')
        if response.status_code not in (301, 302, 303, 307, 308) or location is None:
            return response
        if redirect_count >= 3:
            raise UpstreamURLRejected('Too many upstream redirects')

        next_url = resolved_url.url.join(location)
        current_url = str(next_url)
        current_method, current_content, current_headers = _PrepareRedirectRequest(
            response.status_code,
            current_method,
            current_content,
            current_headers,
        )

    raise UpstreamURLRejected('Too many upstream redirects')
