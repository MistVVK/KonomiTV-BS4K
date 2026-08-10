
import asyncio
import concurrent.futures
import ipaddress
import math
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import httpx
import psutil
import ruamel.yaml
import ruamel.yaml.scalarstring
from pydantic import (
    BaseModel,
    DirectoryPath,
    Field,
    FilePath,
    IPvAnyAddress,
    IPvAnyNetwork,
    PositiveInt,
    UrlConstraints,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import Url

from app.constants import (
    API_REQUEST_HEADERS,
    BASE_DIR,
)
from app.utils.HostPath import (
    HostPathError,
    NormalizeHostPath,
    ToHostPath,
    ToRuntimePath,
)
from app.utils.TSInformation import TerrestrialRegion


def _GetUsedListenPorts() -> set[int]:
    """現在の KonomiTV-BS4K プロセス群を除き、他プロセスが待ち受けている TCP ポートを取得する。"""

    current_process = psutil.Process()
    used_ports: set[int] = set()
    for connection in psutil.net_connections():
        if connection.status != 'LISTEN' or connection.pid is None:
            continue

        # サーバー設定更新時は、稼働中の Uvicorn・リローダー・Akebi がすでに対象ポートを使用している。
        # 現在プロセスと同じプロセスツリーに属する待ち受けは除外し、外部プロセスとの衝突だけを検出する。
        try:
            process = psutil.Process(connection.pid)
            if (
                process.pid == current_process.pid or
                process.pid == current_process.ppid() or
                process.ppid() == current_process.pid or
                process.ppid() == current_process.ppid()
            ):
                continue
        except Exception:
            pass

        if connection.laddr is not None:
            used_ports.add(cast(Any, connection.laddr).port)
    return used_ports


# クライアント設定を表す Pydantic モデル (クライアント設定同期用 API で利用)
# デバイス間で同期するとかえって面倒なことになりそうな設定は除外されている
# 詳細は client/src/services/Settings.ts と client/src/stores/SettingsStore.ts を参照

class ClientSettings(BaseModel):
    # 0 は未同期の初期値として正当なので、正の値に限定しない
    last_synced_at: Annotated[float, Field(ge=0.0, allow_inf_nan=False)] = 0.0
    # showed_panel_last_time: 同期無効
    # selected_twitter_panel_account: 同期無効
    # twitter_panel_post_targets: 同期無効
    saved_twitter_hashtags: list[str] = []
    mylist: list[dict[str, Any]] = []
    watched_history: list[dict[str, Any]] = []
    # lshaped_screen_crop_enabled: 同期無効
    # lshaped_screen_crop_zoom_level: 同期無効
    # lshaped_screen_crop_x_position: 同期無効
    # lshaped_screen_crop_y_position: 同期無効
    # lshaped_screen_crop_zoom_origin: 同期無効
    pinned_channel_ids: list[str] = []
    timetable_channel_width: Literal['Wide', 'Normal', 'Narrow'] = 'Normal'
    timetable_hour_height: Literal['Wide', 'Normal', 'Narrow'] = 'Normal'
    timetable_hover_expand: bool = False
    timetable_dim_shopping_programs: bool = True
    # 番組表のジャンル別のハイライト色
    # キーはジャンル名 (大分類)、値はハイライトカラー
    # クライアント側の ILocalClientSettingsDefault.timetable_genre_colors と一致させる必要がある
    timetable_genre_colors: dict[str, Literal['White', 'Pink', 'Red', 'Orange', 'Yellow', 'Lime', 'Teal', 'Cyan', 'Blue', 'Ochre', 'Brown']] = {
        'ニュース・報道': 'White',
        '情報・ワイドショー': 'White',
        'ドキュメンタリー・教養': 'Blue',
        'スポーツ': 'Cyan',
        'ドラマ': 'Pink',
        'アニメ・特撮': 'Yellow',
        'バラエティ': 'Lime',
        '音楽': 'Orange',
        '映画': 'Brown',
        '劇場・公演': 'Ochre',
        '趣味・教育': 'Teal',
        '福祉': 'White',
        'その他': 'White',
    }
    show_gr_channels: bool = True
    show_oneseg_channels: bool = True
    show_bs_channels: bool = True
    show_cs_channels: bool = True
    show_catv_channels: bool = True
    show_sky_channels: bool = True
    show_bs4k_channels: bool = True
    ui_theme: Literal[
        'KonomiClassic',
        'KonomiNavy',
        'KonomiCharcoal',
        'DeepPlum',
        'NightBlue',
        'DayBlue',
        'KonomiIvory',
        'PearlBlue',
        'WarmCream',
        'CoolGray',
    ] = 'KonomiClassic'
    show_player_background_image: bool = True
    use_pure_black_player_background: bool = False
    tv_channel_sort_by_jikkyo_force: bool = False
    tv_channel_up_down_buttons_reverse: bool = False
    tv_channel_selection_requires_alt_key: bool = False
    use_28hour_clock: bool = False
    show_original_broadcast_time_during_playback: bool = False
    panel_display_state: Literal['RestorePreviousState', 'AlwaysDisplay', 'AlwaysFold'] = 'RestorePreviousState'
    tv_panel_active_tab: Literal['Program', 'Channel', 'Comment', 'Twitter'] = 'Program'
    video_panel_active_tab: Literal['RecordedProgram', 'Series', 'Comment', 'Twitter'] = 'RecordedProgram'
    video_series_sort_key: Literal['SeasonEpisode', 'BroadcastDate', 'Title'] = 'SeasonEpisode'
    video_series_sort_direction: Literal['Asc', 'Desc'] = 'Asc'
    video_watched_history_max_count: PositiveInt = 50
    # tv_streaming_quality: 同期無効
    # tv_streaming_quality_cellular: 同期無効
    # bs4k_streaming_quality: 同期無効
    # bs4k_streaming_quality_cellular: 同期無効
    # tv_encoding_codec: 同期無効
    # tv_encoding_codec_cellular: 同期無効
    # bs4k_tv_encoding_codec: 同期無効
    # bs4k_tv_encoding_codec_cellular: 同期無効
    # tv_low_latency_mode: 同期無効
    # tv_low_latency_mode_cellular: 同期無効
    # tv_low_latency_mode_for_bs4k: 同期無効
    # tv_low_latency_mode_for_bs4k_cellular: 同期無効
    # tv_24fps_mode: 同期無効
    # tv_24fps_mode_cellular: 同期無効
    # video_streaming_quality: 同期無効
    # video_streaming_quality_cellular: 同期無効
    # video_encoding_codec: 同期無効
    # video_encoding_codec_cellular: 同期無効
    # bs4k_video_encoding_codec: 同期無効
    # bs4k_video_encoding_codec_cellular: 同期無効
    # video_24fps_mode: 同期無効
    # video_24fps_mode_cellular: 同期無効
    caption_font: str = 'Rounded M+ 1m for ARIB'
    always_border_caption_text: bool = True
    specify_caption_opacity: bool = False
    caption_opacity: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)] = 1.0
    tv_show_superimpose: bool = True
    video_show_superimpose: bool = False
    # tv_show_data_broadcasting: 同期無効
    # enable_internet_access_from_data_broadcasting: 同期無効
    capture_save_mode: Literal['Browser', 'UploadServer', 'Both'] = 'UploadServer'
    capture_caption_mode: Literal['VideoOnly', 'CompositingCaption', 'Both'] = 'Both'
    capture_filename_pattern: str = 'Capture_%date%-%time%'
    # capture_copy_to_clipboard: 同期無効
    # sync_settings: 同期無効
    jikkyo_enabled: bool = False
    prefer_posting_to_nicolive: bool = True
    comment_speed_rate: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 1.0
    comment_font_size: PositiveInt = 34
    close_comment_form_after_sending: bool = True
    mute_vulgar_comments: bool = True
    mute_abusive_discriminatory_prejudiced_comments: bool = True
    mute_big_size_comments: bool = True
    mute_fixed_comments: bool = False
    mute_colored_comments: bool = False
    mute_consecutive_same_characters_comments: bool = False
    mute_comment_keywords_normalize_alphanumeric_width_case: bool = True
    muted_comment_keywords: list[dict[str, str]] = []
    muted_niconico_user_ids: list[str] = []
    fold_panel_after_sending_tweet: bool = False
    reset_hashtag_when_program_switches: bool = True
    auto_add_watching_channel_hashtag: bool = True
    twitter_reply_thread_mode: Literal['PerHashtag', 'PerDay', 'Disabled'] = 'PerHashtag'
    bluesky_reply_thread_mode: Literal['PerHashtag', 'PerDay', 'Disabled'] = 'Disabled'
    twitter_active_tab: Literal['Search', 'Timeline', 'Capture'] = 'Capture'
    tweet_hashtag_position: Literal['Prepend', 'Append', 'PrependWithLineBreak', 'AppendWithLineBreak'] = 'Append'
    tweet_capture_watermark_position: Literal['None', 'TopLeft', 'TopRight', 'BottomLeft', 'BottomRight'] = 'None'

    @staticmethod
    def _rejectNonFiniteNumbers(value: Any, path: str = 'root') -> None:
        """list/dict を再帰走査し、非有限 float (NaN/Inf) を拒否する。"""

        if isinstance(value, float):
            if math.isfinite(value) is False:
                raise ValueError(f'Non-finite number is not allowed at {path}')
            return
        if isinstance(value, dict):
            for key, child in value.items():
                ClientSettings._rejectNonFiniteNumbers(child, f'{path}.{key}')
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                ClientSettings._rejectNonFiniteNumbers(child, f'{path}[{index}]')

    @model_validator(mode='before')
    @classmethod
    def rejectNestedNonFiniteNumbers(cls, data: Any) -> Any:
        """
        mylist / watched_history など dict[str, Any] 配下の NaN/Inf も 422 にする。

        Field(allow_inf_nan=False) は最上位 float のみ対象のため、任意 dict 内は別途検査する。
        """

        if isinstance(data, dict):
            cls._rejectNonFiniteNumbers(data)
        return data


