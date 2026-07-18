import ipaddress
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psutil
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class ServerStartupSettings:
    """HTTPS モードに応じた Uvicorn の起動設定。"""

    host: str
    port: int
    ssl_certfile: str | None
    ssl_keyfile: str | None
    use_akebi: bool
    proxy_headers: bool = False


def BuildServerStartupSettings(server_settings: Any) -> ServerStartupSettings:
    """サーバー設定から Akebi / Uvicorn の排他的な起動構成を組み立てる。"""

    if server_settings.https_mode == 'akebi':
        return ServerStartupSettings(
            host = '127.0.0.77',
            port = server_settings.port + 10,
            ssl_certfile = None,
            ssl_keyfile = None,
            use_akebi = True,
        )

    if server_settings.https_mode == 'certificate':
        certificate = server_settings.custom_https_certificate
        private_key = server_settings.custom_https_private_key
        assert isinstance(certificate, Path)
        assert isinstance(private_key, Path)
        return ServerStartupSettings(
            host = '0.0.0.0',
            port = server_settings.port,
            ssl_certfile = str(certificate),
            ssl_keyfile = str(private_key),
            use_akebi = False,
        )

    return ServerStartupSettings(
        host = str(server_settings.reverse_proxy_listen_address),
        port = server_settings.port,
        ssl_certfile = None,
        ssl_keyfile = None,
        use_akebi = False,
    )


def GetRequiredThirdpartyLibraries(https_mode: str) -> set[str]:
    """HTTPS モードに応じて起動前に存在確認する thirdparty を返す。"""

    libraries = {'FFmpeg', 'FFprobe', 'QSVEncC', 'NVEncC', 'VCEEncC', 'tsreadex', 'psisiarc'}
    if https_mode == 'akebi':
        libraries.add('Akebi')
    return libraries


def GetAkebiAccessURLs(port: int) -> list[tuple[str, str]]:
    """Akebi の証明書でアクセスできる HTTPS URL とインターフェイス名を返す。"""

    access_urls = [(f'https://my.local.konomi.tv:{port}/', 'ローカルホスト')]
    interface_addresses: list[tuple[ipaddress.IPv4Address, str]] = []
    try:
        network_interfaces = psutil.net_if_addrs()
    except OSError:
        # 制限されたコンテナ環境などで列挙できなくても、ループバック用 URL は案内できる
        return access_urls
    for interface_name, addresses in network_interfaces.items():
        for address in addresses:
            if address.family != socket.AF_INET:
                continue
            ip_address = ipaddress.ip_address(address.address)
            if not isinstance(ip_address, ipaddress.IPv4Address):
                continue
            # upstream インストーラーと同様に、ループバックとリンクローカルは表示しない
            if ip_address.is_loopback or ip_address.is_link_local:
                continue
            interface_addresses.append((ip_address, interface_name))

    # IP アドレス順で安定して表示し、同一 IP が複数回列挙された場合は最初の1件だけを使う
    seen_ip_addresses: set[ipaddress.IPv4Address] = set()
    for ip_address, interface_name in sorted(interface_addresses, key=lambda item: int(item[0])):
        if ip_address in seen_ip_addresses:
            continue
        seen_ip_addresses.add(ip_address)
        hostname = str(ip_address).replace('.', '-')
        access_urls.append((f'https://{hostname}.local.konomi.tv:{port}/', interface_name))
    return access_urls


