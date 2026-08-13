import asyncio
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, Request
from httpx import ASGITransport
from httpx import AsyncClient as HTTPXAsyncClient

from app import schemas
from app.CompatibilityAPI import (
    CreateCompatibilityAPI,
    KomorebiReservationRequestMiddleware,
    KomorebiResponseMiddleware,
    PortDispatchApplication,
    PreserveReservationSettingsForKomorebi,
    TransformRecordedProgramForKomorebi,
    ValidateCompatibilityLiveStreamQuality,
    histories_router,
)
from app.constants import VERSION
from app.routers import LiveStreamsRouter


def BuildRecordedProgramResponse() -> dict[str, Any]:
    """互換変換テスト用の録画番組レスポンスを構築する。"""

    return {
        'id': 42,
        'title': 'テスト番組 第1話',
        'series_title': 'テスト番組',
        'series_id': 7,
        'episode_number': '1',
        'start_time': '2026-07-19T10:00:00+09:00',
        'end_time': '2026-07-19T10:30:00+09:00',
        'recorded_video': {
            'id': 9001,
            'status': 'Recorded',
            'recording_start_time': None,
            'recording_end_time': None,
            'has_video': True,
            'video_codec': None,
            'primary_audio_codec': None,
            'playback_index_state': 'Pending',
        },
    }


async def GetCompatibilityTestResponse(path: str):
    """録画番組互換ミドルウェアを通してテスト用 API を呼び出す。"""

    app = FastAPI()

    @app.get('/api/videos')
    async def VideosAPI():
        return {
            'total': 1,
            'recorded_programs': [BuildRecordedProgramResponse()],
        }

    @app.get('/api/videos/search')
    async def VideosSearchAPI():
        return {
            'total': 1,
            'recorded_programs': [BuildRecordedProgramResponse()],
        }

    @app.get('/api/videos/{video_id}')
    async def VideoAPI(video_id: int):
        del video_id
        return BuildRecordedProgramResponse()

    app.add_middleware(KomorebiResponseMiddleware)
    async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        return await client.get(path)


def AssertRecordedProgramIsKomorebiCompatible(recorded_program: dict[str, Any]) -> None:
    """録画番組が Komorebi の要求するフィールドへ変換済みであることを確認する。"""

    assert recorded_program['seriesName'] == 'テスト番組'
    assert recorded_program['isEpisodic'] is True
    assert recorded_program['isRecording'] is False
    assert recorded_program['playbackPosition'] == 0.0

    recorded_video = recorded_program['recorded_video']
    assert recorded_video['id'] == 42
    assert recorded_video['recording_start_time'] == recorded_program['start_time']
    assert recorded_video['recording_end_time'] == recorded_program['end_time']
    assert recorded_video['video_codec'] == ''
    assert recorded_video['audio_codec'] == ''
    assert recorded_video['has_key_frames'] is True


def test_recorded_program_list_is_transformed_for_komorebi() -> None:
    response = asyncio.run(GetCompatibilityTestResponse('/api/videos'))

    assert response.status_code == 200
    AssertRecordedProgramIsKomorebiCompatible(response.json()['recorded_programs'][0])
    assert response.headers['content-length'] == str(len(response.content))


def test_recorded_program_search_is_transformed_for_komorebi() -> None:
    response = asyncio.run(GetCompatibilityTestResponse('/api/videos/search'))

    assert response.status_code == 200
    AssertRecordedProgramIsKomorebiCompatible(response.json()['recorded_programs'][0])


def test_recorded_program_detail_is_transformed_for_komorebi() -> None:
    response = asyncio.run(GetCompatibilityTestResponse('/api/videos/42'))

    assert response.status_code == 200
    AssertRecordedProgramIsKomorebiCompatible(response.json())


def test_recorded_program_with_failed_index_is_not_playable() -> None:
    app = FastAPI()
    recorded_program = BuildRecordedProgramResponse()
    recorded_program['recorded_video']['playback_index_state'] = 'Failed'

    @app.get('/api/videos/{video_id}')
    async def VideoAPI(video_id: int):
        del video_id
        return recorded_program

    app.add_middleware(KomorebiResponseMiddleware)

    async def GetResponse():
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            return await client.get('/api/videos/42')

    response = asyncio.run(GetResponse())
    assert response.json()['recorded_video']['has_key_frames'] is False


