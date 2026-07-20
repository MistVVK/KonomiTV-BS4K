import pickle
import ssl
import unittest
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import uvicorn

from KonomiTV import MultiListenerUvicornServer


class _FakeLifespan:
    def __init__(self) -> None:
        self.state = {'shared': object()}
        self.should_exit = False
        self.startup_calls = 0
        self.shutdown_calls = 0

    async def startup(self) -> None:
        self.startup_calls += 1

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class _FakeListenerServer:
    def __init__(self) -> None:
        self.close_calls = 0
        self.wait_closed_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    async def wait_closed(self) -> None:
        self.wait_closed_calls += 1


class _FakeLoop:
    def __init__(self, *, fail_on_call: int | None = None) -> None:
        self.create_server_calls: list[dict[str, Any]] = []
        self.create_server_attempts = 0
        self.fail_on_call = fail_on_call

    async def create_server(
        self,
        protocol_factory: Callable[..., Any],
        *,
        sock: Any,
        ssl: Any,
        backlog: int,
    ) -> _FakeListenerServer:
        self.create_server_attempts += 1
        if self.create_server_attempts == self.fail_on_call:
            raise OSError('listener startup failed')
        protocol = protocol_factory(self)
        listener_server = _FakeListenerServer()
        self.create_server_calls.append({
            'protocol': protocol,
            'sock': sock,
            'ssl': ssl,
            'backlog': backlog,
            'listener_server': listener_server,
        })
        return listener_server


class _FakeConfig:
    def __init__(
        self,
        *,
        name: str,
        ssl: object,
        protocol_calls: list[dict[str, Any]],
        loaded: bool,
    ) -> None:
        self.name = name
        self.ssl = ssl
        self.backlog = 128
        self.loaded = loaded
        self.load_calls = 0
        self.timeout_graceful_shutdown = 1
        self.workers = 1

        def ProtocolFactory(**kwargs: Any) -> dict[str, Any]:
            protocol = {'listener': name, **kwargs}
            protocol_calls.append(protocol)
            return protocol

        self.http_protocol_class = ProtocolFactory

    def load(self) -> None:
        self.load_calls += 1
        self.loaded = True