# サーバー設定を表す Pydantic モデル
# config.yaml のバリデーションは設定データをこの Pydantic モデルに通すことで行う

class _ServerSettingsGeneral(BaseModel):
    backend: Literal['EDCB', 'Mirakurun'] = 'EDCB'
    jikkyo_enabled: bool = False
    always_receive_tv_from_mirakurun: bool = False
    edcb_url: Annotated[Url, UrlConstraints(allowed_schemes=['tcp'])] = Url('tcp://127.0.0.1:4510/')
    mirakurun_url: Annotated[Url, UrlConstraints(allowed_schemes=['http', 'https'])] = Url('http://127.0.0.1:40772/')
    encoder: Literal['FFmpeg', 'QSV', 'NVENC', 'AMF'] = 'FFmpeg'
    encoder_bs4k: Literal['FFmpeg', 'QSV', 'NVENC', 'AMF'] = 'FFmpeg'
    encoder_bs4k_input_probesize: PositiveInt = 3000
    encoder_bs4k_input_analyze: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 1.5
    encoder_bs4k_input_analysis_enabled: bool = True
    encoder_bs4k_max_interleave_delta: PositiveInt = 800
    encoder_bs4k_low_latency: bool = False
    konomitv_bs4k_acceptance_diagnostics_enabled: bool = False
    bs4k_ignore_viewer_low_latency: bool = True
    bs4k_live_startup_discard_enabled: bool = True
    bs4k_live_startup_discard_seconds: Annotated[float, Field(ge=0.0, allow_inf_nan=False)] = 2.0
    program_update_interval: Annotated[float, Field(ge=0.1, allow_inf_nan=False)] = 5.0
    debug: bool = False
    debug_encoder: bool = False

    @property
    def live_stream_backend(self) -> Literal['EDCB', 'Mirakurun']:
        """
        ライブ放送波を実際に受信するバックエンドを返す。

        Args:
            なし。

        Returns:
            Literal['EDCB', 'Mirakurun']: ライブ放送波を受信するバックエンド。
        """

        # メタデータバックエンドが Mirakurun の場合は、always_receive_tv_from_mirakurun の値にかかわらず
        # ライブ放送波も Mirakurun から受信する。EDCB の場合だけ設定値に応じて受信元を切り替える。
        if self.backend == 'EDCB' and self.always_receive_tv_from_mirakurun is False:
            return 'EDCB'
        return 'Mirakurun'

    @field_validator('edcb_url')
    def validate_edcb_url(cls, edcb_url: Url, info: ValidationInfo) -> Url:
        # URL を末尾のスラッシュありに統一
        edcb_url = Url(str(edcb_url).rstrip('/') + '/')
        # バリデーションをスキップする場合はここで終了
        if type(info.context) is dict and info.context.get('bypass_validation') is True:
            return edcb_url
        # EDCB バックエンドの接続確認
        if info.data.get('backend') == 'EDCB':
            # 循環参照を避けるために遅延インポート
            from app.utils.edcb.EDCBUtil import EDCBUtil
            # edcb_url を明示的に指定
            ## edcb_url を省略すると内部で再帰的に LoadConfig() が呼ばれてしまい RecursionError が発生する
            edcb_host = EDCBUtil.getEDCBHost(edcb_url)
            edcb_port = EDCBUtil.getEDCBPort(edcb_url)
            # ホスト名またはポートが指定されていない
            if ((edcb_host is None) or (edcb_port is None and edcb_host != 'edcb-namedpipe')):
                raise ValueError(
                    'URL 内にホスト名またはポートが指定されていません。\n'
                    'EDCB の URL を間違えている可能性があります。'
                )
            # 現在の EpgTimerSrv の動作ステータスを取得できるか試してみる
            ## RecursionError 回避のために edcb_url を明示的に指定
            ## ThreadPoolExecutor 上で実行し、自動リロードモード時に発生するイベントループ周りの謎エラーを回避する
            with concurrent.futures.ThreadPoolExecutor(1) as executor:
                result = executor.submit(asyncio.run, EDCBUtil.getEDCBStatus(edcb_url)).result()
            if result == 'Unknown':
                raise ValueError(
                    f'EDCB ({edcb_url}) にアクセスできませんでした。\n'
                    'EDCB が起動していないか、URL を間違えている可能性があります。'
                )
            from app import logging
            logging.info(f'Backend: EDCB ({edcb_url}) Status: {result}')
        return edcb_url

    @field_validator('mirakurun_url')
    def validate_mirakurun_url(cls, mirakurun_url: Url, info: ValidationInfo) -> Url:
        # URL を末尾のスラッシュありに統一
        ## HTTP/HTTPS URL では Pydantic の Url インスタンスが自動的に末尾のスラッシュを付けてしまうようだが、
        ## 挙動が変わらないとも限らないので、一応明示的に付けておく
        mirakurun_url = Url(str(mirakurun_url).rstrip('/') + '/')
        # バリデーションをスキップする場合はここで終了
        if type(info.context) is dict and info.context.get('bypass_validation') is True:
            return mirakurun_url
        # Mirakurun バックエンドの接続確認
        if info.data.get('backend') == 'Mirakurun' or info.data.get('always_receive_tv_from_mirakurun') is True:
            # 試しにリクエストを送り、200 (OK) が返ってきたときだけ有効な URL とみなす
            try:
                response = httpx.get(
                    # Mirakurun API は http://127.0.0.1:40772//api/tuners のような二重スラッシュを許容しないので、
                    # mirakurun_url の末尾のスラッシュを削除してから endpoint を追加する必要がある
                    ## 従来は /api/version にアクセスしていたが、Mirakurun 4.0.0-beta.5 以下のバージョンには
                    ## API 実行時に録画中のストリームがドロップする重大なバグがあるため、他のエンドポイントを使うようにした
                    ## ref: https://github.com/Chinachu/Mirakurun/commit/27fccf9cd9dd08e56614dabf2ceb1b27a6096f0e
                    url = str(mirakurun_url).rstrip('/') + '/api/tuners',
                    headers = API_REQUEST_HEADERS,
                    timeout = 20,  # 久々のアクセスだとなぜか時間がかかることがあるため、ここだけタイムアウトを長めに設定
                )
                # レスポンスヘッダーの Server から Mirakurun か mirakc かを判定
                server_header = response.headers.get('server', '').lower()
                if 'mirakc' in server_header:
                    mirakurun_or_mirakc = 'mirakc'
                    # Server ヘッダーからバージョン情報を抽出 (例: mirakc/3.4.4)
                    version_info = server_header.split('/')[-1] if '/' in server_header else 'unknown'
                else:
                    mirakurun_or_mirakc = 'Mirakurun'
                    # Server ヘッダーからバージョン情報を抽出 (例: Mirakurun/3.9.0-rc.4)
                    version_info = server_header.split('/')[-1] if '/' in server_header else 'unknown'
            except (httpx.NetworkError, httpx.TimeoutException):
                raise ValueError(
                    f'Mirakurun / mirakc ({mirakurun_url}) にアクセスできませんでした。\n'
                    'Mirakurun / mirakc が起動していないか、URL を間違えている可能性があります。'
                )
            try:
                response_json = response.json()
                if response.status_code != 200 or not isinstance(response_json, list) or version_info == 'unknown':
                    raise ValueError()
            except Exception:
                raise ValueError(
                    f'{mirakurun_url} は {mirakurun_or_mirakc} の URL ではありません。\n'
                    f'{mirakurun_or_mirakc} の URL を間違えている可能性があります。'
                )
            from app import logging
            logging.info(f'Backend: {mirakurun_or_mirakc} {version_info} ({mirakurun_url})')
            if info.data.get('always_receive_tv_from_mirakurun') is True:
                logging.info(f'Always receive TV from {mirakurun_or_mirakc}.')
        return mirakurun_url

    @classmethod
    def _validate_encoder_value(cls, encoder: str) -> str:
        from app import logging
        from app.streams.RecordedPlaybackCapabilities import (
            RecordedPlaybackBackend,
            RecordedPlaybackEncoder,
        )

        # 正規化済みの公開設定名から、ライブ再生と同じ FFmpeg 8 定義を使う
        encoder_type = cast(RecordedPlaybackEncoder, encoder)
        encoder_executable = RecordedPlaybackBackend.getExecutable(encoder_type)

        # HW エンコーダーは FFmpeg 8 で短い実エンコードを行い、H.264 と H.265 の利用可否を検査する
        ## EncC の --check-hw は実際のライブ経路と異なるため、移行後の起動可否を正しく表せない
        if encoder != 'FFmpeg':
            device: str | None = None
            if encoder_type in ('QSV', 'AMF'):
                devices = RecordedPlaybackBackend.discoverRenderDevices(encoder_type)
                if len(devices) > 0:
                    device = devices[0]
            probe_results: dict[Literal['avc', 'hevc'], bool] = {}
            with tempfile.TemporaryDirectory(prefix='konomitv-bs4k-live-encoder-probe-') as temporary_directory:
                for codec in cast(tuple[Literal['avc', 'hevc'], ...], ('avc', 'hevc')):
                    output_path = Path(temporary_directory) / f'{codec}.mp4'
                    command = RecordedPlaybackBackend.buildProbeCommand(
                        encoder_type,
                        codec,
                        8,
                        output_path,
                        device,
                    )
                    try:
                        result = subprocess.run(
                            command,
                            capture_output = True,
                            timeout = 20,
                            check = False,
                            env = RecordedPlaybackBackend.getEnvironment(encoder_type),
                        )
                    except (OSError, subprocess.SubprocessError) as ex:
                        raise ValueError(f'{encoder} の FFmpeg 8 ハードウェア能力検査を実行できませんでした。') from ex
                    probe_results[codec] = result.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 0

            # H.264 は全ライブ品質の最低要件なので、利用できない環境では起動を拒否する
            if probe_results['avc'] is False:
                raise ValueError(
                    f'お使いの環境では {encoder} がサポートされていないため、KonomiTV-BS4K を起動できません。\n'
                    f'別のエンコーダーを選択するか、FFmpeg 8 の {encoder} 向け動作環境を整備してください。'
                )

            # H.265/HEVC に対応していない環境では、HEVC を選択できない旨を出力する
            if probe_results['hevc'] is False:
                logging.warning(f'お使いの環境では {encoder} での H.265/HEVC エンコードがサポートされていないため、映像コーデックに HEVC は利用できません。')

        # エンコーダーのバージョン情報を取得する
        ## バージョン情報は出力の1行目にある
        try:
            result = subprocess.run(
                [encoder_executable, '-version'],
                capture_output = True,
                timeout = 10,
                check = False,
                env = RecordedPlaybackBackend.getEnvironment(encoder_type),
            )
        except (OSError, subprocess.SubprocessError) as ex:
            raise ValueError(f'{encoder} のバージョン情報を取得できませんでした。') from ex
        if result.returncode != 0 or result.stdout.strip() == b'':
            raise ValueError(f'{encoder} のバージョン情報を取得できませんでした。')
        encoder_version = result.stdout.decode('utf-8', errors='replace').split('\n')[0]
        ## Copyright 以降の文字列を削除し、公開設定名と実際の FFmpeg 8 バックエンドを併記する
        encoder_version = re.sub(r' Copyright.*$', '', encoder_version)
        encoder_version = encoder_version.replace('ffmpeg', 'FFmpeg').strip()
        logging.info(f'Encoder: {encoder} via {encoder_version}')
        return encoder

    @field_validator('encoder', 'encoder_bs4k', mode='before')
    def validate_encoder(cls, encoder: Any, info: ValidationInfo) -> str:
        # FFmpeg 8 バックエンドへ移行する前の設定値を、現在の正規識別子へ一方向に変換する。
        ## この互換処理は config.yaml の読み込み境界だけに限定し、API と実行時の型には旧名を残さない。
        if isinstance(encoder, str):
            encoder = {
                'QSVEncC': 'QSV',
                'NVEncC': 'NVENC',
                'VCEEncC': 'AMF',
            }.get(encoder, encoder)
        else:
            raise ValueError('エンコーダー名は文字列で指定してください。')

        # バリデーションをスキップする場合はここで終了
        if type(info.context) is dict and info.context.get('bypass_validation') is True:
            return encoder
        return cls._validate_encoder_value(encoder)

