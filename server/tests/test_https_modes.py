import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import ValidationError
from ruamel.yaml import YAML
from starlette.types import Message, Receive, Scope, Send

import app as app_package
import app.config as config_module
from app.config import ServerSettings
from app.utils.HTTPS import (
    BuildServerStartupSettings,
    GetRequiredThirdpartyLibraries,
    ReverseProxyMiddleware,
)


class HTTPSModeConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        temporary_path = Path(self.temporary_directory.name)
        self.certificate_path = temporary_path / 'certificate.pem'
        self.private_key_path = temporary_path / 'private-key.pem'
        self.certificate_path.write_text('certificate', encoding='utf-8')
        self.private_key_path.write_text('private-key', encoding='utf-8')

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def server_settings(**overrides: object):
        return ServerSettings.model_validate(
            {'server': {'port': 65420, **overrides}},
            context={'bypass_validation': False},
        ).server

    def test_default_mode_is_akebi(self) -> None:
        settings = self.server_settings()
        self.assertEqual(settings.https_mode, 'akebi')

    def test_akebi_rejects_certificate_settings(self) -> None:
        with self.assertRaisesRegex(ValidationError, 'https_mode を certificate に変更'):
            self.server_settings(custom_https_certificate=self.certificate_path)

    def test_certificate_requires_both_files(self) -> None:
        with self.assertRaisesRegex(ValidationError, '両方を指定'):
            self.server_settings(
                https_mode='certificate',
                custom_https_certificate=self.certificate_path,
            )

        settings = self.server_settings(
            https_mode='certificate',
            custom_https_certificate=self.certificate_path,
            custom_https_private_key=self.private_key_path,
        )
        self.assertEqual(settings.custom_https_private_key, self.private_key_path)

    def test_reverse_proxy_requires_non_empty_cidr_list(self) -> None:
        with self.assertRaisesRegex(ValidationError, '1件以上'):
            self.server_settings(https_mode='reverse_proxy')

    def test_reverse_proxy_rejects_certificate_settings(self) -> None:
        with self.assertRaisesRegex(ValidationError, '指定できません'):
            self.server_settings(
                https_mode='reverse_proxy',
                custom_https_certificate=self.certificate_path,
                custom_https_private_key=self.private_key_path,
                trusted_proxy_cidrs=['10.0.0.0/8'],
            )

    def test_non_reverse_proxy_mode_rejects_trusted_proxy_cidrs(self) -> None:
        with self.assertRaisesRegex(ValidationError, 'reverse_proxy の場合のみ'):
            self.server_settings(trusted_proxy_cidrs=['10.0.0.0/8'])

    def test_non_reverse_proxy_mode_rejects_custom_listen_address(self) -> None:
        with self.assertRaisesRegex(ValidationError, 'reverse_proxy の場合のみ'):
            self.server_settings(reverse_proxy_listen_address='127.0.0.1')

    def test_startup_settings_are_exclusive(self) -> None:
        akebi = BuildServerStartupSettings(self.server_settings())
        self.assertEqual((akebi.host, akebi.port), ('127.0.0.77', 65430))
        self.assertTrue(akebi.use_akebi)
        self.assertIsNone(akebi.ssl_certfile)
        self.assertFalse(akebi.proxy_headers)

        certificate = BuildServerStartupSettings(self.server_settings(
            https_mode='certificate',
            custom_https_certificate=self.certificate_path,
            custom_https_private_key=self.private_key_path,
        ))
        self.assertEqual((certificate.host, certificate.port), ('0.0.0.0', 65420))
        self.assertFalse(certificate.use_akebi)
        self.assertEqual(certificate.ssl_certfile, str(self.certificate_path))
        self.assertEqual(certificate.ssl_keyfile, str(self.private_key_path))
        self.assertFalse(certificate.proxy_headers)

        reverse_proxy = BuildServerStartupSettings(self.server_settings(
            https_mode='reverse_proxy',
            reverse_proxy_listen_address='::',
            trusted_proxy_cidrs=['10.0.0.0/8'],
        ))
        self.assertEqual((reverse_proxy.host, reverse_proxy.port), ('::', 65420))
        self.assertFalse(reverse_proxy.use_akebi)
        self.assertIsNone(reverse_proxy.ssl_certfile)
        self.assertFalse(reverse_proxy.proxy_headers)

    def test_akebi_is_required_only_in_akebi_mode(self) -> None:
        self.assertIn('Akebi', GetRequiredThirdpartyLibraries('akebi'))
        self.assertNotIn('Akebi', GetRequiredThirdpartyLibraries('certificate'))
        self.assertNotIn('Akebi', GetRequiredThirdpartyLibraries('reverse_proxy'))

    def test_certificate_paths_are_mapped_through_host_rootfs_in_docker(self) -> None:
        temporary_path = Path(self.temporary_directory.name)
        host_rootfs = temporary_path / 'host-rootfs'
        host_rootfs.mkdir()
        (host_rootfs / 'certificate.pem').write_text('certificate', encoding='utf-8')
        (host_rootfs / 'private-key.pem').write_text('private-key', encoding='utf-8')
        config_path = temporary_path / 'config.yaml'
        config_path.write_text(
            'server:\n'
            "    https_mode: 'certificate'\n"
            '    port: 65420\n'
            "    custom_https_certificate: '/certificate.pem'\n"
            "    custom_https_private_key: '/private-key.pem'\n",
            encoding='utf-8',
        )

        original_config = config_module._CONFIG  # pyright: ignore[reportPrivateUsage]
        original_config_path = config_module._CONFIG_YAML_PATH  # pyright: ignore[reportPrivateUsage]
        original_docker_path_prefix = config_module._DOCKER_PATH_PREFIX  # pyright: ignore[reportPrivateUsage]
        try:
            config_module._CONFIG = None  # pyright: ignore[reportPrivateUsage]
            config_module._CONFIG_YAML_PATH = config_path  # pyright: ignore[reportPrivateUsage]
            config_module._DOCKER_PATH_PREFIX = str(host_rootfs)  # pyright: ignore[reportPrivateUsage]
            test_logging = SimpleNamespace(debug=Mock(), error=Mock())
            with (
                patch.object(app_package, 'logging', test_logging, create=True),
                patch.dict('sys.modules', {'app.logging': test_logging}),
                patch('app.utils.GetPlatformEnvironment', return_value='Linux-Docker'),
            ):
                settings = config_module.LoadConfig(bypass_validation=True)
                self.assertEqual(settings.server.custom_https_certificate, host_rootfs / 'certificate.pem')
                self.assertEqual(settings.server.custom_https_private_key, host_rootfs / 'private-key.pem')
                config_module.SaveConfig(settings)

            saved_config = YAML().load(config_path.read_text(encoding='utf-8'))
            self.assertEqual(saved_config['server']['custom_https_certificate'], '/certificate.pem')
            self.assertEqual(saved_config['server']['custom_https_private_key'], '/private-key.pem')
        finally:
            config_module._CONFIG = original_config  # pyright: ignore[reportPrivateUsage]
            config_module._CONFIG_YAML_PATH = original_config_path  # pyright: ignore[reportPrivateUsage]
            config_module._DOCKER_PATH_PREFIX = original_docker_path_prefix  # pyright: ignore[reportPrivateUsage]