class MultiListenerUvicornServerTest(unittest.IsolatedAsyncioTestCase):
    async def test_listener_specific_ssl_and_shared_state_are_applied(self) -> None:
        protocol_calls: list[dict[str, Any]] = []
        primary_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        secondary_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        primary_config = _FakeConfig(
            name = 'primary',
            ssl = primary_ssl,
            protocol_calls = protocol_calls,
            loaded = True,
        )
        secondary_config = _FakeConfig(
            name = 'secondary',
            ssl = secondary_ssl,
            protocol_calls = protocol_calls,
            loaded = False,
        )
        server = MultiListenerUvicornServer([
            cast(uvicorn.Config, primary_config),
            cast(uvicorn.Config, secondary_config),
        ])
        lifespan = _FakeLifespan()
        server.lifespan = cast(Any, lifespan)
        primary_socket = Mock(name='primary_socket')
        secondary_socket = Mock(name='secondary_socket')
        loop = _FakeLoop()

        with patch('asyncio.get_running_loop', return_value=loop):
            await server.startup(sockets=[primary_socket, secondary_socket])

        self.assertEqual(lifespan.startup_calls, 1)
        self.assertEqual(secondary_config.load_calls, 1)
        self.assertEqual(len(loop.create_server_calls), 2)
        self.assertIs(loop.create_server_calls[0]['sock'], primary_socket)
        self.assertIs(loop.create_server_calls[0]['ssl'], primary_ssl)
        self.assertIs(loop.create_server_calls[1]['sock'], secondary_socket)
        self.assertIs(loop.create_server_calls[1]['ssl'], secondary_ssl)

        self.assertEqual([call['listener'] for call in protocol_calls], ['primary', 'secondary'])
        for protocol in protocol_calls:
            self.assertIs(protocol['server_state'], server.server_state)
            self.assertIs(protocol['app_state'], lifespan.state)

    async def test_shutdown_closes_primary_and_secondary_listeners_with_one_lifespan(self) -> None:
        protocol_calls: list[dict[str, Any]] = []
        primary_config = _FakeConfig(
            name = 'primary',
            ssl = object(),
            protocol_calls = protocol_calls,
            loaded = True,
        )
        secondary_config = _FakeConfig(
            name = 'secondary',
            ssl = object(),
            protocol_calls = protocol_calls,
            loaded = True,
        )
        server = MultiListenerUvicornServer([
            cast(uvicorn.Config, primary_config),
            cast(uvicorn.Config, secondary_config),
        ])
        lifespan = _FakeLifespan()
        server.lifespan = cast(Any, lifespan)
        primary_socket = Mock(name='primary_socket')
        secondary_socket = Mock(name='secondary_socket')
        loop = _FakeLoop()

        with patch('asyncio.get_running_loop', return_value=loop):
            await server.startup(sockets=[primary_socket, secondary_socket])

        listener_servers = [
            cast(_FakeListenerServer, call['listener_server'])
            for call in loop.create_server_calls
        ]
        with patch('uvicorn.server.asyncio.sleep', new=AsyncMock()):
            await server.shutdown(sockets=[primary_socket, secondary_socket])

        self.assertEqual(lifespan.startup_calls, 1)
        self.assertEqual(lifespan.shutdown_calls, 1)
        for listener_server in listener_servers:
            self.assertEqual(listener_server.close_calls, 1)
            self.assertEqual(listener_server.wait_closed_calls, 1)
        primary_socket.close.assert_called_once_with()
        secondary_socket.close.assert_called_once_with()

    async def test_startup_requires_one_socket_per_listener(self) -> None:
        config = _FakeConfig(
            name = 'primary',
            ssl = object(),
            protocol_calls = [],
            loaded = True,
        )
        server = MultiListenerUvicornServer([cast(uvicorn.Config, config)])

        with self.assertRaisesRegex(ValueError, 'One pre-bound socket'):
            await server.startup(sockets=[])

    async def test_secondary_listener_failure_shuts_down_primary_listener_and_lifespan(self) -> None:
        protocol_calls: list[dict[str, Any]] = []
        primary_config = _FakeConfig(
            name = 'primary',
            ssl = object(),
            protocol_calls = protocol_calls,
            loaded = True,
        )
        secondary_config = _FakeConfig(
            name = 'secondary',
            ssl = object(),
            protocol_calls = protocol_calls,
            loaded = True,
        )
        server = MultiListenerUvicornServer([
            cast(uvicorn.Config, primary_config),
            cast(uvicorn.Config, secondary_config),
        ])
        lifespan = _FakeLifespan()
        server.lifespan = cast(Any, lifespan)
        primary_socket = Mock(name='primary_socket')
        secondary_socket = Mock(name='secondary_socket')
        loop = _FakeLoop(fail_on_call=2)

        with (
            patch('asyncio.get_running_loop', return_value=loop),
            patch('uvicorn.server.asyncio.sleep', new=AsyncMock()),
            self.assertRaisesRegex(OSError, 'listener startup failed'),
        ):
            await server.startup(sockets=[primary_socket, secondary_socket])

        self.assertEqual(lifespan.startup_calls, 1)
        self.assertEqual(lifespan.shutdown_calls, 1)
        self.assertFalse(server.started)
        primary_listener = cast(_FakeListenerServer, loop.create_server_calls[0]['listener_server'])
        self.assertEqual(primary_listener.close_calls, 1)
        self.assertEqual(primary_listener.wait_closed_calls, 1)
        primary_socket.close.assert_called_once_with()
        secondary_socket.close.assert_called_once_with()

    def test_at_least_one_listener_config_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, 'At least one'):
            MultiListenerUvicornServer([])

    def test_run_target_is_picklable_for_watchfiles_reload(self) -> None:
        primary_config = uvicorn.Config(
            'app.app:application',
            host = '127.0.0.1',
            port = 7000,
            log_config = None,
        )
        secondary_config = uvicorn.Config(
            'app.app:application',
            host = '127.0.0.1',
            port = 7200,
            log_config = None,
        )
        server = MultiListenerUvicornServer([primary_config, secondary_config])

        restored_run = pickle.loads(pickle.dumps(server.run))

        self.assertTrue(callable(restored_run))
        restored_server = restored_run.__self__
        self.assertIsInstance(restored_server, MultiListenerUvicornServer)
        self.assertEqual(len(restored_server.listener_configs), 2)