class _ServerSettingsServer(BaseModel):
    https_mode: Literal['akebi', 'certificate', 'reverse_proxy'] = 'akebi'
    port: PositiveInt = 7000
    opencode_serve_port: Annotated[int, Field(ge=1024, le=65535)] = 4097
    custom_https_certificate: FilePath | None = None
    custom_https_private_key: FilePath | None = None
    reverse_proxy_listen_address: IPvAnyAddress = ipaddress.IPv4Address('0.0.0.0')
    trusted_proxy_cidrs: list[IPvAnyNetwork] = []

    @model_validator(mode='after')
    def validate_https_mode(self, info: ValidationInfo) -> '_ServerSettingsServer':
        # 自動リロード先プロセスでは、起動元プロセスで検証済みの設定をそのまま復元する
        if type(info.context) is dict and info.context.get('bypass_validation') is True:
            return self

        certificate_is_set = self.custom_https_certificate is not None
        private_key_is_set = self.custom_https_private_key is not None

        if self.https_mode == 'akebi':
            if certificate_is_set or private_key_is_set:
                raise ValueError(
                    'https_mode が akebi ではカスタム HTTPS 証明書を利用できません。\n'
                    'カスタム HTTPS 証明書を利用する場合は https_mode を certificate に変更してください。'
                )
            if self.trusted_proxy_cidrs:
                raise ValueError('trusted_proxy_cidrs は https_mode が reverse_proxy の場合のみ指定できます。')
            if str(self.reverse_proxy_listen_address) != '0.0.0.0':
                raise ValueError('reverse_proxy_listen_address は https_mode が reverse_proxy の場合のみ変更できます。')

        elif self.https_mode == 'certificate':
            if certificate_is_set is False or private_key_is_set is False:
                raise ValueError(
                    'https_mode が certificate の場合、custom_https_certificate と '
                    'custom_https_private_key の両方を指定してください。'
                )
            if self.trusted_proxy_cidrs:
                raise ValueError('trusted_proxy_cidrs は https_mode が reverse_proxy の場合のみ指定できます。')
            if str(self.reverse_proxy_listen_address) != '0.0.0.0':
                raise ValueError('reverse_proxy_listen_address は https_mode が reverse_proxy の場合のみ変更できます。')

        elif self.https_mode == 'reverse_proxy':
            if certificate_is_set or private_key_is_set:
                raise ValueError(
                    'https_mode が reverse_proxy の場合、custom_https_certificate と '
                    'custom_https_private_key は指定できません。'
                )
            if not self.trusted_proxy_cidrs:
                raise ValueError('https_mode が reverse_proxy の場合、trusted_proxy_cidrs を1件以上指定してください。')

        return self

    @field_validator('port')
    def validate_port(cls, port: int, info: ValidationInfo) -> int:
        # バリデーションをスキップする場合はここで終了
        if type(info.context) is dict and info.context.get('bypass_validation') is True:
            return port
        # リッスンするポート番号が 1024 ~ 65525 の間に収まっているかをチェック
        if port < 1024 or port > 65525:
            raise ValueError(
                'ポート番号の設定が不正なため、KonomiTV-BS4K を起動できません。\n'
                '設定したポート番号が 1024 ~ 65525 (65535 ではない) の間に収まっているかを確認してください。'
            )
        # 使用中のポートを取得
        used_ports = _GetUsedListenPorts()
        # リッスンポートと同じポートが使われていたら、エラーを表示する
        # Akebi HTTPS Server のリッスンポートと Uvicorn のリッスンポートの両方をチェック
        if port in used_ports:
            raise ValueError(
                f'ポート {port} は他のプロセスで使われているため、KonomiTV-BS4K を起動できません。\n'
                f'重複して KonomiTV-BS4K を起動していないか、他のソフトでポート {port} を使っていないかを確認してください。'
            )
        if info.data.get('https_mode') == 'akebi' and (port + 10) in used_ports:
            raise ValueError(
                f'ポート {port + 10} ({port} + 10) は他のプロセスで使われているため、KonomiTV-BS4K を起動できません。\n'
                f'重複して KonomiTV-BS4K を起動していないか、他のソフトでポート {port + 10} を使っていないかを確認してください。'
            )
        return port

    @field_validator('opencode_serve_port')
    def validateOpenCodeServePort(cls, port: int, info: ValidationInfo) -> int:
        """製品用 OpenCode serve のポートが他プロセスと衝突しないことを検証する。

        Args:
            port (int): 検証する OpenCode serve のポート番号。
            info (ValidationInfo): bypass_validation などの検証コンテキスト。

        Returns:
            int: 利用可能な OpenCode serve のポート番号。
        """

        # 自動リロード先プロセスでは、起動元プロセスで検証済みの設定をそのまま復元する。
        if type(info.context) is dict and info.context.get('bypass_validation') is True:
            return port

        # 同じ KonomiTV-BS4K 内のリスナとの衝突は、全セクションが揃う ServerSettings 側で検証する。
        if port in _GetUsedListenPorts():
            raise ValueError(
                f'OpenCode serve のポート {port} は他のプロセスで使われているため、'
                'KonomiTV-BS4K を起動できません。\n'
                f'他のソフトでポート {port} を使っていないかを確認してください。'
            )
        return port

