"""test_acp_client.py から起動する ACP v1 fake stdio agent。"""

from __future__ import annotations

import json
import os
import select
import signal
import sys
import time
from pathlib import Path
from typing import Any


MODE = os.environ.get('FAKE_ACP_MODE', 'normal')
LOG_PATH = Path(os.environ['FAKE_ACP_LOG'])
OUTPUT = os.environ.get('FAKE_ACP_OUTPUT', '{"choice_id":"candidate-1","confidence":0.95}')
EXPECTED_CWD = os.environ['FAKE_ACP_CWD']
PRE_SESSION_UPDATE_MODE = os.environ.get('FAKE_ACP_PRE_SESSION_UPDATE_MODE', '')


def Log(kind: str, message: dict[str, Any]) -> None:
    with LOG_PATH.open('a', encoding='utf-8') as file:
        file.write(json.dumps({'kind': kind, 'message': message}, ensure_ascii=False) + '\n')


def Read() -> dict[str, Any]:
    line = sys.stdin.readline()
    if line == '':
        raise EOFError
    message = json.loads(line)
    Log('received', message)
    return message


def Send(message: dict[str, Any]) -> None:
    Log('sent', message)
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(',', ':')) + '\n')
    sys.stdout.flush()


def Respond(request: dict[str, Any], result: dict[str, Any]) -> None:
    Send({'jsonrpc': '2.0', 'id': request['id'], 'result': result})


def SendSessionUpdate(update: dict[str, Any]) -> None:
    Send({
        'jsonrpc': '2.0',
        'method': 'session/update',
        'params': {
            'sessionId': 'fake-session',
            'update': update,
        },
    })


def FinishPrompt(prompt: dict[str, Any]) -> None:
    first_half = len(OUTPUT) // 2
    for output_chunk in (OUTPUT[:first_half], OUTPUT[first_half:]):
        SendSessionUpdate({
            'sessionUpdate': 'agent_message_chunk',
            'content': {'type': 'text', 'text': output_chunk},
        })
    Respond(prompt, {'stopReason': 'end_turn'})


def AssertRequest(request: dict[str, Any], method: str) -> None:
    assert request['jsonrpc'] == '2.0'
    assert request['method'] == method
    assert isinstance(request['id'], int)