def test_recorded_program_with_analyzing_index_is_not_playable() -> None:
    recorded_program = BuildRecordedProgramResponse()
    recorded_program['recorded_video']['playback_index_state'] = 'Analyzing'

    TransformRecordedProgramForKomorebi(recorded_program)

    assert recorded_program['recorded_video']['has_key_frames'] is False


def test_histories_api_uses_local_only_compatibility_behavior() -> None:
    app = FastAPI()
    app.include_router(histories_router)

    async def GetResponses():
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            get_response = await client.get('/api/histories')
            post_response = await client.post('/api/histories', json={
                'program_id': '42',
                'playback_position': 123.5,
            })
            return get_response, post_response

    get_response, post_response = asyncio.run(GetResponses())
    assert get_response.status_code == 200
    assert get_response.json() == []
    assert post_response.status_code == 204
    assert post_response.content == b''


def test_compatibility_user_is_available_without_authentication() -> None:
    app = CreateCompatibilityAPI()

    async def GetResponse():
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            return await client.get('/api/users/me')

    response = asyncio.run(GetResponse())
    assert response.status_code == 200
    assert response.json() == {
        'id': 0,
        'name': 'Komorebi',
        'pinned_channel_ids': [],
    }


def test_reservation_string_folders_are_transformed() -> None:
    request_data = {
        'program_id': 'NID1-SID2-EID3',
        'record_settings': {
            'recording_folders': ['/recordings/primary'],
        },
    }

    transformed = KomorebiReservationRequestMiddleware.TransformRequestData(request_data)

    assert transformed['record_settings']['recording_folders'] == [{
        'recording_folder_path': '/recordings/primary',
        'recording_file_name_template': None,
        'is_oneseg_separate_recording_folder': False,
    }]


def test_reservation_response_folders_and_nullable_numbers_are_transformed() -> None:
    app = FastAPI()

    @app.get('/api/recording/reservations')
    async def ReservationsAPI():
        return {
            'total': 1,
            'reservations': [{
                'id': 1,
                'record_settings': {
                    'recording_folders': [{
                        'recording_folder_path': '/recordings/primary',
                        'recording_file_name_template': None,
                        'is_oneseg_separate_recording_folder': False,
                    }],
                    'recording_start_margin': None,
                    'recording_end_margin': None,
                    'forced_tuner_id': None,
                },
            }],
        }

    app.add_middleware(KomorebiResponseMiddleware)

    async def GetResponse():
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            return await client.get('/api/recording/reservations')

    response = asyncio.run(GetResponse())
    assert response.status_code == 200
    record_settings = response.json()['reservations'][0]['record_settings']
    assert record_settings['recording_folders'] == ['/recordings/primary']
    assert record_settings['recording_start_margin'] == 0
    assert record_settings['recording_end_margin'] == 0
    assert record_settings['forced_tuner_id'] == 0


def test_reservation_request_body_is_replayed_after_transformation() -> None:
    app = FastAPI()

    @app.post('/api/recording/reservations')
    async def AddReservationAPI(request: Request):
        return await request.json()

    app.add_middleware(KomorebiReservationRequestMiddleware)

    async def GetResponse():
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            return await client.post('/api/recording/reservations', json={
                'program_id': 'NID1-SID2-EID3',
                'record_settings': {'recording_folders': ['/recordings/primary']},
            })

    response = asyncio.run(GetResponse())
    assert response.status_code == 200
    assert response.json()['record_settings']['recording_folders'][0]['recording_folder_path'] == '/recordings/primary'