class _ServerSettingsCompatibilityAPI(BaseModel):
    enabled: bool = False
    https_mode: Literal['inherit', 'akebi', 'certificate', 'reverse_proxy'] = 'inherit'
    port: PositiveInt = 7200
    profile: Literal['KomorebiV1'] = 'KomorebiV1'
    custom_https_certificate: FilePath | None = None
    custom_https_private_key: FilePath | None = None
    reverse_proxy_listen_address: IPvAnyAddress = ipaddress.IPv4Address('0.0.0.0')
    trusted_proxy_cidrs: list[IPvAnyNetwork] = []

    @model_validator(mode='after')
    def validate_https_mode(self, info: ValidationInfo) -> '_ServerSettingsCompatibilityAPI':
        """互換 API 専用の HTTPS モードと関連設定が矛盾しないことを検証する。"""

        # 自動リロード先プロセスでは、起動元プロセスで検証済みの設定をそのまま復元する
        if type(info.context) is dict and info.context.get('bypass_validation') is True:
            return self

        certificate_is_set = self.custom_https_certificate is not None
        private_key_is_set = self.custom_https_private_key is not None

        if self.https_mode in ('inherit', 'akebi'):
            if certificate_is_set or private_key_is_set:
                raise ValueError(
                    '互換 API の https_mode が inherit または akebi の場合、専用の HTTPS 証明書は指定できません。'
                )
            if self.trusted_proxy_cidrs:
                raise ValueError(
                    '互換 API の trusted_proxy_cidrs は https_mode が reverse_proxy の場合のみ指定できます。'
                )
            if str(self.reverse_proxy_listen_address) != '0.0.0.0':
                raise ValueError(
                    '互換 API の reverse_proxy_listen_address は '
                    'https_mode が reverse_proxy の場合のみ変更できます。'
                )

        elif self.https_mode == 'certificate':
            if certificate_is_set is False or private_key_is_set is False:
                raise ValueError(
                    '互換 API の https_mode が certificate の場合、custom_https_certificate と '
                    'custom_https_private_key の両方を指定してください。'
                )
            if self.trusted_proxy_cidrs:
                raise ValueError(
                    '互換 API の trusted_proxy_cidrs は https_mode が reverse_proxy の場合のみ指定できます。'
                )
            if str(self.reverse_proxy_listen_address) != '0.0.0.0':
                raise ValueError(
                    '互換 API の reverse_proxy_listen_address は '
                    'https_mode が reverse_proxy の場合のみ変更できます。'
                )

        elif self.https_mode == 'reverse_proxy':
            if certificate_is_set or private_key_is_set:
                raise ValueError(
                    '互換 API の https_mode が reverse_proxy の場合、custom_https_certificate と '
                    'custom_https_private_key は指定できません。'
                )
            if not self.trusted_proxy_cidrs:
                raise ValueError(
                    '互換 API の https_mode が reverse_proxy の場合、trusted_proxy_cidrs を1件以上指定してください。'
                )

        return self

    @field_validator('port')
    def validate_port(cls, port: int, info: ValidationInfo) -> int:
        # 自動リロード先プロセスでは、起動元プロセスで検証済みの設定をそのまま復元する
        if type(info.context) is dict and info.context.get('bypass_validation') is True:
            return port
        if port < 1024 or port > 65525:
            raise ValueError(
                '互換 API のポート番号が不正なため、KonomiTV-BS4K を起動できません。\n'
                '設定したポート番号が 1024 ~ 65525 の間に収まっているかを確認してください。'
            )
        return port

