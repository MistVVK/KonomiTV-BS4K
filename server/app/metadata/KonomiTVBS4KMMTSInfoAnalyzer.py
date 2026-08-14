from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta
from typing import Any, cast

from ariblib.constants import COMPONENT_TYPE, CONTENT_TYPE

from app import logging, schemas
from app.constants import JST, LIBRARY_PATH
from app.utils.TSInformation import TSInformation


class KonomiTVBS4KMMTSInfoAnalyzer:
    """libaribtlv の SI callback 結果から MMT/TLV 録画の番組情報を構築する。"""

    def __init__(self, recorded_video: schemas.RecordedVideo) -> None:
        """
        MMT/TLV 番組情報解析器を初期化する。

        Args:
            recorded_video (schemas.RecordedVideo): FFprobe 解析済みの録画ファイル情報。

        Returns:
            なし。
        """

        # helper へ渡す録画ファイルと、解析結果へ再設定する媒体情報を保持する。
        # analyze() だけから参照し、helper の JSON と同じ録画を必ず対応付ける。
        self.recorded_video = recorded_video

    def analyze(self) -> schemas.RecordedProgram | None:
        """
        MMT-SI のサービス・イベントから録画番組情報を取得する。

        Args:
            なし。

        Returns:
            schemas.RecordedProgram | None: 番組を特定できた場合の録画番組情報。
        """

        metadata = self.__extractMetadata()
        if metadata is None:
            return None
        services: list[dict[str, Any]] = [
            cast(dict[str, Any], item) for item in metadata.get('services', []) if isinstance(item, dict)
        ]
        events: list[dict[str, Any]] = [
            cast(dict[str, Any], item) for item in metadata.get('events', []) if isinstance(item, dict)
        ]
        tracks: list[dict[str, Any]] = [
            cast(dict[str, Any], item) for item in metadata.get('tracks', []) if isinstance(item, dict)
        ]
        tots: list[dict[str, Any]] = [
            cast(dict[str, Any], item) for item in metadata.get('tots', []) if isinstance(item, dict)
        ]

        # BS4K サービスを持つ context のうち映像・音声が最も多いものを録画対象として選び、
        # 同じ TLV に別ネットワークが含まれても、その track 数に引きずられないようにする。
        media_track_counts: dict[int, int] = {}
        for track in tracks:
            if track.get('kind') not in ('Video', 'Audio'):
                continue
            context_id = self.__parseInteger(track.get('context_id'))
            if context_id is not None:
                media_track_counts[context_id] = media_track_counts.get(context_id, 0) + 1
        bs4k_services = [
            service for service in services
            if self.__parseInteger(service.get('original_network_id')) == 0x000B
            and self.__parseInteger(service.get('context_id')) is not None
        ]
        bs4k_context_ids: list[int] = []
        for service in bs4k_services:
            service_context_id = self.__parseInteger(service.get('context_id'))
            if service_context_id is not None:
                bs4k_context_ids.append(service_context_id)
        selected_context_id = max(
            bs4k_context_ids,
            key=lambda context_id: media_track_counts.get(context_id, 0),
            default=None,
        )
        service = next(
            (
                item for item in bs4k_services
                if self.__parseInteger(item.get('context_id')) == selected_context_id
            ),
            None,
        )
        if service is None:
            logging.warning('[KonomiTVBS4KMMTSInfoAnalyzer] BS4K service was not found in MMT-SI metadata.')
            return None

        context_id = self.__parseInteger(service.get('context_id'))
        service_id = self.__parseInteger(service.get('service_id'))
        network_id = self.__parseInteger(service.get('original_network_id'))
        transport_stream_id = self.__parseInteger(service.get('tlv_stream_id'))
        if context_id is None or service_id is None or network_id != 0x000B:
            return None

        event_candidates = [
            event for event in events
            if self.__parseInteger(event.get('context_id')) == context_id
            and self.__parseInteger(event.get('service_id')) == service_id
            and self.__parseInteger(event.get('start_time_unix_milliseconds')) is not None
            and self.__parseInteger(event.get('duration_seconds')) is not None
        ]
        if len(event_candidates) == 0:
            logging.warning('[KonomiTVBS4KMMTSInfoAnalyzer] Event information was not found in MMT-SI metadata.')
            return None

        # 先頭・末尾 window の MH-TOT はファイル時刻より信頼できるため、録画範囲を先に確定する。
        tot_times = sorted(
            datetime.fromtimestamp(milliseconds / 1000, tz=JST)
            for item in tots
            if self.__parseInteger(item.get('context_id')) == context_id
            if (milliseconds := self.__parseInteger(item.get('time_unix_milliseconds'))) is not None
        )
        recording_time = (
            (tot_times[0], tot_times[-1])
            if len(tot_times) >= 2 and tot_times[0] < tot_times[-1]
            else None
        )

        event: dict[str, Any] | None = None
        if recording_time is not None:
            recording_start_time, recording_end_time = recording_time
            recording_midpoint = recording_start_time + (recording_end_time - recording_start_time) / 2

            # 録画範囲との重複秒数を最優先し、中点包含・p/f・中心距離で同点を解消する。
            def ScoreByRecordingTime(item: dict[str, Any]) -> tuple[float, int, int, float]:
                """
                MH-TOT の録画範囲との一致度を算出する。

                Args:
                    item (dict[str, Any]): 評価対象の MH-EIT event。

                Returns:
                    tuple[float, int, int, float]: 重複秒数、中点包含、p/f、中心距離の逆数値。
                """

                event_start = self.__eventStartTime(item)
                event_end = event_start + timedelta(
                    seconds=self.__parseInteger(item.get('duration_seconds')) or 0,
                )
                overlap_start = max(event_start, recording_start_time)
                overlap_end = min(event_end, recording_end_time)
                overlap_seconds = max(0.0, (overlap_end - overlap_start).total_seconds())
                contains_midpoint = int(event_start <= recording_midpoint < event_end)
                is_present_following = int(self.__parseInteger(item.get('table_id')) == 0x8B)
                event_midpoint = event_start + (event_end - event_start) / 2
                center_distance = abs((event_midpoint - recording_midpoint).total_seconds())
                return (overlap_seconds, contains_midpoint, is_present_following, -center_distance)

            candidate = max(event_candidates, key=ScoreByRecordingTime)
            if ScoreByRecordingTime(candidate)[0] > 0:
                event = candidate

        # p/f present を末尾側で観測できた場合は、先頭マージン中の前番組より後の通知を採用する。
        if event is None:
            present_events = [
                item for item in event_candidates
                if self.__parseInteger(item.get('table_id')) == 0x8B
                and self.__parseInteger(item.get('section_number')) == 0
            ]
            if present_events:
                event = max(
                    present_events,
                    key=lambda item: self.__parseInteger(item.get('input_offset')) or 0,
                )

        # 最後のフォールバックだけ、ファイル更新時刻と動画長から推定した録画中央を使う。
        if event is None:
            estimated_recording_midpoint = (
                self.recorded_video.file_modified_at - timedelta(seconds=self.recorded_video.duration / 2)
            )
            event = min(
                event_candidates,
                key=lambda item: abs(
                    (
                        self.__eventStartTime(item)
                        + timedelta(seconds=(self.__parseInteger(item.get('duration_seconds')) or 0) / 2)
                        - estimated_recording_midpoint
                    ).total_seconds()
                ),
            )
        event_start_time = self.__eventStartTime(event)
        event_duration = float(self.__parseInteger(event.get('duration_seconds')) or 0)
        if event_duration <= 0:
            return None

        channel_type = TSInformation.getNetworkType(network_id)
        if channel_type != 'BS4K':
            return None
        remocon_id = TSInformation.calculateRemoconID(channel_type, service_id)
        channel_number = asyncio.run(TSInformation.calculateChannelNumber(
            channel_type,
            network_id,
            service_id,
            remocon_id,
        ))
        channel = schemas.Channel(
            id=f'NID{network_id}-SID{service_id:03d}',
            display_channel_id=channel_type.lower() + channel_number,
            network_id=network_id,
            service_id=service_id,
            transport_stream_id=transport_stream_id,
            remocon_id=remocon_id,
            channel_number=channel_number,
            type=channel_type,
            name=TSInformation.formatString(str(service.get('service_name') or 'BS4K')),
            is_subchannel=TSInformation.calculateIsSubchannel(channel_type, service_id),
            is_radiochannel=False,
            is_watchable=False,
        )

        now = datetime.now(tz=JST)
        recorded_program = schemas.RecordedProgram(
            recorded_video=self.recorded_video,
            channel=channel,
            network_id=network_id,
            service_id=service_id,
            event_id=self.__parseInteger(event.get('event_id')),
            title=TSInformation.formatString(str(event.get('title') or '')),
            description=TSInformation.formatString(str(event.get('description') or '')),
            start_time=event_start_time,
            end_time=event_start_time + timedelta(seconds=event_duration),
            duration=event_duration,
            is_free=bool(event.get('free_ca_mode')) is False,
            genres=self.__buildGenres(event),
            created_at=now,
            updated_at=now,
        )
        if recording_time is not None:
            recording_start_time, recording_end_time = recording_time
            event_end_time = event_start_time + timedelta(seconds=event_duration)
            self.recorded_video.recording_start_time = recording_start_time
            self.recorded_video.recording_end_time = recording_end_time
            recorded_program.recording_start_margin = max(
                0.0,
                (event_start_time - recording_start_time).total_seconds(),
            )
            recorded_program.recording_end_margin = max(
                0.0,
                (recording_end_time - event_end_time).total_seconds(),
            )
            recorded_program.is_partially_recorded = (
                event_start_time < recording_start_time or recording_end_time < event_end_time
            )
        extended_description = TSInformation.formatString(str(event.get('extended_description') or '')).strip()
        if extended_description:
            recorded_program.detail = {'番組内容': extended_description}
        self.__applyAudioComponents(recorded_program, event)
        return recorded_program

    def __extractMetadata(self) -> dict[str, Any] | None:
        """
        固定 helper を実行して MMT-SI JSON を取得する。

        Args:
            なし。

        Returns:
            dict[str, Any] | None: helper の JSON。失敗時は None。
        """

        try:
            process = subprocess.run(
                [LIBRARY_PATH['KonomiTVBS4KTLVMetadata'], self.recorded_video.file_path],
                capture_output=True,
                timeout=40,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as ex:
            logging.warning('[KonomiTVBS4KMMTSInfoAnalyzer] MMT-SI helper execution failed.', exc_info=ex)
            return None
        if process.returncode != 0:
            logging.warning(
                '[KonomiTVBS4KMMTSInfoAnalyzer] MMT-SI helper returned an error. '
                f'[returncode: {process.returncode}]'
            )
            return None
        try:
            payload = json.loads(process.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as ex:
            logging.warning('[KonomiTVBS4KMMTSInfoAnalyzer] MMT-SI helper returned invalid JSON.', exc_info=ex)
            return None
        return cast(dict[str, Any], payload) if isinstance(payload, dict) else None

    @staticmethod
    def __parseInteger(value: object) -> int | None:
        """
        JSON 値を厳密な整数へ変換する。

        Args:
            value (object): helper JSON の値。

        Returns:
            int | None: 整数化できる値。失敗時は None。
        """

        if isinstance(value, bool):
            return None
        try:
            return int(cast(Any, value))
        except (TypeError, ValueError):
            return None

    @classmethod
    def __eventStartTime(cls, event: dict[str, Any]) -> datetime:
        """
        イベント開始時刻を JST の datetime へ変換する。

        Args:
            event (dict[str, Any]): helper のイベント情報。

        Returns:
            datetime: JST のイベント開始時刻。
        """

        milliseconds = cls.__parseInteger(event.get('start_time_unix_milliseconds')) or 0
        return datetime.fromtimestamp(milliseconds / 1000, tz=JST)

    @classmethod
    def __buildGenres(cls, event: dict[str, Any]) -> list[schemas.Genre]:
        """
        MMT の content descriptor を API ジャンルへ変換する。

        Args:
            event (dict[str, Any]): helper のイベント情報。

        Returns:
            list[schemas.Genre]: 既知ジャンルだけを変換した一覧。
        """

        genres: list[schemas.Genre] = []
        source_genres = event.get('genres')
        if not isinstance(source_genres, list):
            return genres
        for source_genre in source_genres:
            if not isinstance(source_genre, dict):
                continue
            level1 = cls.__parseInteger(source_genre.get('level1'))
            level2 = cls.__parseInteger(source_genre.get('level2'))
            if level1 is None or level2 is None:
                continue
            content_type = CONTENT_TYPE.get(level1)
            if content_type is None:
                continue
            major, middle_types = content_type
            middle = middle_types.get(level2)
            if middle is None:
                continue
            genres.append({
                'major': str(major).replace('／', '・'),
                'middle': str(middle).replace('／', '・'),
            })
        return genres

    def __applyAudioComponents(
        self,
        recorded_program: schemas.RecordedProgram,
        event: dict[str, Any],
    ) -> None:
        """
        MMT 音声コンポーネントの言語・種別を番組と録画トラックへ反映する。

        Args:
            recorded_program (schemas.RecordedProgram): 更新対象の録画番組情報。
            event (dict[str, Any]): helper のイベント情報。

        Returns:
            なし。
        """

        source_components = event.get('audio_components')
        if not isinstance(source_components, list):
            return
        components: list[dict[str, Any]] = [
            cast(dict[str, Any], item) for item in source_components if isinstance(item, dict)
        ]
        components.sort(key=lambda item: bool(item.get('main_component')) is False)
        for index, component in enumerate(components[:2]):
            component_type = self.__parseInteger(component.get('component_type'))
            audio_type = (
                COMPONENT_TYPE.get(0x02, {}).get(component_type, 'Unknown')
                if component_type is not None else 'Unknown'
            )
            language_code = str(component.get('language') or '')
            language = TSInformation.getISO639LanguageCodeName(language_code) if language_code else '日本語'
            secondary_code = str(component.get('secondary_language') or '')
            if 'デュアルモノ' in audio_type and secondary_code:
                language += '+' + TSInformation.getISO639LanguageCodeName(secondary_code)
            if index == 0:
                recorded_program.primary_audio_type = audio_type
                recorded_program.primary_audio_language = language
            else:
                recorded_program.secondary_audio_type = audio_type
                recorded_program.secondary_audio_language = language
            if index < len(self.recorded_video.audio_tracks):
                self.recorded_video.audio_tracks[index]['language'] = language
                if 'デュアルモノ' in audio_type:
                    self.recorded_video.audio_tracks[index]['channel'] = 'Dual Mono'
                    self.recorded_video.audio_tracks[index]['is_dual_mono'] = True