def test_reservation_update_preserves_settings_komorebi_cannot_represent() -> None:
    existing_record_settings = schemas.RecordSettings(
        recording_folders=[schemas.RecordingFolder(
            recording_folder_path='/recordings/primary',
            recording_file_name_template='$title$-$date$.ts',
            is_oneseg_separate_recording_folder=True,
        )],
        recording_start_margin=None,
        recording_end_margin=None,
        forced_tuner_id=None,
    )
    komorebi_record_settings = schemas.RecordSettings(
        recording_folders=[schemas.RecordingFolder(
            recording_folder_path='/recordings/primary',
        )],
        recording_start_margin=0,
        recording_end_margin=0,
        forced_tuner_id=0,
    )

    preserved = PreserveReservationSettingsForKomorebi(
        komorebi_record_settings,
        existing_record_settings,
    )

    assert preserved.recording_folders[0].recording_file_name_template == '$title$-$date$.ts'
    assert preserved.recording_folders[0].is_oneseg_separate_recording_folder is True
    assert preserved.recording_start_margin is None
    assert preserved.recording_end_margin is None
    assert preserved.forced_tuner_id is None


def test_port_dispatch_uses_destination_port_and_main_lifespan() -> None:
    calls: list[tuple[str, str]] = []

    async def MainApp(scope, receive, send):
        del receive, send
        calls.append(('main', scope['type']))

    async def CompatibilityApp(scope, receive, send):
        del receive, send
        calls.append(('compatibility', scope['type']))

    app = PortDispatchApplication(MainApp, CompatibilityApp, compatibility_port=7210)

    async def Receive():
        return {'type': 'http.disconnect'}

    async def Send(message):
        del message

    async def DispatchScopes() -> None:
        await app({'type': 'lifespan'}, Receive, Send)
        await app({'type': 'http', 'server': ('127.0.0.77', 7010)}, Receive, Send)
        await app({'type': 'http', 'server': ('127.0.0.77', 7210)}, Receive, Send)
        await app({'type': 'websocket', 'server': ('127.0.0.77', 7210)}, Receive, Send)

    asyncio.run(DispatchScopes())
    assert calls == [
        ('main', 'lifespan'),
        ('main', 'http'),
        ('compatibility', 'http'),
        ('compatibility', 'websocket'),
    ]


def test_compatibility_app_does_not_publish_web_ui_or_server_settings() -> None:
    app = CreateCompatibilityAPI()
    paths = {route.path for route in app.routes}
    method_paths = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, 'methods', set()) or set()
    }

    assert app.version == VERSION
    assert '/api/channels' in paths
    assert '/api/videos' in paths
    assert '/api/histories' in paths
    assert ('GET', '/api/videos/search') in method_paths
    assert ('GET', '/api/recording/conditions/{reservation_condition_id}') in method_paths
    assert ('GET', '/api/streams/video/{video_id}/{quality}/video/playlist') in method_paths
    assert ('GET', '/api/streams/video/{video_id}/{quality}/audio/{rendition_id}/segment') in method_paths
    assert ('GET', '/api/streams/video/{video_id}/{quality}/subtitle/{subtitle_index}/webvtt') in method_paths
    assert ('GET', '/api/streams/video/{video_id}/{quality}/offline-stream') not in method_paths
    assert ('GET', '/api/streams/video/{video_id}/{quality}/offline-estimate') not in method_paths
    assert ('POST', '/api/streams/video/{video_id}/{quality}/offline-jobs') not in method_paths
    assert ('GET', '/api/streams/video/{video_id}/offline-jobs/{job_id}') not in method_paths
    assert ('GET', '/api/streams/video/{video_id}/offline-jobs/{job_id}/download') not in method_paths
    assert ('DELETE', '/api/streams/video/{video_id}/offline-jobs/{job_id}') not in method_paths
    assert ('PUT', '/api/recording/reservations/{reservation_id}') in method_paths
    assert ('POST', '/api/videos/{video_id}/reanalyze') not in method_paths
    assert ('POST', '/api/videos/{video_id}/detect-cm-sections') not in method_paths
    assert ('POST', '/api/videos/{video_id}/thumbnail/regenerate') not in method_paths
    assert ('DELETE', '/api/videos/{video_id}') not in method_paths
    assert not any(path.startswith('/api/settings') for path in paths)
    assert not any(path.startswith('/api/maintenance') for path in paths)
    assert '/api/version' not in paths
    assert '/{file:path}' not in paths