class _ServerSettingsTV(BaseModel):
    preferred_terrestrial_region: TerrestrialRegion | None = None
    max_alive_time: PositiveInt = 10
    debug_mode_ts_path: FilePath | None = None

class _ServerSettingsVideo(BaseModel):
    recorded_folders: list[DirectoryPath] = []
    exclude_scan_paths: list[str] = []
    recorded_fmp4_cache_folder: Path | None = None
    recorded_playback_index_backfill_enabled: bool = True

    @field_validator('recorded_fmp4_cache_folder')
    def validate_recorded_fmp4_cache_folder(cls, folder: Path | None) -> Path | None:
        """録画fMP4キャッシュ保存先を作成し、書き込み可能であることを検証する。"""

        if folder is None:
            return None
        if folder.is_absolute() is False:
            raise ValueError('録画 fMP4 キャッシュの保存先には絶対パスを指定してください。')
        try:
            folder.mkdir(parents=True, exist_ok=True)
            write_test_path = folder / '.konomitv-bs4k-fmp4-write-test'
            write_test_path.write_bytes(b'')
            write_test_path.unlink()
        except OSError as ex:
            raise ValueError('録画 fMP4 キャッシュの保存先へ書き込めません。') from ex
        return folder

class _ServerSettingsCapture(BaseModel):
    upload_folders: list[DirectoryPath] = []


class _ServerSettingsCMAnalysis(BaseModel):
    """サーバー全体で共有するCM解析の運用設定（config.yaml）。"""

    enabled: bool = False
    # 起動時に DirectoryPath で存在検証しない。未設定時は data/cm-analysis/logos を使う。
    logo_directory: Path | None = None
    excluded_directories: list[str] = []


