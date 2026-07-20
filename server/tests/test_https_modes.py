import socket
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import ValidationError
from ruamel.yaml import YAML
from starlette.types import Message, Receive, Scope, Send

import app as app_package
import app.config as config_module
from app.config import ResolveCompatibilityHTTPSSettings, ServerSettings
from app.utils.HTTPS import (
    BuildServerStartupSettings,
    GetAkebiAccessURLs,
    GetRequiredThirdpartyLibraries,
    ReverseProxyMiddleware,
)


class HTTPSModeConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        temporary_path = Path(self.temporary_directory.name)
        self.certificate_path = temporary_path / 'certificate.pem'
        self.private_key_path = temporary_path / 'private-key.pem'
        self.compatibility_certificate_path = temporary_path / 'compatibility-certificate.pem'
        self.compatibility_private_key_path = temporary_path / 'compatibility-private-key.pem'
        self.certificate_path.write_text('certificate', encoding='utf-8')
        self.private_key_path.write_text('private-key', encoding='utf-8')
        self.compatibility_certificate_path.write_text('compatibility-certificate', encoding='utf-8')
        self.compatibility_private_key_path.write_text('compatibility-private-key', encoding='utf-8')

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def server_settings(**overrides: object):
        return ServerSettings.model_validate(
            {'server': {'port': 65420, **overrides}},
            context={'bypass_validation': False},
        ).server

    def compatibility_settings(
        self,
        *,
        server_port: int = 65400,
        compatibility_port: int = 65420,
        https_mode: str = 'akebi',
        compatibility_https_mode: str = 'inherit',
        compatibility_overrides: dict[str, object] | None = None,
        used_ports: set[int] | None = None,
        **server_overrides: object,
    ) -> ServerSettings:
        with patch('app.config._GetUsedListenPorts', return_value=used_ports or set()):
            return ServerSettings.model_validate(
                {
                    'server': {
                        'port': server_port,
                        'https_mode': https_mode,
                        **server_overrides,
                    },
                    'compatibility_api': {
                        'enabled': True,
                        'port': compatibility_port,
                        'profile': 'KomorebiV1',
                        'https_mode': compatibility_https_mode,
                        **(compatibility_overrides or {}),
                    },
                },
                context={'bypass_validation': False},
            )

    def server_mode_overrides(self, https_mode: str) -> dict[str, object]:
        if https_mode == 'certificate':
            return {
                'custom_https_certificate': self.certificate_path,
                'custom_https_private_key': self.private_key_path,
            }
        if https_mode == 'reverse_proxy':
            return {
                'reverse_proxy_listen_address': '127.0.0.2',
                'trusted_proxy_cidrs': ['10.0.0.0/8'],
            }
        return {}

    def compatibility_mode_overrides(self, https_mode: str) -> dict[str, object]:
        if https_mode == 'certificate':
            return {
                'custom_https_certificate': self.compatibility_certificate_path,
                'custom_https_private_key': self.compatibility_private_key_path,
            }
        if https_mode == 'reverse_proxy':
            return {
                'reverse_proxy_listen_address': '127.0.0.3',
                'trusted_proxy_cidrs': ['192.0.2.0/24'],
            }
        return {}

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

    def test_resolved_compatibility_https_settings_cover_every_mode_pair(self) -> None:
        for main_mode in ('akebi', 'certificate', 'reverse_proxy'):
            for compatibility_mode in ('inherit', 'akebi', 'certificate', 'reverse_proxy'):
                with self.subTest(main_mode=main_mode, compatibility_mode=compatibility_mode):
                    settings = self.compatibility_settings(
                        https_mode=main_mode,
                        compatibility_https_mode=compatibility_mode,
                        compatibility_overrides=self.compatibility_mode_overrides(compatibility_mode),
                        **self.server_mode_overrides(main_mode),
                    )
                    resolved = ResolveCompatibilityHTTPSSettings(settings)
                    effective_mode = main_mode if compatibility_mode == 'inherit' else compatibility_mode

                    if compatibility_mode == 'inherit':
                        self.assertIs(resolved, settings.server)
                    else:
                        self.assertIs(resolved, settings.compatibility_api)
                    self.assertEqual(resolved.https_mode, effective_mode)

                    startup = BuildServerStartupSettings(resolved, port=settings.compatibility_api.port)
                    if effective_mode == 'akebi':
                        self.assertEqual((startup.host, startup.port), ('127.0.0.77', 65430))
                        self.assertTrue(startup.use_akebi)
                        self.assertIsNone(startup.ssl_certfile)
                    elif effective_mode == 'certificate':
                        expected_certificate = (
                            self.certificate_path
                            if compatibility_mode == 'inherit'
                            else self.compatibility_certificate_path
                        )
                        expected_private_key = (
                            self.private_key_path
                            if compatibility_mode == 'inherit'
                            else self.compatibility_private_key_path
                        )
                        self.assertEqual((startup.host, startup.port), ('0.0.0.0', 65420))
                        self.assertFalse(startup.use_akebi)
                        self.assertEqual(startup.ssl_certfile, str(expected_certificate))
                        self.assertEqual(startup.ssl_keyfile, str(expected_private_key))
                    else:
                        expected_host = '127.0.0.2' if compatibility_mode == 'inherit' else '127.0.0.3'
                        self.assertEqual((startup.host, startup.port), (expected_host, 65420))
                        self.assertFalse(startup.use_akebi)
                        self.assertIsNone(startup.ssl_certfile)

    def test_compatibility_api_defaults_to_disabled_komorebi_listener(self) -> None:
        with patch('app.config._GetUsedListenPorts', return_value=set()):
            settings = ServerSettings.model_validate({}, context={'bypass_validation': False})

        self.assertFalse(settings.compatibility_api.enabled)
        self.assertEqual(settings.compatibility_api.https_mode, 'inherit')
        self.assertEqual(settings.compatibility_api.port, 7200)
        self.assertEqual(settings.compatibility_api.profile, 'KomorebiV1')
        self.assertIsNone(settings.compatibility_api.custom_https_certificate)
        self.assertIsNone(settings.compatibility_api.custom_https_private_key)
        self.assertEqual(str(settings.compatibility_api.reverse_proxy_listen_address), '0.0.0.0')
        self.assertEqual(settings.compatibility_api.trusted_proxy_cidrs, [])

    def test_compatibility_inherit_and_akebi_reject_dedicated_https_settings(self) -> None:
        for compatibility_mode in ('inherit', 'akebi'):
            with self.subTest(compatibility_mode=compatibility_mode, setting='certificate'):
                with self.assertRaisesRegex(ValidationError, '専用の HTTPS 証明書は指定できません'):
                    self.compatibility_settings(
                        compatibility_https_mode=compatibility_mode,
                        compatibility_overrides={'custom_https_certificate': self.compatibility_certificate_path},
                    )
            with self.subTest(compatibility_mode=compatibility_mode, setting='trusted_proxy_cidrs'):
                with self.assertRaisesRegex(ValidationError, 'reverse_proxy の場合のみ'):
                    self.compatibility_settings(
                        compatibility_https_mode=compatibility_mode,
                        compatibility_overrides={'trusted_proxy_cidrs': ['192.0.2.0/24']},
                    )
            with self.subTest(compatibility_mode=compatibility_mode, setting='listen_address'):
                with self.assertRaisesRegex(ValidationError, 'reverse_proxy の場合のみ'):
                    self.compatibility_settings(
                        compatibility_https_mode=compatibility_mode,
                        compatibility_overrides={'reverse_proxy_listen_address': '127.0.0.3'},
                    )

    def test_compatibility_certificate_requires_its_own_certificate_pair(self) -> None:
        with self.assertRaisesRegex(ValidationError, '両方を指定'):
            self.compatibility_settings(
                compatibility_https_mode='certificate',
                compatibility_overrides={'custom_https_certificate': self.compatibility_certificate_path},
            )

        settings = self.compatibility_settings(
            compatibility_https_mode='certificate',
            compatibility_overrides=self.compatibility_mode_overrides('certificate'),
        )
        self.assertEqual(
            settings.compatibility_api.custom_https_certificate,
            self.compatibility_certificate_path,
        )
        self.assertEqual(
            settings.compatibility_api.custom_https_private_key,
            self.compatibility_private_key_path,
        )

        with self.assertRaisesRegex(ValidationError, 'reverse_proxy の場合のみ'):
            self.compatibility_settings(
                compatibility_https_mode='certificate',
                compatibility_overrides={
                    **self.compatibility_mode_overrides('certificate'),
                    'trusted_proxy_cidrs': ['192.0.2.0/24'],
                },
            )

    def test_compatibility_reverse_proxy_requires_cidrs_and_rejects_certificates(self) -> None:
        with self.assertRaisesRegex(ValidationError, '1件以上'):
            self.compatibility_settings(compatibility_https_mode='reverse_proxy')

        with self.assertRaisesRegex(ValidationError, '指定できません'):
            self.compatibility_settings(
                compatibility_https_mode='reverse_proxy',
                compatibility_overrides={
                    'custom_https_certificate': self.compatibility_certificate_path,
                    'custom_https_private_key': self.compatibility_private_key_path,
                    'trusted_proxy_cidrs': ['192.0.2.0/24'],
                },
            )

        settings = self.compatibility_settings(
            compatibility_https_mode='reverse_proxy',
            compatibility_overrides=self.compatibility_mode_overrides('reverse_proxy'),
        )
        self.assertEqual(str(settings.compatibility_api.reverse_proxy_listen_address), '127.0.0.3')
        self.assertEqual([str(cidr) for cidr in settings.compatibility_api.trusted_proxy_cidrs], ['192.0.2.0/24'])

    def test_main_and_compatibility_reserved_port_matrix(self) -> None:
        main_port = 65400
        for main_mode in ('akebi', 'certificate', 'reverse_proxy'):
            for compatibility_mode in ('inherit', 'akebi', 'certificate', 'reverse_proxy'):
                effective_compatibility_mode = main_mode if compatibility_mode == 'inherit' else compatibility_mode
                forbidden_ports = {main_port}
                if main_mode == 'akebi':
                    forbidden_ports.add(main_port + 10)
                if effective_compatibility_mode == 'akebi':
                    forbidden_ports.add(main_port - 10)

                for compatibility_port in (main_port - 10, main_port, main_port + 10):
                    with self.subTest(
                        main_mode=main_mode,
                        compatibility_mode=compatibility_mode,
                        compatibility_port=compatibility_port,
                    ):
                        validate = lambda: self.compatibility_settings(
                            server_port=main_port,
                            compatibility_port=compatibility_port,
                            https_mode=main_mode,
                            compatibility_https_mode=compatibility_mode,
                            compatibility_overrides=self.compatibility_mode_overrides(compatibility_mode),
                            **self.server_mode_overrides(main_mode),
                        )
                        if compatibility_port in forbidden_ports:
                            with self.assertRaisesRegex(ValidationError, '重複しています'):
                                validate()
                        else:
                            settings = validate()
                            self.assertEqual(settings.compatibility_api.port, compatibility_port)

    def test_enabled_compatibility_api_used_port_matrix(self) -> None:
        compatibility_port = 65420
        for main_mode in ('akebi', 'certificate', 'reverse_proxy'):
            for compatibility_mode in ('inherit', 'akebi', 'certificate', 'reverse_proxy'):
                effective_compatibility_mode = main_mode if compatibility_mode == 'inherit' else compatibility_mode
                for used_port in (compatibility_port, compatibility_port + 10):
                    with self.subTest(
                        main_mode=main_mode,
                        compatibility_mode=compatibility_mode,
                        used_port=used_port,
                    ):
                        validate = lambda: self.compatibility_settings(
                            compatibility_port=compatibility_port,
                            https_mode=main_mode,
                            compatibility_https_mode=compatibility_mode,
                            compatibility_overrides=self.compatibility_mode_overrides(compatibility_mode),
                            used_ports={used_port},
                            **self.server_mode_overrides(main_mode),
                        )
                        if used_port == compatibility_port or effective_compatibility_mode == 'akebi':
                            with self.assertRaisesRegex(ValidationError, '他のプロセスで使われている'):
                                validate()
                        else:
                            settings = validate()
                            self.assertEqual(settings.compatibility_api.port, compatibility_port)

    def test_disabled_compatibility_api_does_not_reserve_its_port(self) -> None:
        with patch('app.config._GetUsedListenPorts', return_value={7200, 7210}):
            settings = ServerSettings.model_validate(
                {
                    'server': {'port': 65400},
                    'compatibility_api': {'enabled': False, 'port': 7200},
                },
                context={'bypass_validation': False},
            )

        self.assertFalse(settings.compatibility_api.enabled)

    def test_bypass_validation_accepts_prevalidated_compatibility_collision(self) -> None:
        settings = ServerSettings.model_validate(
            {
                'server': {'port': 65400},
                'compatibility_api': {'enabled': True, 'port': 65410},
            },
            context={'bypass_validation': True},
        )
        self.assertTrue(settings.compatibility_api.enabled)

    def test_akebi_is_required_only_in_akebi_mode(self) -> None:
        self.assertIn('Akebi', GetRequiredThirdpartyLibraries('akebi'))
        self.assertNotIn('Akebi', GetRequiredThirdpartyLibraries('certificate'))
        self.assertNotIn('Akebi', GetRequiredThirdpartyLibraries('reverse_proxy'))

    def test_akebi_access_urls_follow_upstream_installer_format(self) -> None:
        address = namedtuple('Address', ('family', 'address'))
        with patch('app.utils.HTTPS.psutil.net_if_addrs', return_value={
            'lo': [address(socket.AF_INET, '127.0.0.1')],
            'enp2s0': [address(socket.AF_INET, '192.168.0.4')],
            'tailscale0': [address(socket.AF_INET, '100.64.0.10')],
            'link-local': [address(socket.AF_INET, '169.254.1.1')],
        }):
            self.assertEqual(GetAkebiAccessURLs(7001), [
                ('https://my.local.konomi.tv:7001/', 'ローカルホスト'),
                ('https://100-64-0-10.local.konomi.tv:7001/', 'tailscale0'),
                ('https://192-168-0-4.local.konomi.tv:7001/', 'enp2s0'),
            ])

    def test_akebi_access_urls_fall_back_to_localhost(self) -> None:
        with patch('app.utils.HTTPS.psutil.net_if_addrs', side_effect=PermissionError):
            self.assertEqual(GetAkebiAccessURLs(7001), [
                ('https://my.local.konomi.tv:7001/', 'ローカルホスト'),
            ])

    def test_certificate_paths_are_mapped_through_host_rootfs_in_docker(self) -> None:
        temporary_path = Path(self.temporary_directory.name)
        host_rootfs = temporary_path / 'host-rootfs'
        host_rootfs.mkdir()
        (host_rootfs / 'certificate.pem').write_text('certificate', encoding='utf-8')
        (host_rootfs / 'private-key.pem').write_text('private-key', encoding='utf-8')
        (host_rootfs / 'compatibility-certificate.pem').write_text('compatibility-certificate', encoding='utf-8')
        (host_rootfs / 'compatibility-private-key.pem').write_text('compatibility-private-key', encoding='utf-8')
        config_path = temporary_path / 'config.yaml'
        config_path.write_text(
            'server:\n'
            "    https_mode: 'certificate'\n"
            '    port: 65420\n'
            "    custom_https_certificate: '/certificate.pem'\n"
            "    custom_https_private_key: '/private-key.pem'\n"
            'compatibility_api:\n'
            '    enabled: true\n'
            "    https_mode: 'certificate'\n"
            '    port: 65440\n'
            "    custom_https_certificate: '/compatibility-certificate.pem'\n"
            "    custom_https_private_key: '/compatibility-private-key.pem'\n",
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
                self.assertEqual(
                    settings.compatibility_api.custom_https_certificate,
                    host_rootfs / 'compatibility-certificate.pem',
                )
                self.assertEqual(
                    settings.compatibility_api.custom_https_private_key,
                    host_rootfs / 'compatibility-private-key.pem',
                )
                config_module.SaveConfig(settings)

            saved_config = YAML().load(config_path.read_text(encoding='utf-8'))
            self.assertEqual(saved_config['server']['custom_https_certificate'], '/certificate.pem')
            self.assertEqual(saved_config['server']['custom_https_private_key'], '/private-key.pem')
            self.assertEqual(
                saved_config['compatibility_api']['custom_https_certificate'],
                '/compatibility-certificate.pem',
            )
            self.assertEqual(
                saved_config['compatibility_api']['custom_https_private_key'],
                '/compatibility-private-key.pem',
            )
        finally:
            config_module._CONFIG = original_config  # pyright: ignore[reportPrivateUsage]
            config_module._CONFIG_YAML_PATH = original_config_path  # pyright: ignore[reportPrivateUsage]
            config_module._DOCKER_PATH_PREFIX = original_docker_path_prefix  # pyright: ignore[reportPrivateUsage]

    def test_compatibility_api_settings_are_added_to_old_config_and_saved(self) -> None:
        temporary_path = Path(self.temporary_directory.name)
        config_path = temporary_path / 'config.yaml'
        config_path.write_text(
            'server:\n'
            '    port: 65420\n',
            encoding='utf-8',
        )

        original_config = config_module._CONFIG  # pyright: ignore[reportPrivateUsage]
        original_config_path = config_module._CONFIG_YAML_PATH  # pyright: ignore[reportPrivateUsage]
        try:
            config_module._CONFIG = None  # pyright: ignore[reportPrivateUsage]
            config_module._CONFIG_YAML_PATH = config_path  # pyright: ignore[reportPrivateUsage]
            test_logging = SimpleNamespace(debug=Mock(), error=Mock())
            with (
                patch.object(app_package, 'logging', test_logging, create=True),
                patch.dict('sys.modules', {'app.logging': test_logging}),
                patch('app.utils.GetPlatformEnvironment', return_value='Linux'),
            ):
                settings = config_module.LoadConfig(bypass_validation=True)
                self.assertFalse(settings.compatibility_api.enabled)
                self.assertEqual(settings.compatibility_api.https_mode, 'inherit')
                self.assertEqual(settings.compatibility_api.port, 7200)
                self.assertIs(ResolveCompatibilityHTTPSSettings(settings), settings.server)

                settings.compatibility_api.enabled = True
                settings.compatibility_api.port = 65440
                config_module.SaveConfig(settings)

            saved_config = YAML().load(config_path.read_text(encoding='utf-8'))
            self.assertEqual(saved_config['compatibility_api'], {
                'enabled': True,
                'https_mode': 'inherit',
                'port': 65440,
                'profile': 'KomorebiV1',
                'custom_https_certificate': None,
                'custom_https_private_key': None,
                'reverse_proxy_listen_address': '0.0.0.0',
                'trusted_proxy_cidrs': [],
            })
        finally:
            config_module._CONFIG = original_config  # pyright: ignore[reportPrivateUsage]
            config_module._CONFIG_YAML_PATH = original_config_path  # pyright: ignore[reportPrivateUsage]


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