def test_compatibility_app_rejects_unlisted_video_operations() -> None:
    app = CreateCompatibilityAPI()

    async def GetResponse():
        async with HTTPXAsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            return await client.post('/api/videos/42/reanalyze')

    response = asyncio.run(GetResponse())
    assert response.status_code == 404


def test_compatibility_stream_dependencies_force_legacy_codecs(monkeypatch) -> None:
    """互換 API の stream dependency は高度 codec query を通常 validator へ渡さない。"""

    class _TerrestrialChannel:

        is_radiochannel = False

    class _TerrestrialChannelQuery:

        async def get_or_none(self):
            return _TerrestrialChannel()

    monkeypatch.setattr(
        LiveStreamsRouter,
        'GetEncoderForLiveChannel',
        lambda _display_channel_id: 'FFmpeg',
    )
    monkeypatch.setattr(
        LiveStreamsRouter.Channel,
        'filter',
        lambda **_kwargs: _TerrestrialChannelQuery(),
    )
    stream_quality = asyncio.run(
        ValidateCompatibilityLiveStreamQuality('1080p', 'gr011')
    )
    assert stream_quality.encoding_options.video_codec == 'avc'
    assert stream_quality.encoding_options.video_bit_depth == 8
    assert stream_quality.encoding_options.audio_codec == 'aac'

    hevc_stream_quality = asyncio.run(
        ValidateCompatibilityLiveStreamQuality('1080p-hevc', 'gr011')
    )
    assert hevc_stream_quality.encoding_options.video_codec == 'hevc'
    assert hevc_stream_quality.encoding_options.audio_codec == 'aac'

    async def UnexpectedMainLiveCapability(*_args, **_kwargs):
        raise AssertionError('compatibility route must not require Stream Anchor capability')

    async def GetAvailableHEVC10BitCapability(*_args, **_kwargs):
        return SimpleNamespace(recorded_available=True)

    monkeypatch.setattr(
        LiveStreamsRouter.KonomiTVBS4KPlaybackCapabilityProbe,
        'getLiveCombinationCapability',
        classmethod(UnexpectedMainLiveCapability),
    )
    monkeypatch.setattr(
        LiveStreamsRouter.KonomiTVBS4KPlaybackCapabilityProbe,
        'getRecordedVideoCapability',
        classmethod(GetAvailableHEVC10BitCapability),
    )
    hevc_10bit_stream_quality = asyncio.run(
        ValidateCompatibilityLiveStreamQuality('1080p-hevc-10bit', 'gr011')
    )
    assert hevc_10bit_stream_quality.encoding_options.video_codec == 'hevc'
    assert hevc_10bit_stream_quality.encoding_options.video_bit_depth == 10

    async def GetUnavailableHEVC10BitCapability(*_args, **_kwargs):
        return SimpleNamespace(recorded_available=False)

    monkeypatch.setattr(
        LiveStreamsRouter.KonomiTVBS4KPlaybackCapabilityProbe,
        'getRecordedVideoCapability',
        classmethod(GetUnavailableHEVC10BitCapability),
    )
    downgraded_hevc_stream_quality = asyncio.run(
        ValidateCompatibilityLiveStreamQuality('1080p-hevc-10bit', 'gr011')
    )
    assert downgraded_hevc_stream_quality.encoding_options.video_codec == 'hevc'
    assert downgraded_hevc_stream_quality.encoding_options.video_bit_depth == 8
    assert downgraded_hevc_stream_quality.encoding_options.is_hevc_10bit_enabled is False

    compatibility_app = CreateCompatibilityAPI()
    openapi_paths = compatibility_app.openapi()['paths']
    live_parameters = openapi_paths[
        '/api/streams/live/{display_channel_id}/{quality}/mpegts'
    ]['get']['parameters']
    recorded_parameters = openapi_paths[
        '/api/streams/video/{video_id}/{quality}/playlist'
    ]['get']['parameters']
    assert {'video_codec', 'video_bit_depth', 'audio_codec'}.isdisjoint(
        parameter['name'] for parameter in live_parameters
    )
    assert {'video_codec', 'video_bit_depth', 'audio_codec', 'audio_track'}.isdisjoint(
        parameter['name'] for parameter in recorded_parameters
    )