class ServerSettings(BaseModel):
    general: _ServerSettingsGeneral = _ServerSettingsGeneral()
    server: _ServerSettingsServer = _ServerSettingsServer()
    compatibility_api: _ServerSettingsCompatibilityAPI = _ServerSettingsCompatibilityAPI()
    tv: _ServerSettingsTV = _ServerSettingsTV()
    video: _ServerSettingsVideo = _ServerSettingsVideo()
    capture: _ServerSettingsCapture = _ServerSettingsCapture()
    cm_analysis: _ServerSettingsCMAnalysis = _ServerSettingsCMAnalysis()

    @model_validator(mode='after')
    def validateListenPorts(self, info: ValidationInfo) -> 'ServerSettings':
        """通常 API・互換 API・OpenCode serve のリスナが互いに衝突しないことを検証する。

        Args:
            info (ValidationInfo): bypass_validation などの検証コンテキスト。

        Returns:
            ServerSettings: ポートが互いに重複していないサーバー設定。
        """

        if type(info.context) is dict and info.context.get('bypass_validation') is True:
            return self

        listen_ports = {
            '通常 API': self.server.port,
            'OpenCode serve': self.server.opencode_serve_port,
        }
        if self.server.https_mode == 'akebi':
            listen_ports['通常 API の内部 Uvicorn'] = self.server.port + 10

        # 無効な互換 API はポートを予約せず、OpenCode serve に同じ値を設定できるようにする。
        compatibility_https_mode: str | None = None
        if self.compatibility_api.enabled:
            compatibility_https_mode = (
                self.server.https_mode
                if self.compatibility_api.https_mode == 'inherit'
                else self.compatibility_api.https_mode
            )
            listen_ports['互換 API'] = self.compatibility_api.port
            if compatibility_https_mode == 'akebi':
                listen_ports['互換 API の内部 Uvicorn'] = self.compatibility_api.port + 10

        port_owners: dict[int, str] = {}
        for owner, port in listen_ports.items():
            if port in port_owners:
                raise ValueError(
                    f'{owner} のポート {port} が {port_owners[port]} と重複しています。\n'
                    'KonomiTV-BS4K が使用する各ポートを重複しない値に変更してください。'
                )
            port_owners[port] = owner

        if self.compatibility_api.enabled is False:
            return self

        assert compatibility_https_mode is not None
        used_ports = _GetUsedListenPorts()
        compatibility_ports = {'互換 API': self.compatibility_api.port}
        if compatibility_https_mode == 'akebi':
            compatibility_ports['互換 API の内部 Uvicorn'] = self.compatibility_api.port + 10
        for owner, port in compatibility_ports.items():
            if port in used_ports:
                raise ValueError(
                    f'{owner} のポート {port} は他のプロセスで使われているため、KonomiTV-BS4K を起動できません。\n'
                    f'他のソフトでポート {port} を使っていないかを確認してください。'
                )

        return self


class _HostServerSettingsServer(_ServerSettingsServer):
    """API・設定ファイルでホストパスを保持する通常サーバー設定。"""

    custom_https_certificate: Path | None = None
    custom_https_private_key: Path | None = None


class _HostServerSettingsCompatibilityAPI(_ServerSettingsCompatibilityAPI):
    """API・設定ファイルでホストパスを保持する互換API設定。"""

    custom_https_certificate: Path | None = None
    custom_https_private_key: Path | None = None


class _HostServerSettingsTV(_ServerSettingsTV):
    """API・設定ファイルでホストパスを保持するライブ視聴設定。"""

    debug_mode_ts_path: Path | None = None


class _HostServerSettingsVideo(_ServerSettingsVideo):
    """API・設定ファイルでホストパスを保持する録画設定。"""

    recorded_folders: list[Path] = []
    exclude_scan_paths: list[str] = []
    recorded_fmp4_cache_folder: Path | None = None

    @field_validator('exclude_scan_paths')
    def normalize_exclude_scan_paths(cls, paths: list[str]) -> list[str]:
        """
        UIの未入力行を除き、従来のLoadConfigと同じく前後の空白を正規化する。

        Args:
            paths (list[str]): WebUI・API・config.yamlの録画スキャン除外パス。

        Returns:
            list[str]: 空行を除去した録画スキャン除外パス。
        """

        return [path.strip() for path in paths if path.strip() != '']

    @field_validator('recorded_fmp4_cache_folder')
    def validate_recorded_fmp4_cache_folder(cls, folder: Path | None) -> Path | None:
        """
        外部モデルでは実アクセス検証を内部モデルへの変換後まで延期する。

        Args:
            folder (Path | None): WebUI・API・config.yamlのホストパス。

        Returns:
            Path | None: 未変更の外部ホストパス。
        """

        return folder


class _HostServerSettingsCapture(_ServerSettingsCapture):
    """API・設定ファイルでホストパスを保持するキャプチャ設定。"""

    upload_folders: list[Path] = []


class _HostServerSettingsCMAnalysis(_ServerSettingsCMAnalysis):
    """API・設定ファイルでホストパスを保持するCM解析設定。"""

    logo_directory: Path | None = None
    excluded_directories: list[str] = []

    @field_validator('excluded_directories')
    def normalize_excluded_directories(cls, paths: list[str]) -> list[str]:
        """
        UIの未入力行を除き、前後の空白を正規化する。

        Args:
            paths (list[str]): WebUI・API・config.yamlのCM解析除外パス。

        Returns:
            list[str]: 空行を除去した除外パス。
        """

        return [path.strip() for path in paths if path.strip() != '']


_SERVER_SETTINGS_PATH_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ('server', 'custom_https_certificate', False),
    ('server', 'custom_https_private_key', False),
    ('compatibility_api', 'custom_https_certificate', False),
    ('compatibility_api', 'custom_https_private_key', False),
    ('tv', 'debug_mode_ts_path', False),
    ('video', 'recorded_folders', True),
    ('video', 'exclude_scan_paths', True),
    ('video', 'recorded_fmp4_cache_folder', False),
    ('capture', 'upload_folders', True),
    ('cm_analysis', 'logo_directory', False),
    ('cm_analysis', 'excluded_directories', True),
)


def _ConvertServerSettingsPaths(
    config_dict: dict[str, Any],
    converter: Callable[[str | Path], Path],
) -> dict[str, Any]:
    """
    対象パス項目を定義表に従って同じ変換境界へ通す。

    Args:
        config_dict (dict[str, Any]): 変換対象のサーバー設定辞書。
        converter (Callable[[str | Path], Path]): 各パスへ適用する変換関数。

    Returns:
        dict[str, Any]: 入力辞書の対象パスを変換した辞書。
    """

    for section, field_name, is_list in _SERVER_SETTINGS_PATH_FIELDS:
        field_value = config_dict[section][field_name]
        if field_value is None:
            continue
        if is_list:
            config_dict[section][field_name] = [
                str(converter(path_value))
                for path_value in field_value
            ]
        else:
            config_dict[section][field_name] = str(converter(field_value))
    return config_dict