class ReverseProxyMiddlewareTest(unittest.IsolatedAsyncioTestCase):
    async def call_middleware(
        self,
        *,
        peer: str,
        headers: list[tuple[bytes, bytes]] | None = None,
        trusted_proxy_cidrs: list[str] | None = None,
    ) -> tuple[list[Message], Scope | None]:
        captured_scope: Scope | None = None
        messages: list[Message] = []

        async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
            nonlocal captured_scope
            captured_scope = scope
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})

        middleware = ReverseProxyMiddleware(
            downstream,
            trusted_proxy_cidrs=trusted_proxy_cidrs or ['10.0.0.0/8'],
        )
        scope: Scope = {
            'type': 'http',
            'asgi': {'version': '3.0'},
            'http_version': '1.1',
            'method': 'GET',
            'scheme': 'http',
            'path': '/',
            'raw_path': b'/',
            'query_string': b'',
            'root_path': '',
            'headers': headers or [(b'host', b'internal:7000')],
            'client': (peer, 12345),
            'server': ('127.0.0.1', 7000),
        }

        async def receive() -> Message:
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def send(message: Message) -> None:
            messages.append(message)

        await middleware(scope, receive, send)
        return messages, captured_scope

    @staticmethod
    def response_status(messages: list[Message]) -> int:
        return next(message['status'] for message in messages if message['type'] == 'http.response.start')

    @staticmethod
    def headers(scope: Scope) -> dict[bytes, bytes]:
        return dict(scope['headers'])

    async def test_untrusted_direct_access_is_rejected_before_headers(self) -> None:
        messages, captured_scope = await self.call_middleware(
            peer='192.0.2.10',
            headers=[
                (b'host', b'internal:7000'),
                (b'x-forwarded-for', b'203.0.113.10'),
                (b'x-forwarded-proto', b'https'),
            ],
        )
        self.assertEqual(self.response_status(messages), 403)
        self.assertIsNone(captured_scope)

    async def test_forwarded_client_scheme_and_host_are_applied(self) -> None:
        messages, captured_scope = await self.call_middleware(
            peer='10.0.0.5',
            headers=[
                (b'host', b'internal:7000'),
                (b'x-forwarded-for', b'203.0.113.10'),
                (b'x-forwarded-proto', b'https'),
                (b'x-forwarded-host', b'tv.example.com'),
            ],
        )
        self.assertEqual(self.response_status(messages), 200)
        assert captured_scope is not None
        self.assertEqual(captured_scope['client'], ('203.0.113.10', 0))
        self.assertEqual(captured_scope['scheme'], 'https')
        self.assertEqual(self.headers(captured_scope)[b'host'], b'tv.example.com')

    async def test_multihop_xff_removes_trusted_proxies_from_right(self) -> None:
        _, captured_scope = await self.call_middleware(
            peer='10.0.0.5',
            headers=[
                (b'host', b'internal:7000'),
                (b'x-forwarded-for', b'203.0.113.10, 10.0.0.20'),
            ],
        )
        assert captured_scope is not None
        self.assertEqual(captured_scope['client'], ('203.0.113.10', 0))

    async def test_spoofed_leftmost_xff_is_not_used(self) -> None:
        _, captured_scope = await self.call_middleware(
            peer='10.0.0.5',
            headers=[
                (b'host', b'internal:7000'),
                (b'x-forwarded-for', b'198.51.100.99, 203.0.113.20'),
            ],
        )
        assert captured_scope is not None
        self.assertEqual(captured_scope['client'], ('203.0.113.20', 0))

    async def test_ipv6_proxy_and_client_are_supported(self) -> None:
        _, captured_scope = await self.call_middleware(
            peer='fd00::5',
            trusted_proxy_cidrs=['fd00::/8'],
            headers=[
                (b'host', b'internal:7000'),
                (b'x-forwarded-for', b'2001:db8::10, fd00::20'),
            ],
        )
        assert captured_scope is not None
        self.assertEqual(captured_scope['client'], ('2001:db8::10', 0))

    async def test_invalid_xff_is_rejected(self) -> None:
        messages, captured_scope = await self.call_middleware(
            peer='10.0.0.5',
            headers=[
                (b'host', b'internal:7000'),
                (b'x-forwarded-for', b'not-an-ip'),
            ],
        )
        self.assertEqual(self.response_status(messages), 400)
        self.assertIsNone(captured_scope)

    async def test_invalid_forwarded_host_is_rejected(self) -> None:
        messages, captured_scope = await self.call_middleware(
            peer='10.0.0.5',
            headers=[
                (b'host', b'internal:7000'),
                (b'x-forwarded-host', b'example.com/path'),
            ],
        )
        self.assertEqual(self.response_status(messages), 400)
        self.assertIsNone(captured_scope)


if __name__ == '__main__':
    unittest.main()