def Main() -> None:
    initialize = Read()
    AssertRequest(initialize, 'initialize')
    assert initialize['params']['protocolVersion'] == 1
    assert initialize['params']['clientCapabilities'] == {}
    if MODE == 'episode_early_tool':
        # session/new より前の tool trace を実行証明へ混入させようとする agent。
        Send({
            'jsonrpc': '2.0',
            'method': 'session/update',
            'params': {
                'sessionId': 'not-created',
                'update': {
                    'sessionUpdate': 'tool_call',
                    'toolCallId': 'episode-search',
                    'title': 'Web search',
                    'kind': 'search',
                    'rawInput': {'type': 'web_search'},
                    'status': 'pending',
                },
            },
        })
        while True:
            time.sleep(0.05)
    if MODE == 'require_vertex_auth':
        # 非対話 method を広告し、authenticate 後だけ session/new を許可する。
        Respond(initialize, {
            'protocolVersion': 1,
            'agentCapabilities': {},
            'authMethods': [
                {
                    'id': 'vertex-ai',
                    'name': 'Vertex AI',
                    'description': 'Use Vertex AI',
                },
            ],
        })
        authenticate = Read()
        AssertRequest(authenticate, 'authenticate')
        assert authenticate['params'] == {'methodId': 'vertex-ai'}
        Respond(authenticate, {})
    elif MODE == 'require_interactive_auth':
        # Grok 相当: 対話 method のみ。session/new は認証エラーを返す。
        Respond(initialize, {
            'protocolVersion': 1,
            'agentCapabilities': {},
            'authMethods': [
                {
                    'id': 'grok.com',
                    'name': 'Grok',
                    'description': 'Sign in with Grok',
                },
            ],
        })
        session_new = Read()
        AssertRequest(session_new, 'session/new')
        Send({
            'jsonrpc': '2.0',
            'id': session_new['id'],
            'error': {
                'code': -32000,
                'message': 'Authentication required',
                'data': 'no auth method id provided',
            },
        })
        return
    else:
        Respond(initialize, {
            'protocolVersion': 1,
            'agentCapabilities': {},
            'authMethods': [],
        })
        if MODE in {
            'benign_mcp_catalog_notification',
            'nonempty_mcp_catalog_notification',
            'extra_mcp_catalog_notification',
        }:
            params: dict[str, Any] = {
                'mcpServers': (
                    [{'name': 'unexpected'}]
                    if MODE == 'nonempty_mcp_catalog_notification'
                    else []
                ),
            }
            if MODE == 'extra_mcp_catalog_notification':
                params['unexpected'] = True
            Send({
                'jsonrpc': '2.0',
                'method': '_vendor.example/mcp/servers_updated',
                'params': params,
            })
        elif MODE in {
            'benign_extension_notification',
            'unsafe_extension_notification',
        }:
            Send({
                'jsonrpc': '2.0',
                'method': (
                    '_vendor.example/terminal/executed'
                    if MODE == 'unsafe_extension_notification'
                    else '_vendor.example/status_updated'
                ),
                'params': {'version': 2},
            })
            if MODE == 'unsafe_extension_notification':
                while True:
                    time.sleep(0.05)

    session_new = Read()
    AssertRequest(session_new, 'session/new')
    assert session_new['params'] == {'cwd': EXPECTED_CWD, 'mcpServers': []}
    if PRE_SESSION_UPDATE_MODE == 'available_commands':
        # Grok 0.2.112 相当: session/new response より先に、同じ sessionId の
        # command catalog metadata を複数通知する。
        for command_name in ('help', 'review'):
            Send({
                'jsonrpc': '2.0',
                'method': 'session/update',
                'params': {
                    'sessionId': 'fake-session',
                    '_meta': {'provider': 'fake-grok'},
                    'update': {
                        'sessionUpdate': 'available_commands_update',
                        'availableCommands': [{
                            'name': command_name,
                            'description': f'{command_name} command',
                        }],
                        '_meta': {'version': 1},
                    },
                },
            })
    elif PRE_SESSION_UPDATE_MODE == 'wrong_session':
        Send({
            'jsonrpc': '2.0',
            'method': 'session/update',
            'params': {
                'sessionId': 'different-session',
                'update': {
                    'sessionUpdate': 'available_commands_update',
                    'availableCommands': [],
                },
            },
        })
    elif PRE_SESSION_UPDATE_MODE == 'tool_call':
        Send({
            'jsonrpc': '2.0',
            'method': 'session/update',
            'params': {
                'sessionId': 'fake-session',
                'update': {
                    'sessionUpdate': 'tool_call',
                    'toolCallId': 'pre-session-tool',
                    'title': 'terminal',
                    'kind': 'execute',
                    'status': 'pending',
                },
            },
        })
        while True:
            time.sleep(0.05)
    session_result: dict[str, Any] = {'sessionId': 'fake-session'}
    if MODE != 'legacy_model' and MODE != 'no_model':
        session_result['configOptions'] = [{
            'id': 'model',
            'name': 'Model',
            'category': 'model',
            'type': 'select',
            'currentValue': 'default',
            'options': [
                {'value': 'default', 'name': 'Default'},
                {'value': 'test-model', 'name': 'Test Model'},
            ],
        }]
        if MODE == 'reasoning_config':
            session_result['configOptions'].append({
                'id': 'reasoning_effort',
                'name': 'Reasoning effort',
                'category': 'thought_level',
                'type': 'select',
                'currentValue': 'medium',
                'options': [
                    {'value': 'low', 'name': 'Low'},
                    {'value': 'medium', 'name': 'Medium'},
                    {'value': 'high', 'name': 'High'},
                ],
            })
    elif MODE == 'legacy_model':
        session_result['models'] = {
            'currentModelId': 'default',
            'availableModels': [{'modelId': 'test-model', 'name': 'Test Model'}],
        }
    Respond(session_new, session_result)

    if MODE == 'stdin_block':
        # session 確立後は stdin を一切読まず、大きな prompt の drain を意図的に詰まらせる。
        # SIGTERM も無視し、client が無通信 timeout 後に SIGKILL と wait まで行うことを検証する。
        Log('stdin_block_started', {'pid': os.getpid()})
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(0.05)

    # model / reasoning_effort の set_config_option や set_model を任意回数受け付ける。
    prompt = Read()
    while prompt.get('method') in {'session/set_config_option', 'session/set_model'}:
        if prompt['method'] == 'session/set_config_option':
            params = prompt['params']
            assert params['sessionId'] == 'fake-session'
            assert params['configId'] in {'model', 'reasoning_effort'}
            if params['configId'] == 'model':
                assert params['value'] == 'test-model'
            else:
                assert params['value'] == 'high'
            Respond(prompt, {'configOptions': session_result.get('configOptions', [])})
        else:
            assert MODE == 'legacy_model'
            assert prompt['params']['sessionId'] == 'fake-session'
            # provider 固有 ID へ推論深さを連結せず、裸のモデルだけを渡す。
            assert prompt['params']['modelId'] == 'test-model'
            Respond(prompt, {})
        prompt = Read()

    AssertRequest(prompt, 'session/prompt')
    assert prompt['params']['sessionId'] == 'fake-session'
    assert isinstance(prompt['params']['prompt'], list)
    assert prompt['params']['prompt'][0]['type'] == 'text'

    if MODE == 'timeout':
        # 無通信タイムアウト検証用: 進捗を出さず SIGTERM も無視してハングする。
        # 以前は thought chunk を送り続けていたが、それでは無通信打ち切りを再現できない。
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            readable, _, _ = select.select([sys.stdin], [], [], 0.05)
            if not readable:
                continue
            message = Read()
            if message.get('method') == 'session/cancel':
                Log('cancel_received', message)

    if MODE == 'slow_but_active':
        # 無通信タイムアウトより長く進捗を出し続けたあと、正常に完了する。
        # timeout_sec より総実行時間が長くても、行が届いていれば打ち切られないことを示す。
        active_until = time.monotonic() + 2.5
        while time.monotonic() < active_until:
            Send({
                'jsonrpc': '2.0',
                'method': 'session/update',
                'params': {
                    'sessionId': 'fake-session',
                    'update': {
                        'sessionUpdate': 'agent_thought_chunk',
                        'content': {'type': 'text', 'text': 'still running'},
                    },
                },
            })
            time.sleep(0.2)
        # 通常経路へ落として最終 JSON を返す（この直後の共通処理が OUTPUT を送る）

    if MODE == 'active_forever':
        # 無通信 window より短い間隔で進捗を永久送信し、絶対実行時間だけを発火させる。
        # cancel は読み取って記録し、hard timeout 経路が session 回収へ到達したことを示す。
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            readable, _, _ = select.select([sys.stdin], [], [], 0.05)
            if readable:
                message = Read()
                if message.get('method') == 'session/cancel':
                    Log('cancel_received', message)
                    return
            Send({
                'jsonrpc': '2.0',
                'method': 'session/update',
                'params': {
                    'sessionId': 'fake-session',
                    'update': {
                        'sessionUpdate': 'agent_thought_chunk',
                        'content': {'type': 'text', 'text': 'still running'},
                    },
                },
            })

    if MODE == 'term_ignoring_child':
        # leader は cancel 後に終了し、子孫だけが TERM を無視して残るケースを再現する。
        child_pid = os.fork()
        if child_pid == 0:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            while True:
                time.sleep(0.05)
        Log('spawned_term_ignoring_child', {'pid': child_pid})
        while True:
            readable, _, _ = select.select([sys.stdin], [], [], 0.05)
            if not readable:
                continue
            message = Read()
            if message.get('method') == 'session/cancel':
                Log('cancel_received', message)
                # leader だけ終了し、process group に TERM 無視の子を残す。
                return

    if MODE == 'oversized_output':
        # 1 行は上限未満だが、総出力が上限を超える多数の chunk を送る。
        chunk_text = 'x' * 8192
        for _ in range(40):
            Send({
                'jsonrpc': '2.0',
                'method': 'session/update',
                'params': {
                    'sessionId': 'fake-session',
                    'update': {
                        'sessionUpdate': 'agent_message_chunk',
                        'content': {'type': 'text', 'text': chunk_text},
                    },
                },
            })
        while True:
            message = Read()
            if message.get('method') == 'session/cancel':
                Log('cancel_received', message)
                return

    if MODE == 'stdin_flood_permissions':
        # 親の拒否応答を読まず permission request を大量送信し、stdin pipe を詰まらせる。
        # 大きな reject option を選ばせて応答を合計 4 MiB 超にし、cancel 前の通常応答だけで
        # pipe backpressure を確実に発生させる敵対的 agent。
        for request_id in range(10_000, 10_200):
            reject_option_id = f'reject-{request_id}-' + ('x' * 32_768)
            Send({
                'jsonrpc': '2.0',
                'id': request_id,
                'method': 'session/request_permission',
                'params': {
                    'sessionId': 'fake-session',
                    'toolCall': {'toolCallId': f'flood-{request_id}'},
                    'options': [{
                        'optionId': reject_option_id,
                        'name': 'Reject always',
                        'kind': 'reject_always',
                    }],
                },
            })
        # 以降は親の入力を一切読まず生存し、cleanup の TERM/KILL を待つ。
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(0.05)

    if MODE in {'tool_call', 'candidate_web_tool'}:
        is_web_tool = MODE == 'candidate_web_tool'
        Send({
            'jsonrpc': '2.0',
            'method': 'session/update',
            'params': {
                'sessionId': 'fake-session',
                'update': {
                    'sessionUpdate': 'tool_call',
                    'toolCallId': 'unsafe-tool',
                    'title': 'Web search' if is_web_tool else 'Unsafe tool',
                    'kind': 'search' if is_web_tool else 'execute',
                    'rawInput': {'type': 'web_search'} if is_web_tool else {},
                    'status': 'pending',
                },
            },
        })
        while True:
            message = Read()
            if message.get('method') == 'session/cancel':
                Log('cancel_received', message)
                return

    if MODE.startswith('episode_'):
        if MODE == 'episode_no_tool':
            FinishPrompt(prompt)
            return

        if MODE == 'episode_unsafe_tool':
            SendSessionUpdate({
                'sessionUpdate': 'tool_call',
                'toolCallId': 'episode-search',
                'title': 'Terminal command',
                'kind': 'execute',
                'rawInput': {'type': 'shell', 'command': 'curl https://example.com'},
                'status': 'pending',
            })
            while True:
                message = Read()
                if message.get('method') == 'session/cancel':
                    Log('cancel_received', message)
                    return

        if MODE == 'episode_unsupported_web_tool':
            SendSessionUpdate({
                'sessionUpdate': 'tool_call',
                'toolCallId': 'episode-search',
                'title': 'Provider renamed operation',
                'kind': 'search',
                'status': 'in_progress',
            })
            cancel = Read()
            assert cancel['method'] == 'session/cancel'
            Log('cancel_received', cancel)
            return

        if MODE == 'episode_private_fetch':
            Send({
                'jsonrpc': '2.0',
                'id': 903,
                'method': 'session/request_permission',
                'params': {
                    'sessionId': 'fake-session',
                    'toolCall': {
                        'toolCallId': 'episode-fetch',
                        'title': 'Fetching content from: http://127.0.0.1/private',
                        'kind': 'fetch',
                        'status': 'pending',
                        'rawInput': {
                            'type': 'web_fetch',
                            'url': 'http://127.0.0.1/private',
                            'urls': ['http://127.0.0.1/private'],
                        },
                    },
                    'options': [{
                        'optionId': 'reject-once',
                        'name': 'Reject once',
                        'kind': 'reject_once',
                    }],
                },
            })
            permission_response = Read()
            assert permission_response['result'] == {'outcome': {'outcome': 'cancelled'}}
            cancel = Read()
            assert cancel['method'] == 'session/cancel'
            Log('cancel_received', cancel)
            return

        if MODE in {
            'episode_codex_other_no_permission',
            'episode_search_no_permission',
            'episode_mixed_fetch_empty_urls',
            'episode_mixed_fetch_public',
        }:
            is_codex = MODE == 'episode_codex_other_no_permission'
            search_source_url = (
                'https://example.com/codex-other-source'
                if is_codex
                else 'https://example.com/search-source'
            )
            search_raw_input: dict[str, Any] = (
                {
                    'type': 'webSearch',
                    'id': 'episode-search',
                    'query': 'official episode',
                    'action': {'type': 'other'},
                }
                if is_codex
                else {
                    'type': 'google_web_search',
                }
            )
            search_title = (
                'Web search'
                if is_codex
                else 'Searching the web for: "official episode"'
            )
            search_start: dict[str, Any] = {
                'sessionUpdate': 'tool_call',
                'toolCallId': 'episode-search',
                'title': search_title,
                'kind': 'search',
                'rawInput': search_raw_input,
                'status': 'in_progress',
            }
            if is_codex is False:
                search_start.update({'content': [], 'locations': []})
            SendSessionUpdate(search_start)

            search_complete = {
                **search_start,
                'sessionUpdate': 'tool_call_update',
                'status': 'completed',
                'rawOutput': {
                    'sources': [{
                        'url': search_source_url,
                        'title': 'Verified search source',
                    }],
                },
            }
            SendSessionUpdate(search_complete)

            if MODE in {
                'episode_codex_other_no_permission',
                'episode_search_no_permission',
            }:
                FinishPrompt(prompt)
                return

        if MODE in {
            'episode_mixed_fetch_empty_urls',
            'episode_mixed_fetch_public',
            'episode_standalone_fetch_public',
        }:
            is_empty_target = MODE == 'episode_mixed_fetch_empty_urls'
            fetch_url = 'https://example.com/fetch-source'
            fetch_raw_input: dict[str, Any] = {
                'type': 'web_fetch',
                'urls': [] if is_empty_target else [fetch_url],
            }
            fetch_title = (
                'Processing URLs and instructions from prompt: "find the official page"'
                if is_empty_target
                else f'Fetching content from: {fetch_url}'
            )
            if is_empty_target is False:
                fetch_raw_input['url'] = fetch_url
            Send({
                'jsonrpc': '2.0',
                'id': 902,
                'method': 'session/request_permission',
                'params': {
                    'sessionId': 'fake-session',
                    'toolCall': {
                        'toolCallId': 'episode-fetch',
                        'title': fetch_title,
                        'kind': 'fetch',
                        'content': [],
                        'locations': [],
                        'status': 'pending',
                        'rawInput': fetch_raw_input,
                    },
                    'options': [{
                        'optionId': 'allow-once',
                        'name': 'Allow once',
                        'kind': 'allow_once',
                    }, {
                        'optionId': 'allow-always',
                        'name': 'Allow always',
                        'kind': 'allow_always',
                    }, {
                        'optionId': 'reject-once',
                        'name': 'Reject once',
                        'kind': 'reject_once',
                    }],
                },
            })
            permission_response = Read()
            assert permission_response['result'] == {
                'outcome': {
                    'outcome': 'selected',
                    'optionId': 'reject-once',
                },
            }
            FinishPrompt(prompt)
            return

        if MODE in {'episode_grok', 'episode_grok_streamed_source'}:
            tool_call = {
                'sessionUpdate': 'tool_call',
                'toolCallId': 'episode-search',
                'title': 'web_search',
                'kind': 'search',
                'rawInput': {'tool': 'web_search', 'query': 'official episode'},
                'status': 'pending',
            }
            source_url = 'https://example.com/grok-source'
        elif MODE in {
            'episode_web_search',
            'episode_current',
            'episode_permission_first',
            'episode_permission_then_tool_call',
        }:
            is_permission_first = MODE in {
                'episode_permission_first',
                'episode_permission_then_tool_call',
            }
            tool_call = {
                'sessionUpdate': 'tool_call',
                'toolCallId': 'episode-search',
                'title': (
                    'Fetching content from: https://example.com/source'
                    if is_permission_first
                    else 'Searching the web for: "official episode"'
                ),
                'kind': 'fetch' if is_permission_first else 'search',
                'content': [],
                'locations': [],
                'status': 'pending',
            }
            if is_permission_first:
                tool_call['rawInput'] = {
                    'type': 'web_fetch',
                    'url': 'https://example.com/source',
                    'urls': ['https://example.com/source'],
                }
            source_url = 'https://example.com/source'
        else:
            raw_input: dict[str, Any] = {
                'type': 'web_search',
                'query': 'official episode',
            }
            if MODE == 'episode_codex_open_page':
                raw_input['action'] = {
                    'type': 'openPage',
                    'url': 'https://example.com/codex-source',
                }
            tool_call = {
                'sessionUpdate': 'tool_call',
                'toolCallId': 'episode-search',
                'title': (
                    'Open page: https://example.com/codex-source'
                    if MODE == 'episode_codex_open_page'
                    else 'Web search'
                ),
                'kind': 'search',
                'rawInput': raw_input,
                'status': 'pending',
            }
            source_url = 'https://example.com/codex-source'

        if MODE not in {
            'episode_permission_first',
            'episode_permission_then_tool_call',
        }:
            Send({
                'jsonrpc': '2.0',
                'method': 'session/update',
                'params': {
                    'sessionId': 'fake-session',
                    'update': tool_call,
                },
            })
        permission_options = [{
            'optionId': 'allow-once',
            'name': 'Allow once',
            'kind': 'allow_once',
        }, {
            'optionId': 'allow-always',
            'name': 'Allow always',
            'kind': 'allow_always',
        }, {
            'optionId': 'reject-once',
            'name': 'Reject once',
            'kind': 'reject_once',
        }]
        if MODE == 'episode_permanent_permission':
            permission_options = [{
                'optionId': 'allow-always',
                'name': 'Allow always',
                'kind': 'allow_always',
            }]
        elif MODE == 'episode_ambiguous_permission':
            permission_options = [{
                'optionId': 'allow-once-a',
                'name': 'Allow once A',
                'kind': 'allow_once',
            }, {
                'optionId': 'allow-once-b',
                'name': 'Allow once B',
                'kind': 'allow_once',
            }]
        elif MODE == 'episode_aliased_permission':
            permission_options = [{
                'optionId': 'shared-option',
                'name': 'Allow once',
                'kind': 'allow_once',
            }, {
                'optionId': 'shared-option',
                'name': 'Allow always',
                'kind': 'allow_always',
            }]
        Send({
            'jsonrpc': '2.0',
            'id': 901,
            'method': 'session/request_permission',
            'params': {
                'sessionId': 'fake-session',
                'toolCall': (
                    {
                        key: value
                        for key, value in tool_call.items()
                        if key != 'sessionUpdate'
                    }
                    if MODE in {
                        'episode_permission_first',
                        'episode_permission_then_tool_call',
                    }
                    else {'toolCallId': 'episode-search'}
                ),
                'options': permission_options,
            },
        })
        permission_response = Read()
        if MODE in {
            'episode_permanent_permission',
            'episode_ambiguous_permission',
            'episode_aliased_permission',
        }:
            assert permission_response['result'] == {'outcome': {'outcome': 'cancelled'}}
            FinishPrompt(prompt)
            return
        assert permission_response['result'] == {
            'outcome': {
                'outcome': 'selected',
                'optionId': 'allow-once',
            },
        }
        if MODE == 'episode_permission_then_tool_call':
            Send({
                'jsonrpc': '2.0',
                'method': 'session/update',
                'params': {
                    'sessionId': 'fake-session',
                    'update': tool_call,
                },
            })
        elif MODE == 'episode_duplicate_tool_after_permission':
            Send({
                'jsonrpc': '2.0',
                'method': 'session/update',
                'params': {
                    'sessionId': 'fake-session',
                    'update': tool_call,
                },
            })
            cancel = Read()
            assert cancel['method'] == 'session/cancel'
            Log('cancel_received', cancel)
            return

        completed_update: dict[str, Any] = {
            'sessionUpdate': 'tool_call_update',
            'toolCallId': 'episode-search',
            'status': 'completed',
        }
        if MODE in {
            'episode_permission_first',
            'episode_permission_then_tool_call',
        }:
            completed_update.update({
                'title': tool_call['title'],
                'kind': tool_call['kind'],
                'content': [],
                'locations': [],
            })
        if MODE == 'episode_update_unsafe_metadata':
            completed_update['command'] = 'cat /etc/passwd'
        elif MODE == 'episode_tool_failed':
            completed_update['status'] = 'failed'
        elif MODE == 'episode_grok':
            completed_update['rawOutput'] = {
                'action': {
                    'type': 'search',
                    'query': 'official episode',
                    'sources': [
                        {'url': source_url, 'title': 'Verified official source'},
                        {'url': source_url, 'title': 'Duplicate source'},
                        {'url': 'http://127.0.0.1/private', 'title': 'Private source'},
                    ],
                },
                'id': 'fake-grok-search',
                'status': 'completed',
            }
        elif MODE == 'episode_streamed_source':
            Send({
                'jsonrpc': '2.0',
                'method': 'session/update',
                'params': {
                    'sessionId': 'fake-session',
                    'update': {
                        'sessionUpdate': 'tool_call_update',
                        'toolCallId': 'episode-search',
                        'status': 'in_progress',
                        'rawOutput': {
                            'sources': [{
                                'url': source_url,
                                'title': 'Verified official source',
                            }],
                        },
                    },
                },
            })
        elif MODE not in {
            'episode_no_source',
            'episode_codex_open_page',
            'episode_current',
        }:
            completed_update['rawOutput'] = {
                'sources': [
                    {'url': source_url, 'title': 'Verified official source'},
                    {'url': source_url, 'title': 'Duplicate source'},
                    {'url': 'http://127.0.0.1/private', 'title': 'Private source'},
                    {'url': 'https://user:password@example.com/private', 'title': 'Credential URL'},
                ],
            }
        else:
            completed_update['content'] = {
                'type': 'text',
                'text': 'Search completed without a source URL.',
            }
        Send({
            'jsonrpc': '2.0',
            'method': 'session/update',
            'params': {
                'sessionId': 'fake-session',
                'update': completed_update,
            },
        })
        if MODE == 'episode_update_unsafe_metadata':
            cancel = Read()
            assert cancel['method'] == 'session/cancel'
            Log('cancel_received', cancel)
            return

        first_half = len(OUTPUT) // 2
        for output_chunk in (OUTPUT[:first_half], OUTPUT[first_half:]):
            Send({
                'jsonrpc': '2.0',
                'method': 'session/update',
                'params': {
                    'sessionId': 'fake-session',
                    'update': {
                        'sessionUpdate': 'agent_message_chunk',
                        'content': {'type': 'text', 'text': output_chunk},
                    },
                },
            })
        Respond(prompt, {'stopReason': 'end_turn'})
        return

    if MODE in {'large_line', 'oversized_line'}:
        # asyncio の subprocess StreamReader limit がアプリ側の 1 MiB 契約と一致することを検証する。
        repeated_text_bytes = 70_000 if MODE == 'large_line' else 1_048_576
        Send({
            'jsonrpc': '2.0',
            'method': 'session/update',
            'params': {
                'sessionId': 'fake-session',
                'update': {
                    'sessionUpdate': 'agent_message_chunk',
                    'content': {'type': 'text', 'text': 'x' * repeated_text_bytes},
                },
            },
        })

    first_half = len(OUTPUT) // 2
    Send({
        'jsonrpc': '2.0',
        'method': 'session/update',
        'params': {
            'sessionId': 'fake-session',
            'update': {
                'sessionUpdate': 'agent_message_chunk',
                'content': {'type': 'text', 'text': OUTPUT[:first_half]},
            },
        },
    })

    if MODE in {
        'normal',
        'strict_output',
        'unsafe_permission',
        'candidate_aliased_permission',
    }:
        options = [{
            'optionId': 'allow-once',
            'name': 'Allow once',
            'kind': 'allow_once',
        }]
        if MODE != 'unsafe_permission':
            options.extend([
                {
                    'optionId': 'reject-once',
                    'name': 'Reject once',
                    'kind': 'reject_once',
                },
                {
                    'optionId': 'reject-always',
                    'name': 'Reject always',
                    'kind': 'reject_always',
                },
            ])
        if MODE == 'candidate_aliased_permission':
            options = [{
                'optionId': 'shared-option',
                'name': 'Allow once',
                'kind': 'allow_once',
            }, {
                'optionId': 'shared-option',
                'name': 'Reject always',
                'kind': 'reject_always',
            }]
        Send({
            'jsonrpc': '2.0',
            'id': 900,
            'method': 'session/request_permission',
            'params': {
                'sessionId': 'fake-session',
                'toolCall': {'toolCallId': 'permission-test'},
                'options': options,
            },
        })
        permission_response = Read()
        if MODE in {'unsafe_permission', 'candidate_aliased_permission'}:
            assert permission_response['result'] == {'outcome': {'outcome': 'cancelled'}}
            cancel = Read()
            assert cancel['method'] == 'session/cancel'
            Log('cancel_received', cancel)
            return
        assert permission_response['result'] == {
            'outcome': {
                'outcome': 'selected',
                'optionId': 'reject-always',
            },
        }

    Send({
        'jsonrpc': '2.0',
        'method': 'session/update',
        'params': {
            'sessionId': 'fake-session',
            'update': {
                'sessionUpdate': 'agent_message_chunk',
                'content': {'type': 'text', 'text': OUTPUT[first_half:]},
            },
        },
    })
    Respond(prompt, {'stopReason': 'end_turn'})


if __name__ == '__main__':
    try:
        Main()
    except Exception as ex:
        Log('error', {'type': type(ex).__name__, 'message': str(ex)})
        raise