class HostServerSettings(BaseModel):
    """WebUI・API・config.yamlでホスト絶対パスを保持するサーバー設定。"""

    general: _ServerSettingsGeneral = _ServerSettingsGeneral()
    server: _HostServerSettingsServer = _HostServerSettingsServer()
    compatibility_api: _HostServerSettingsCompatibilityAPI = _HostServerSettingsCompatibilityAPI()
    tv: _HostServerSettingsTV = _HostServerSettingsTV()
    video: _HostServerSettingsVideo = _HostServerSettingsVideo()
    capture: _HostServerSettingsCapture = _HostServerSettingsCapture()
    cm_analysis: _HostServerSettingsCMAnalysis = _HostServerSettingsCMAnalysis()

    @model_validator(mode='after')
    def normalize_host_paths(self) -> 'HostServerSettings':
        """
        外部モデルを構築した時点で対象パス項目をホスト表現へ正規化する。

        旧クライアントから受け取ったDocker内部接頭辞を、保存や内部変換まで
        モデル内に保持しない。各セクションは再検証してPathなどの宣言型を維持する。

        Returns:
            HostServerSettings: 対象パスを正規化した外部設定。
        """

        normalized_config = _ConvertServerSettingsPaths(
            self.model_dump(mode='json'),
            NormalizeHostPath,
        )
        bypass_context = {'bypass_validation': True}
        self.server = _HostServerSettingsServer.model_validate(
            normalized_config['server'],
            context=bypass_context,
        )
        self.compatibility_api = _HostServerSettingsCompatibilityAPI.model_validate(
            normalized_config['compatibility_api'],
            context=bypass_context,
        )
        self.tv = _HostServerSettingsTV.model_validate(
            normalized_config['tv'],
            context=bypass_context,
        )
        self.video = _HostServerSettingsVideo.model_validate(
            normalized_config['video'],
            context=bypass_context,
        )
        self.capture = _HostServerSettingsCapture.model_validate(
            normalized_config['capture'],
            context=bypass_context,
        )
        self.cm_analysis = _HostServerSettingsCMAnalysis.model_validate(
            normalized_config['cm_analysis'],
            context=bypass_context,
        )
        return self

    @classmethod
    def fromServerSettings(cls, settings: ServerSettings) -> 'HostServerSettings':
        """
        内部実行用設定から外部向けホストパス設定を生成する。

        Args:
            settings (ServerSettings): 実アクセス用パスを保持する内部設定。

        Returns:
            HostServerSettings: 全対象パスをホスト表現へ変換した設定。
        """

        config_dict = settings.model_dump(mode='json')
        return cls.model_validate(
            _ConvertServerSettingsPaths(config_dict, ToHostPath),
            context={'bypass_validation': True},
        )

    def toServerSettings(self, bypass_validation: bool = False) -> ServerSettings:
        """
        ホストパス設定を内部実行用設定へ変換して検証する。

        Args:
            bypass_validation (bool): カスタム実行環境検証を省略するかどうか。

        Returns:
            ServerSettings: Dockerでは対象パスへ接頭辞を1回だけ付けた内部設定。

        Raises:
            HostPathError: 外部パスが安全なホスト絶対パスでない場合。
            ValidationError: 実アクセス先が存在しないなど内部設定の検証に失敗した場合。
        """

        config_dict = self.model_dump(mode='json')
        return ServerSettings.model_validate(
            _ConvertServerSettingsPaths(config_dict, ToRuntimePath),
            context={'bypass_validation': bypass_validation},
        )

    def toSaveConfigDict(self) -> dict[str, Any]:
        """
        設定ファイルへ保存できる正規化済み辞書を返す。

        Returns:
            dict[str, Any]: 旧接頭辞を除き、ホストパスだけを保持する設定辞書。

        Raises:
            HostPathError: 外部パスが安全なホスト絶対パスでない場合。
        """

        return _ConvertServerSettingsPaths(self.model_dump(mode='json'), ToHostPath)


def ResolveCompatibilityHTTPSSettings(
    settings: ServerSettings,
) -> _ServerSettingsServer | _ServerSettingsCompatibilityAPI:
    """互換 API が実際に使用する HTTPS 設定を返す。"""

    if settings.compatibility_api.https_mode == 'inherit':
        return settings.server
    return settings.compatibility_api


# サーバー設定データと読み込み・保存用の関数
# _CONFIG には config.yaml から読み込んだ KonomiTV サーバーの設定データが保持される
# _CONFIG には直接アクセスせず、Config() 関数を通してアクセスする

_CONFIG: ServerSettings | None = None
_CONFIG_YAML_PATH = BASE_DIR.parent / 'config.yaml'


def LoadConfig(bypass_validation: bool = False) -> ServerSettings:
    """
    config.yaml からサーバー設定データのロードとバリデーションを行い、グローバル変数に格納する
    基本 KonomiTV サーバー起動時に一度だけ呼び出されるが、自動リロードモードやマルチプロセス実行時にも呼び出される
    いずれの場合でも、1プロセス内で複数回呼び出されることはない

    Args:
        bypass_validation (bool): バリデーションをスキップするかどうか

    Raises:
        SystemExit: 設定ファイルが配置されていなかったり、設定内容が不正な場合はサーバーを起動できないため、プロセスを終了する

    Returns:
        ServerSettings: 読み込んだサーバー設定データ
    """

    def MergeConfigWithDefaults(config_dict: dict[str, Any]) -> dict[str, Any]:
        """
        config.yaml の読み込み結果をデフォルト設定とマージする

        Args:
            config_dict (dict[str, Any]): config.yaml から読み込んだ設定データ

        Returns:
            dict[str, Any]: デフォルト設定とマージ済みの設定データ
        """

        def merge_dicts(base_dict: dict[str, Any], override_dict: dict[str, Any]) -> dict[str, Any]:
            merged_dict = dict(base_dict)
            for key, value in override_dict.items():
                if (
                    key in merged_dict
                    and isinstance(merged_dict[key], dict)
                    and isinstance(value, dict)
                ):
                    merged_dict[key] = merge_dicts(merged_dict[key], value)
                else:
                    merged_dict[key] = value
            return merged_dict

        default_config_dict = HostServerSettings().model_dump(mode='json')
        return merge_dicts(default_config_dict, config_dict)

    global _CONFIG, _CONFIG_YAML_PATH
    assert _CONFIG is None, 'LoadConfig() has already been called.'

    # 循環参照を避けるために遅延インポート
    from app import logging

    # 設定ファイルが配置されていない場合、エラーを表示して終了する
    if Path.exists(_CONFIG_YAML_PATH) is False:
        logging.error('設定ファイルが配置されていないため、KonomiTV-BS4K を起動できません。')
        logging.error('config.example.yaml を config.yaml にコピーし、お使いの環境に合わせて編集してください。')
        sys.exit(1)

    # 設定ファイルからサーバー設定をロードする
    try:
        with open(_CONFIG_YAML_PATH, encoding='utf-8') as file:
            config_raw = ruamel.yaml.YAML().load(file)
            if config_raw is None:
                logging.error('設定ファイルが空のため、KonomiTV-BS4K を起動できません。')
                logging.error('config.example.yaml を config.yaml にコピーし、お使いの環境に合わせて編集してください。')
                sys.exit(1)
        config_dict: dict[str, dict[str, Any]] = dict(config_raw)
    except Exception as error:
        logging.error('設定ファイルのロード中にエラーが発生したため、KonomiTV-BS4K を起動できません。')
        logging.error(f'{type(error).__name__}: {error}')
        sys.exit(1)

    # config.yamlに存在しない設定値は、ホスト表現のデフォルト値で補完する。
    config_dict = MergeConfigWithDefaults(config_dict)

    # 外部表現を先に構築し、対象パス項目を共通変換してからDirectoryPath / FilePathを検証する。
    try:
        host_config = HostServerSettings.model_validate(
            config_dict,
            context={'bypass_validation': True},
        )
        _CONFIG = host_config.toServerSettings(bypass_validation=bypass_validation)
        if bypass_validation is False:
            logging.debug('Server settings loaded.')
    except HostPathError as error:
        logging.error('設定内容が不正なため、KonomiTV-BS4K を起動できません。')
        logging.error(str(error))
        sys.exit(1)
    except ValidationError as error:
        if bypass_validation is False:
            # エラーのうちどれか一つでもカスタムバリデーターからのエラーだった場合、エラーメッセージを表示して終了する
            ## カスタムバリデーターからのエラーメッセージかどうかは ctx に error が含まれているかどうかで判定する
            custom_error = False
            for error_message in error.errors():
                if 'ctx' in error_message and 'error' in error_message['ctx']:
                    validation_error = error_message['ctx']['error']
                    if not isinstance(validation_error, (str, ValueError)):
                        continue
                    custom_error = True
                    for message in str(validation_error).split('\n'):
                        logging.error(message)
            if custom_error is True:
                sys.exit(1)

            # それ以外のバリデーションエラー
            logging.error('設定内容が不正なため、KonomiTV-BS4K を起動できません。')
            logging.error('以下のエラーメッセージを参考に、config.yaml の記述が正しいかを確認してください。')
            # Pydanticの文字列表現には内部パスと入力値が含まれるため、項目名と安全な説明だけを表示する。
            for error_message in error.errors(include_input=False):
                location = '.'.join(str(part) for part in error_message['loc'])
                logging.error(f'{location}: {error_message["msg"]}')
        else:
            logging.error('サーバー設定を復元できませんでした。')
        sys.exit(1)

    return _CONFIG