class ReverseProxyMiddleware:
    """TCP 接続元を検証してから信頼済み転送ヘッダーを ASGI scope へ反映する。"""

    def __init__(self, app: ASGIApp, trusted_proxy_cidrs: Sequence[str | IPNetwork]) -> None:
        self.app = app
        self.trusted_proxy_networks = tuple(ipaddress.ip_network(str(cidr), strict=False) for cidr in trusted_proxy_cidrs)
        if not self.trusted_proxy_networks:
            raise ValueError('trusted_proxy_cidrs must not be empty.')

    @staticmethod
    def _normalize_ip_address(address: IPAddress) -> IPAddress:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            return address.ipv4_mapped
        return address

    def _is_trusted_proxy(self, address: IPAddress) -> bool:
        normalized_address = self._normalize_ip_address(address)
        return any(
            normalized_address.version == network.version and normalized_address in network
            for network in self.trusted_proxy_networks
        )

    @staticmethod
    def _get_forwarded_value(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
        values = [value.decode('latin-1') for key, value in headers if key.lower() == name]
        if not values:
            return None
        return ','.join(values)

    @staticmethod
    def _replace_header(headers: list[tuple[bytes, bytes]], name: bytes, value: str | None) -> list[tuple[bytes, bytes]]:
        replaced_headers = [(key, current_value) for key, current_value in headers if key.lower() != name]
        if value is not None:
            replaced_headers.append((name, value.encode('latin-1')))
        return replaced_headers

    @staticmethod
    def _is_valid_forwarded_host(host: str) -> bool:
        if not host or any(character.isspace() for character in host):
            return False
        try:
            parsed_host = urlsplit(f'//{host}')
            _ = parsed_host.port
        except ValueError:
            return False
        return (
            parsed_host.hostname is not None
            and parsed_host.username is None
            and parsed_host.password is None
            and parsed_host.path == ''
            and parsed_host.query == ''
            and parsed_host.fragment == ''
        )

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, status_code: int, message: str) -> None:
        if scope['type'] == 'http':
            await PlainTextResponse(message, status_code=status_code)(scope, receive, send)
        elif scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008, 'reason': message})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] not in ('http', 'websocket'):
            await self.app(scope, receive, send)
            return

        peer = scope.get('client')
        if peer is None:
            await self._reject(scope, receive, send, 403, 'Direct access is forbidden.')
            return

        try:
            peer_address = self._normalize_ip_address(ipaddress.ip_address(peer[0]))
        except ValueError:
            await self._reject(scope, receive, send, 403, 'Direct access is forbidden.')
            return

        # 転送ヘッダーを一切参照する前に、実際の TCP 接続元を検証する
        if not self._is_trusted_proxy(peer_address):
            await self._reject(scope, receive, send, 403, 'Direct access is forbidden.')
            return

        headers = list(scope.get('headers', []))
        forwarded_for = self._get_forwarded_value(headers, b'x-forwarded-for')
        forwarded_proto = self._get_forwarded_value(headers, b'x-forwarded-proto')
        forwarded_host = self._get_forwarded_value(headers, b'x-forwarded-host')

        client_address = peer_address
        if forwarded_for is not None:
            try:
                forwarded_addresses = [
                    self._normalize_ip_address(ipaddress.ip_address(item.strip()))
                    for item in forwarded_for.split(',')
                    if item.strip()
                ]
            except ValueError:
                await self._reject(scope, receive, send, 400, 'Invalid X-Forwarded-For header.')
                return
            if not forwarded_addresses:
                await self._reject(scope, receive, send, 400, 'Invalid X-Forwarded-For header.')
                return

            # 右端の実接続元から左へたどり、信頼済みプロキシを除外した最初の IP をクライアントとする
            address_chain = [*forwarded_addresses, peer_address]
            client_address = address_chain[0]
            for address in reversed(address_chain):
                if self._is_trusted_proxy(address):
                    continue
                client_address = address
                break

        scheme = scope.get('scheme', 'http')
        if forwarded_proto is not None:
            scheme = forwarded_proto.rsplit(',', maxsplit=1)[-1].strip().lower()
            if scheme not in ('http', 'https'):
                await self._reject(scope, receive, send, 400, 'Invalid X-Forwarded-Proto header.')
                return

        public_host: str | None = None
        if forwarded_host is not None:
            public_host = forwarded_host.rsplit(',', maxsplit=1)[-1].strip()
            if not self._is_valid_forwarded_host(public_host):
                await self._reject(scope, receive, send, 400, 'Invalid X-Forwarded-Host header.')
                return

        scope['client'] = (str(client_address), 0)
        scope['scheme'] = scheme
        headers = self._replace_header(headers, b'x-forwarded-for', str(client_address))
        headers = self._replace_header(headers, b'x-forwarded-proto', scheme)
        headers = self._replace_header(headers, b'x-forwarded-host', public_host)
        if public_host is not None:
            headers = self._replace_header(headers, b'host', public_host)
        scope['headers'] = headers

        await self.app(scope, receive, send)