def SaveConfig(config: HostServerSettings) -> None:
    """
    変更されたサーバー設定データを、コメントやフォーマットを保持した形で config.yaml に書き込む
    この関数は _CONFIG を更新しないため、設定変更を反映するにはサーバーを再起動する必要がある
    (仮に _CONFIG を更新するよう実装しても、すでに Config() から取得した値を使って実行されている処理は更新できない)

    Args:
        config (HostServerSettings): ホストパスで受け取ったサーバー設定データ
    """

    global _CONFIG_YAML_PATH

    # 保存直前にも全対象を同じ変換表へ通し、旧形式入力をconfig.yamlへ残さない。
    config_dict = config.toSaveConfigDict()

    # config.yaml の内容をロード
    yaml = ruamel.yaml.YAML()
    yaml.default_flow_style = None  # None を使うと、スカラー以外のものはブロックスタイルになる
    yaml.preserve_quotes = True
    yaml.width = 20
    yaml.indent(mapping=4, sequence=4, offset=4)
    try:
        with open(_CONFIG_YAML_PATH, encoding='utf-8') as file:
            config_raw = yaml.load(file)
    except Exception as error:
        # 回復不可能
        raise RuntimeError(f'Failed to load config.yaml: {error}')

    # config.yaml の内容を更新して保存
    # コメントやフォーマットを保持して保存するために更新方法を工夫している
    for key in config_dict:
        # config.yaml 側に存在しないセクションがある場合は新規で作成する
        if key not in config_raw or config_raw[key] is None:
            config_raw[key] = ruamel.yaml.CommentedMap()
        for sub_key in config_dict[key]:
            # config.yaml 側に存在しないキーがある場合は新規で作成する
            if sub_key not in config_raw[key]:
                if type(config_dict[key][sub_key]) is list:
                    config_raw[key][sub_key] = ruamel.yaml.CommentedSeq()
                else:
                    config_raw[key][sub_key] = None
            # 文字列のリストを更新する場合は clear() と extend() を使う
            if type(config_dict[key][sub_key]) is list:
                if type(config_raw[key][sub_key]) is ruamel.yaml.CommentedSeq:
                    config_raw[key][sub_key].clear()
                    for item in config_dict[key][sub_key]:
                        config_raw[key][sub_key].append(ruamel.yaml.scalarstring.SingleQuotedScalarString(item))
                else:
                    config_raw[key][sub_key] = ruamel.yaml.CommentedSeq(config_dict[key][sub_key])
            # 文字列は明示的に SingleQuotedScalarString に変換する
            elif type(config_dict[key][sub_key]) is str:
                config_raw[key][sub_key] = ruamel.yaml.scalarstring.SingleQuotedScalarString(config_dict[key][sub_key])
            else:
                config_raw[key][sub_key] = config_dict[key][sub_key]

    # None を null として出力するようにする
    yaml.Representer.add_representer(type(None), lambda self, data: self.represent_scalar('tag:yaml.org,2002:null', 'null'))  # type: ignore

    # 配列の末尾の "']" を "',\n    ]" に変換する transform 関数を定義
    # 基本的に recorded_folders 用 (ruamel.yaml がフロースタイルの改行などを保持できないための苦肉の策)
    def transform(value: str) -> str:
        return value.replace("']", "',\n    ]")

    with open(_CONFIG_YAML_PATH, mode='w', encoding='utf-8') as file:
        yaml.dump(config_raw, file, transform=transform)


def SaveConfigAndApply(config: HostServerSettings, *, bypass_validation: bool = True) -> ServerSettings:
    """
    config.yaml へ保存し、稼働中の _CONFIG も差し替える。

    通常のサーバー設定更新 (SettingsRouter) は再起動前提のため SaveConfig のみを使う。
    CM 解析設定のように WebUI から即時反映が必要な項目はこちらを使う。

    Args:
        config (HostServerSettings): ホストパスで受け取ったサーバー設定データ。
        bypass_validation (bool): エンコーダー接続などの重い検証を省略するか。
            稼働中プロセスからの部分更新では True を推奨する。

    Returns:
        ServerSettings: 差し替え後の内部実行用設定。
    """

    global _CONFIG
    SaveConfig(config)
    # エンコーダー再検査を避けるため、既に検証済みの稼働中更新では bypass する。
    _CONFIG = config.toServerSettings(bypass_validation=bypass_validation)
    return _CONFIG


def Config() -> ServerSettings:
    """
    LoadConfig() でグローバル変数に格納したサーバー設定データを取得する
    この関数は、LoadConfig() を実行した後に呼び出さなければならない

    Returns:
        ServerSettings: サーバー設定データ
    """

    global _CONFIG
    assert _CONFIG is not None, 'Server settings have not been initialized.'
    return _CONFIG
