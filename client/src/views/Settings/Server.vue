<template>
    <!-- ベース画面の中にそれぞれの設定画面で異なる部分を記述する -->
    <component :is="embedded ? 'div' : SettingsBase">
        <h2 class="settings__heading" v-if="embedded === false">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:server-surface-16-filled" width="22px" />
            <span class="ml-2">{{section_title}}</span>
        </h2>
        <div class="settings__description" v-if="embedded === false">
            {{section_description}}<br>
        </div>
        <div class="settings__description mt-1" v-if="embedded === false && section !== 'users'">
            [サーバー設定を更新] ボタンを押さずにこのページから離れると、変更内容は破棄されます。<br>
            変更を反映するには KonomiTV サーバーの再起動が必要です。<br>
        </div>
        <div class="settings__content" :class="{'settings__content--disabled': is_disabled}">
            <div class="settings__content-heading" v-if="isSectionVisible('backend') || isSectionVisible('streaming') || isSectionVisible('diagnostics')">
                <Icon icon="fa-solid:sliders-h" width="22px" style="padding: 0 3px;" />
                <span class="ml-2">{{general_section_title}}</span>
            </div>
            <div class="settings__item settings__item--switch" v-if="isSectionVisible('backend')">
                <label class="settings__item-heading" for="jikkyo_enabled_on_server">ニコニコ実況 / NX-Jikkyo 連携を有効にする</label>
                <label class="settings__item-label" for="jikkyo_enabled_on_server">
                    無効にすると、KonomiTV サーバーとクライアントの両方からニコニコ実況 / NX-Jikkyo へのアクセスを停止し、関連 UI も非表示にします。デフォルトは無効です。<br>
                    保存済みのニコニコアカウント連携情報とクライアント設定は削除されません。変更の反映には KonomiTV サーバーの再起動と、開いているすべてのクライアントの再読み込みが必要です。<br>
                    ブラウザから外部サービスへ直接接続済みの WebSocket は、KonomiTV サーバー側から切断できません。<br>
                </label>
                <v-switch class="settings__item-switch" color="primary" id="jikkyo_enabled_on_server" hide-details
                    v-model="server_settings.general.jikkyo_enabled">
                </v-switch>
            </div>
            <div class="settings__item" v-if="isSectionVisible('backend')">
                <div class="settings__item-heading">利用するバックエンド</div>
                <div class="settings__item-label">
                    EDCB・Mirakurun のいずれかを選択してください。<br>
                    バックエンドに Mirakurun が選択されているときは、録画予約機能は利用できません。<br>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'"
                    :items="['EDCB', 'Mirakurun']" v-model="server_settings.general.backend">
                </v-select>
            </div>
            <div class="settings__item" v-if="isSectionVisible('backend')">
                <div class="settings__item-heading">EDCB (EpgTimerNW) の TCP API の URL</div>
                <div class="settings__item-label">
                    バックエンドに EDCB が選択されているときに利用されます。<br>
                    tcp://edcb-namedpipe/ と指定すると、TCP API の代わりに名前付きパイプを使って通信します (ローカルのみ)。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'"
                    v-model="server_settings.general.edcb_url">
                </v-text-field>
            </div>
            <div class="settings__item" v-if="isSectionVisible('backend')">
                <div class="settings__item-heading">Mirakurun / mirakc の HTTP API の URL</div>
                <div class="settings__item-label">
                    バックエンドに Mirakurun が選択されているときに利用されます。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'"
                    v-model="server_settings.general.mirakurun_url">
                </v-text-field>
            </div>
            <div class="settings__item" v-if="isSectionVisible('streaming')">
                <div class="settings__item-heading">録画と BS4K 以外で利用するエンコーダー</div>
                <div class="settings__item-label">
                    FFmpeg はソフトウェアエンコーダーです。<br>
                    すべての PC で利用できますが、CPU に多大な負荷がかかり、パフォーマンスが悪いです。<br>
                </div>
                <div class="settings__item-label mt-1">
                    QSVEncC・NVEncC・VCEEncC はハードウェアエンコーダーです。<br>
                    CPU 負荷が低く、パフォーマンスがとても高いです（おすすめ）。<br>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'"
                    :items="encoder_options"
                    v-model="server_settings.general.encoder">
                </v-select>
            </div>
            <div class="settings__item" v-if="isSectionVisible('backend')">
                <div class="settings__item-heading">番組情報の更新間隔 (分)</div>
                <div class="settings__item-label">
                    番組情報を EDCB または Mirakurun / mirakc から取得する間隔を設定します。デフォルトは 5 (分) です。<br>
                </div>
                <v-slider class="settings__item-form" color="primary" show-ticks="always" thumb-label hide-details
                    :min="0.5" :max="60" :step="0.5"
                    :density="is_form_dense ? 'compact' : 'default'"
                    v-model="server_settings.general.program_update_interval">
                </v-slider>
            </div>
            <div class="settings__item settings__item--switch" v-if="isSectionVisible('diagnostics')">
                <label class="settings__item-heading" for="debug">デバッグモードを有効にする</label>
                <label class="settings__item-label" for="debug">
                    有効にすると、デバッグログも出力されるようになります。<br>
                </label>
                <v-switch class="settings__item-switch" color="primary" id="debug" hide-details
                    v-model="server_settings.general.debug">
                </v-switch>
            </div>
            <div class="settings__item settings__item--switch" v-if="isSectionVisible('diagnostics')">
                <label class="settings__item-heading" for="debug_encoder">エンコーダーのログを有効にする</label>
                <label class="settings__item-label" for="debug_encoder">
                    有効にすると、ライブ視聴時のエンコーダーのログが KonomiTV/server/logs/ 以下に保存されます。<br>
                    さらにデバッグモード有効時は、サーバーログにエンコーダーのログがリアルタイム出力されます。<br>
                </label>
                <v-switch class="settings__item-switch" color="primary" id="debug_encoder" hide-details
                    v-model="server_settings.general.debug_encoder">
                </v-switch>
            </div>
            <div class="settings__content-heading mt-6" v-if="isSectionVisible('network')">
                <Icon icon="fluent:server-surface-16-filled" width="22px" />
                <span class="ml-2">ネットワーク・HTTPS</span>
            </div>
            <div class="settings__item" v-if="isSectionVisible('network')">
                <div class="settings__item-heading">KonomiTV サーバーのリッスンポート</div>
                <div class="settings__item-label">
                    デフォルトのリッスンポートは 7000 です。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'"
                    v-model="server_settings.server.port">
                </v-text-field>
            </div>
            <div class="settings__item" v-if="isSectionVisible('network')">
                <div class="settings__item-heading">HTTPS / リバースプロキシの動作モード</div>
                <div class="settings__item-label">
                    akebi は従来どおり Akebi Keyless Server で HTTPS を提供します。<br>
                    certificate は指定した証明書で直接 HTTPS を提供します。reverse_proxy は信頼済みプロキシの背後で HTTP を提供します。<br>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :items="['akebi', 'certificate', 'reverse_proxy']"
                    :density="is_form_dense ? 'compact' : 'default'"
                    v-model="server_settings.server.https_mode">
                </v-select>
            </div>
            <div class="settings__item" v-if="isSectionVisible('network') && server_settings.server.https_mode === 'certificate'">
                <div class="settings__item-heading">HTTPS 証明書・秘密鍵ファイルへの絶対パス</div>
                <div class="settings__item-label">
                    certificate モードでは証明書と秘密鍵の両方が必須です。Docker ではホスト上の絶対パスを指定してください。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" hide-details
                    label="例: /etc/letsencrypt/live/tv.example.com/fullchain.pem"
                    :density="is_form_dense ? 'compact' : 'default'"
                    v-model="server_settings.server.custom_https_certificate">
                </v-text-field>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" hide-details
                    label="例: /etc/letsencrypt/live/tv.example.com/privkey.pem"
                    :density="is_form_dense ? 'compact' : 'default'"
                    v-model="server_settings.server.custom_https_private_key">
                </v-text-field>
            </div>
            <div class="settings__item" v-if="isSectionVisible('network') && server_settings.server.https_mode === 'reverse_proxy'">
                <div class="settings__item-heading">リバースプロキシ用 HTTP リッスンアドレス</div>
                <div class="settings__item-label">
                    通常は 0.0.0.0 のまま変更する必要はありません。KonomiTV のポートを外部へ直接公開しないでください。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" hide-details
                    label="例: 0.0.0.0"
                    :density="is_form_dense ? 'compact' : 'default'"
                    v-model="server_settings.server.reverse_proxy_listen_address">
                </v-text-field>
                <div class="settings__item-heading mt-5">信頼済みリバースプロキシの CIDR 許可リスト</div>
                <div class="settings__item-label">
                    実際に KonomiTV へ接続する nginx / Apache などの送信元 CIDR を1件以上指定してください。IPv4・IPv6に対応しています。<br>
                </div>
                <div v-for="(cidr, index) in server_settings.server.trusted_proxy_cidrs" :key="'trusted-proxy-cidr-' + index">
                    <div class="d-flex align-center mt-3">
                        <v-text-field class="settings__item-form mt-0" color="primary" variant="outlined" hide-details
                            placeholder="例: 172.18.0.0/16"
                            :density="is_form_dense ? 'compact' : 'default'"
                            v-model="server_settings.server.trusted_proxy_cidrs[index]">
                        </v-text-field>
                        <button v-ripple class="settings__item-delete-button"
                            @click="server_settings.server.trusted_proxy_cidrs.splice(index, 1)">
                            <svg class="iconify iconify--fluent" width="20px" height="20px" viewBox="0 0 16 16">
                                <path fill="currentColor" d="M7 3h2a1 1 0 0 0-2 0ZM6 3a2 2 0 1 1 4 0h4a.5.5 0 0 1 0 1h-.564l-1.205 8.838A2.5 2.5 0 0 1 9.754 15H6.246a2.5 2.5 0 0 1-2.477-2.162L2.564 4H2a.5.5 0 0 1 0-1h4Zm1 3.5a.5.5 0 0 0-1 0v5a.5.5 0 0 0 1 0v-5ZM9.5 6a.5.5 0 0 0-.5.5v5a.5.5 0 0 0 1 0v-5a.5.5 0 0 0-.5-.5Z"></path>
                            </svg>
                        </button>
                    </div>
                </div>
                <v-btn class="mt-3" color="background-lighten-2" variant="flat" height="40px"
                    @click="server_settings.server.trusted_proxy_cidrs.push('')">
                    <Icon icon="fluent:add-12-filled" height="17px" />
                    <span class="ml-1">信頼済み CIDR を追加</span>
                </v-btn>
            </div>
            <div class="settings__content-heading mt-6" v-if="section === 'basic'">
                <Icon icon="fluent:plug-connected-20-filled" width="22px" />
                <span class="ml-2">互換 API</span>
            </div>
            <div class="settings__item settings__item--switch" v-if="section === 'basic'">
                <label class="settings__item-heading" for="compatibility_api_enabled">互換 API を有効にする</label>
                <label class="settings__item-label" for="compatibility_api_enabled">
                    Komorebi など、KonomiTV 互換 API を利用するクライアント向けの API を専用ポートで公開します。<br>
                    通常の KonomiTV Web UI・API のリッスンポートや動作は変更されません。<br>
                </label>
                <v-switch class="settings__item-switch" color="primary" id="compatibility_api_enabled" hide-details
                    v-model="server_settings.compatibility_api.enabled">
                </v-switch>
            </div>
            <div class="settings__item" v-if="section === 'basic'">
                <div class="settings__item-heading">互換 API 専用の HTTPS / リバースプロキシ動作モード</div>
                <div class="settings__item-label">
                    以下の接続設定は互換 API 専用です。通常の KonomiTV Web UI・API の接続設定には影響しません。<br>
                    「通常 API と同じ設定を使用」を選んだ場合だけ、上の KonomiTV サーバー設定を引き継ぎます。<br>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :items="compatibility_https_mode_options"
                    :density="is_form_dense ? 'compact' : 'default'"
                    v-model="server_settings.compatibility_api.https_mode">
                </v-select>
            </div>
            <div class="settings__item"
                v-if="section === 'basic' && server_settings.compatibility_api.https_mode === 'certificate'">
                <div class="settings__item-heading">互換 API 専用 HTTPS 証明書・秘密鍵ファイルへの絶対パス</div>
                <div class="settings__item-label">
                    通常 API に設定した証明書は使用しません。Docker ではホスト上の絶対パスを両方指定してください。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" hide-details="auto"
                    label="例: /etc/letsencrypt/live/compat.example.com/fullchain.pem"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :error-messages="compatibility_api_certificate_error"
                    v-model="server_settings.compatibility_api.custom_https_certificate">
                </v-text-field>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" hide-details="auto"
                    label="例: /etc/letsencrypt/live/compat.example.com/privkey.pem"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :error-messages="compatibility_api_private_key_error"
                    v-model="server_settings.compatibility_api.custom_https_private_key">
                </v-text-field>
            </div>
            <div class="settings__item"
                v-if="section === 'basic' && server_settings.compatibility_api.https_mode === 'reverse_proxy'">
                <div class="settings__item-heading">互換 API 専用 HTTP リッスンアドレス</div>
                <div class="settings__item-label">
                    このアドレスと互換 API ポートはリバースプロキシからだけ到達可能にし、外部へ直接公開しないでください。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" hide-details="auto"
                    label="例: 0.0.0.0"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :error-messages="compatibility_api_reverse_proxy_listen_address_error"
                    v-model="server_settings.compatibility_api.reverse_proxy_listen_address">
                </v-text-field>
                <div class="settings__item-heading mt-5">互換 API で信頼するリバースプロキシの CIDR</div>
                <div class="settings__item-label">
                    上の通常 API の許可リストとは別です。実際に互換 API へ接続する nginx / Apache などの送信元 CIDR を1件以上指定してください。<br>
                </div>
                <div v-for="(cidr, index) in server_settings.compatibility_api.trusted_proxy_cidrs"
                    :key="'compatibility-trusted-proxy-cidr-' + index">
                    <div class="d-flex align-center mt-3">
                        <v-text-field class="settings__item-form mt-0" color="primary" variant="outlined" hide-details
                            placeholder="例: 172.18.0.0/16"
                            :density="is_form_dense ? 'compact' : 'default'"
                            v-model="server_settings.compatibility_api.trusted_proxy_cidrs[index]">
                        </v-text-field>
                        <button v-ripple class="settings__item-delete-button"
                            @click="server_settings.compatibility_api.trusted_proxy_cidrs.splice(index, 1)">
                            <svg class="iconify iconify--fluent" width="20px" height="20px" viewBox="0 0 16 16">
                                <path fill="currentColor" d="M7 3h2a1 1 0 0 0-2 0ZM6 3a2 2 0 1 1 4 0h4a.5.5 0 0 1 0 1h-.564l-1.205 8.838A2.5 2.5 0 0 1 9.754 15H6.246a2.5 2.5 0 0 1-2.477-2.162L2.564 4H2a.5.5 0 0 1 0-1h4Zm1 3.5a.5.5 0 0 0-1 0v5a.5.5 0 0 0 1 0v-5ZM9.5 6a.5.5 0 0 0-.5.5v5a.5.5 0 0 0 1 0v-5a.5.5 0 0 0-.5-.5Z"></path>
                            </svg>
                        </button>
                    </div>
                </div>
                <div class="settings__item-label text-error mt-2"
                    v-if="compatibility_api_trusted_proxy_cidrs_error !== ''">
                    {{compatibility_api_trusted_proxy_cidrs_error}}
                </div>
                <v-btn class="mt-3" color="background-lighten-2" variant="flat" height="40px"
                    @click="server_settings.compatibility_api.trusted_proxy_cidrs.push('')">
                    <Icon icon="fluent:add-12-filled" height="17px" />
                    <span class="ml-1">互換 API の信頼済み CIDR を追加</span>
                </v-btn>
            </div>
            <div class="settings__item" v-if="section === 'basic'">
                <div class="settings__item-heading">互換 API のリッスンポート</div>
                <div class="settings__item-label">
                    通常の KonomiTV サーバーとは異なる未使用のポートを指定してください。デフォルトは 7200 です。<br>
                    Akebi では指定ポートに加えて、10 番大きいポートを内部 HTTP 通信に使用します。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" type="number"
                    min="1024" max="65525" step="1" hide-details="auto"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :error-messages="compatibility_api_port_error"
                    v-model.number="server_settings.compatibility_api.port">
                </v-text-field>
            </div>
            <div class="settings__item" v-if="section === 'basic'">
                <div class="settings__item-heading">互換プロファイル</div>
                <div class="settings__item-label">
                    接続するクライアントが期待する API 仕様を選択します。現在は Komorebi V1 のみ利用できます。<br>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'"
                    :items="compatibility_profile_options"
                    v-model="server_settings.compatibility_api.profile">
                </v-select>
            </div>
            <div class="settings__content-heading mt-6" v-if="isSectionVisible('backend') || isSectionVisible('streaming')">
                <Icon icon="fluent:tv-20-filled" width="22px" />
                <span class="ml-2">{{tv_section_title}}</span>
            </div>
            <div class="settings__item settings__item--switch" v-if="isSectionVisible('backend')">
                <label class="settings__item-heading" for="always_receive_tv_from_mirakurun">常に Mirakurun / mirakc から放送波を受信する</label>
                <label class="settings__item-label" for="always_receive_tv_from_mirakurun">
                    利用するバックエンドが EDCB のとき、常に Mirakurun / mirakc から放送波を受信するかを設定します。
                    バックエンドに Mirakurun が選択されているときは効果がありません。<br>
                </label>
                <label class="settings__item-label mt-1" for="always_receive_tv_from_mirakurun">
                    KonomiTV から EDCB と Mirakurun / mirakc 両方にアクセスできる必要があります。<br>
                    EDCB はチューナー起動やチャンネル切り替えに時間がかかるため、Mirakurun / mirakc が利用できる環境であれば、この設定を有効にするとより快適に使えます。<br>
                </label>
                <v-switch class="settings__item-switch" color="primary" id="always_receive_tv_from_mirakurun" hide-details
                    v-model="server_settings.general.always_receive_tv_from_mirakurun">
                </v-switch>
            </div>
            <div class="settings__item" v-if="isSectionVisible('backend')">
                <div class="settings__item-heading">チャンネル表示・選局で優先するエリア (地デジ)</div>
                <div class="settings__item-label">
                    複数の地域の放送波が受信できる環境で、リモコン番号が同じチャンネルが複数ある場合に、どのエリアのチャンネルを優先して表示・選局するかを設定します。デフォルトは未設定です。<br>
                </div>
                <div class="settings__item-label mt-1">
                    優先エリアのチャンネルは枝番なし (例: Ch:011) で、それ以外のチャンネルは枝番付き (例: Ch:011-1) で表示されます。キーボードショートカットやリモコンボタンでの選局時も、優先エリアのチャンネルが選局されます。<br>
                </div>
                <div class="settings__item-label mt-1">
                    設定しない場合は、(ネットワークID)-(サービスID) の数値順で優先順位が決まります。<br>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined" hide-details
                    :density="is_form_dense ? 'compact' : 'default'"
                    :items="preferred_terrestrial_region_options"
                    v-model="server_settings.tv.preferred_terrestrial_region">
                </v-select>
            </div>
            <div class="settings__item" v-if="isSectionVisible('streaming')">
                <div class="settings__item-heading">誰も見ていないチャンネルのエンコードタスクを維持する秒数</div>
                <div class="settings__item-label">
                    10 秒に設定したなら、10 秒間誰も見ていない状態が継続したらエンコードタスク（エンコーダー）を終了します。<br>
                </div>
                <div class="settings__item-label mt-1">
                    0 秒に設定すると、ネット回線が瞬断したりリロードしただけでチューナーとエンコーダーの再起動が必要になり、再生復帰までに時間がかかります。余裕をもたせておく事をおすすめします。<br>
                </div>
                <v-slider class="settings__item-form" color="primary" show-ticks="always" thumb-label hide-details
                    :min="0" :max="60" :step="1"
                    :density="is_form_dense ? 'compact' : 'default'"
                    v-model="server_settings.tv.max_alive_time">
                </v-slider>
            </div>
            <div class="settings__content-heading mt-6" v-if="isSectionVisible('storage')">
                <Icon icon="fluent:movies-and-tv-20-filled" width="22px" />
                <span class="ml-2">ビデオのオンデマンドストリーミング</span>
            </div>
            <div class="settings__item" v-if="isSectionVisible('storage')">
                <div class="settings__item-heading">録画済み番組の保存先フォルダの絶対パス</div>
                <div class="settings__item-label" style="padding-bottom: 2px;">
                    指定フォルダ以下に保存されている MPEG-TS 形式の録画ファイルを KonomiTV サーバーが自動的に見つけ出し、メタデータの解析とサムネイルの作成を行います。<br>
                    解析が完了すると、録画番組一覧から再生できるようになります。<br>
                </div>
                <div class="settings__item-label mt-1" style="padding-bottom: 2px;">
                    複数の保存先フォルダを指定できます。フォルダやファイルのシンボリックリンクにも対応しています。<br>
                    シンボリックリンクは実体のパスに変換されるため、同じ録画ファイルが重複スキャンされることはありません。<br>
                </div>
                <div v-for="(folder, index) in server_settings.video.recorded_folders" :key="'recorded-folder-' + index">
                    <div class="d-flex align-center mt-3">
                        <v-text-field class="settings__item-form mt-0" color="primary" variant="outlined" hide-details
                            placeholder="例: E:\TV-Record"
                            :density="is_form_dense ? 'compact' : 'default'"
                            v-model="server_settings.video.recorded_folders[index]">
                        </v-text-field>
                        <button v-ripple class="settings__item-delete-button"
                            @click="server_settings.video.recorded_folders.splice(index, 1)">
                            <svg class="iconify iconify--fluent" width="20px" height="20px" viewBox="0 0 16 16">
                                <path fill="currentColor" d="M7 3h2a1 1 0 0 0-2 0ZM6 3a2 2 0 1 1 4 0h4a.5.5 0 0 1 0 1h-.564l-1.205 8.838A2.5 2.5 0 0 1 9.754 15H6.246a2.5 2.5 0 0 1-2.477-2.162L2.564 4H2a.5.5 0 0 1 0-1h4Zm1 3.5a.5.5 0 0 0-1 0v5a.5.5 0 0 0 1 0v-5ZM9.5 6a.5.5 0 0 0-.5.5v5a.5.5 0 0 0 1 0v-5a.5.5 0 0 0-.5-.5Z"></path>
                            </svg>
                        </button>
                    </div>
                </div>
                <v-btn class="mt-3" color="background-lighten-2" variant="flat" height="40px"
                    @click="server_settings.video.recorded_folders.push('')">
                    <Icon icon="fluent:add-12-filled" height="17px" />
                    <span class="ml-1">保存先フォルダを追加</span>
                </v-btn>
            </div>
            <div class="settings__item" v-if="isSectionVisible('storage')">
                <div class="settings__item-heading">録画フォルダのスキャン対象から除外するフォルダの絶対パス</div>
                <div class="settings__item-label" style="padding-bottom: 2px;">
                    録画フォルダ以下にある一時フォルダなど、スキャン対象から除外したいサブフォルダを指定できます。<br>
                </div>
                <div class="settings__item-label mt-1" style="padding-bottom: 2px;">
                    シンボリックリンク解決前のパスと、解決後の実体パスの両方で前方一致判定を行います。<br>
                    例えば、<code>E:\TV-Record\Temp</code> を指定すると、そのサブフォルダ以下の録画ファイルはスキャン対象から除外されます。<br>
                </div>
                <div v-for="(pattern, index) in server_settings.video.exclude_scan_paths" :key="'exclude-pattern-' + index">
                    <div class="d-flex align-center mt-3">
                        <v-text-field class="settings__item-form mt-0" color="primary" variant="outlined" hide-details
                            placeholder="例: E:\TV-Record\Trash"
                            :density="is_form_dense ? 'compact' : 'default'"
                            v-model="server_settings.video.exclude_scan_paths[index]">
                        </v-text-field>
                        <button v-ripple class="settings__item-delete-button"
                            @click="server_settings.video.exclude_scan_paths.splice(index, 1)">
                            <svg class="iconify iconify--fluent" width="20px" height="20px" viewBox="0 0 16 16">
                                <path fill="currentColor" d="M7 3h2a1 1 0 0 0-2 0ZM6 3a2 2 0 1 1 4 0h4a.5.5 0 0 1 0 1h-.564l-1.205 8.838A2.5 2.5 0 0 1 9.754 15H6.246a2.5 2.5 0 0 1-2.477-2.162L2.564 4H2a.5.5 0 0 1 0-1h4Zm1 3.5a.5.5 0 0 0-1 0v5a.5.5 0 0 0 1 0v-5ZM9.5 6a.5.5 0 0 0-.5.5v5a.5.5 0 0 0 1 0v-5a.5.5 0 0 0-.5-.5Z"></path>
                            </svg>
                        </button>
                    </div>
                </div>
                <v-btn class="mt-3" color="background-lighten-2" variant="flat" height="40px"
                    @click="server_settings.video.exclude_scan_paths.push('')">
                    <Icon icon="fluent:add-12-filled" height="17px" />
                    <span class="ml-1">除外フォルダを追加</span>
                </v-btn>
            </div>
            <div class="settings__item" v-if="isSectionVisible('storage')">
                <div class="settings__item-heading">録画再生 fMP4 キャッシュの保存先フォルダの絶対パス</div>
                <div class="settings__item-label" style="padding-bottom: 2px;">
                    未指定の場合は、各録画ファイルと同じフォルダへキャッシュファイルを保存します。<br>
                    Docker 版でもホストマシン側の絶対パスを指定してください。設定はサーバー再起動後に反映されます。<br>
                </div>
                <v-text-field class="settings__item-form mt-3" color="primary" variant="outlined" hide-details
                    placeholder="未指定（録画ファイルと同じフォルダ）"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :model-value="server_settings.video.recorded_fmp4_cache_folder ?? ''"
                    @update:model-value="server_settings.video.recorded_fmp4_cache_folder = $event === '' ? null : String($event)">
                </v-text-field>
            </div>
            <div class="settings__item" v-if="isSectionVisible('storage')">
                <div class="settings__item-heading">既存録画の再生用インデックスを自動生成する</div>
                <div class="settings__item-label">
                    無効にすると、サーバー起動後に既存録画を順番に解析するバックフィルだけを停止します。<br>
                    再生を要求した録画と新しく完了した録画のインデックス生成は、無効時も継続します。設定はサーバー再起動後に反映されます。<br>
                </div>
                <v-switch class="settings__item-switch" color="primary"
                    id="recorded_playback_index_backfill_enabled" hide-details
                    v-model="server_settings.video.recorded_playback_index_backfill_enabled">
                </v-switch>
            </div>
            <div class="settings__content-heading mt-6" v-if="isSectionVisible('storage')">
                <Icon icon="fluent:image-multiple-16-filled" width="22px" />
                <span class="ml-2">キャプチャ</span>
            </div>
            <div class="settings__item" v-if="isSectionVisible('storage')">
                <div class="settings__item-heading">アップロードしたキャプチャ画像の保存先フォルダの絶対パス</div>
                <div class="settings__item-label">
                    <router-link class="link" to="/settings/personal/capture">[キャプチャ]</router-link> → [キャプチャの保存先] で [KonomiTV サーバーにアップロード] または
                    [ブラウザでのダウンロードと、KonomiTV サーバーへのアップロードを両方行う] が選択されているときに利用されます。<br>
                </div>
                <div class="settings__item-label mt-1" style="padding-bottom: 2px;">
                    複数の保存先フォルダを指定できます。<br>
                    先頭から順に利用され、保存先フォルダがいっぱいになったら次の保存先フォルダに保存されます。<br>
                </div>
                <div v-for="(folder, index) in server_settings.capture.upload_folders" :key="'upload-folder-' + index">
                    <div class="d-flex align-center mt-3">
                        <v-text-field class="settings__item-form mt-0" color="primary" variant="outlined" hide-details
                            placeholder="例: E:\TV-Capture"
                            :density="is_form_dense ? 'compact' : 'default'"
                            v-model="server_settings.capture.upload_folders[index]">
                        </v-text-field>
                        <button v-ripple class="settings__item-delete-button"
                            @click="server_settings.capture.upload_folders.splice(index, 1)">
                            <svg class="iconify iconify--fluent" width="20px" height="20px" viewBox="0 0 16 16">
                                <path fill="currentColor" d="M7 3h2a1 1 0 0 0-2 0ZM6 3a2 2 0 1 1 4 0h4a.5.5 0 0 1 0 1h-.564l-1.205 8.838A2.5 2.5 0 0 1 9.754 15H6.246a2.5 2.5 0 0 1-2.477-2.162L2.564 4H2a.5.5 0 0 1 0-1h4Zm1 3.5a.5.5 0 0 0-1 0v5a.5.5 0 0 0 1 0v-5ZM9.5 6a.5.5 0 0 0-.5.5v5a.5.5 0 0 0 1 0v-5a.5.5 0 0 0-.5-.5Z"></path>
                            </svg>
                        </button>
                    </div>
                </div>
                <v-btn class="mt-3" color="background-lighten-2" variant="flat" height="40px"
                    @click="server_settings.capture.upload_folders.push('')">
                    <Icon icon="fluent:add-12-filled" height="17px" />
                    <span class="ml-1">保存先フォルダを追加</span>
                </v-btn>
            </div>
            <v-btn class="settings__save-button bg-secondary mt-6" variant="flat"
                v-if="section !== 'users'"
                :disabled="section === 'basic' && has_compatibility_api_validation_error"
                @click="updateServerSettings()">
                <Icon icon="fluent:save-16-filled" class="mr-2" height="23px" />サーバー設定を更新
            </v-btn>
            <div class="settings__content-heading mt-8" v-if="isSectionVisible('users')">
                <Icon icon="fluent:person-board-20-filled" width="22px" />
                <span class="ml-2">アカウント</span>
            </div>
            <div class="settings__item" v-if="isSectionVisible('users')">
                <div class="settings__item-heading">アカウントの管理</div>
                <div class="settings__item-label">
                    現在 KonomiTV に登録されているすべてのアカウントの一覧の確認、管理者権限の付与/剥奪、アカウントの削除ができます。<br>
                </div>
                <div class="settings__item-label mt-1">
                    ログイン中ユーザーの設定変更は、別途 <router-link class="link" to="/settings/account">アカウント設定画面</router-link> から行ってください。<br>
                </div>
            </div>
            <v-btn class="settings__save-button mt-4" variant="flat" v-if="isSectionVisible('users')"
                @click="account_manage_settings_modal = !account_manage_settings_modal">
                <Icon icon="fluent:person-board-20-filled" height="20px" />
                <span class="ml-1">アカウントの管理設定を開く</span>
            </v-btn>
        </div>
        <AccountManageSettings :modelValue="account_manage_settings_modal" @update:modelValue="account_manage_settings_modal = $event" />
    </component>
</template>
<script lang="ts" setup>

import { storeToRefs } from 'pinia';
import { computed, ref, toRaw, watch } from 'vue';

import type { IServerSettings } from '@/services/Settings';

import AccountManageSettings from '@/components/Settings/AccountManageSettings.vue';
import Message from '@/message';
import useServerSettingsStore from '@/stores/ServerSettingsStore';
import useUserStore from '@/stores/UserStore';
import Utils from '@/utils';
import SettingsBase from '@/views/Settings/Base.vue';

type ServerSettingsSection = 'basic' | 'backend' | 'network' | 'streaming' | 'storage' | 'users' | 'diagnostics' | 'all';

const props = withDefaults(defineProps<{
    section?: ServerSettingsSection;
    embedded?: boolean;
}>(), {
    section: 'all',
    embedded: false,
});

// 同じコンポーネントを複数の設定ルートから利用し、指定された責務の設定だけを表示する
const section = computed(() => props.section);
const embedded = computed(() => props.embedded);
const section_title = computed(() => ({
    basic: '基本・接続',
    backend: 'バックエンド・番組情報',
    network: 'ネットワーク・HTTPS',
    streaming: '配信・エンコーダー',
    storage: '録画・ストレージ',
    users: 'ユーザー管理',
    diagnostics: '診断設定',
    all: 'サーバー設定',
})[props.section]);
const section_description = computed(() => {
    if (props.section === 'users') {
        return 'KonomiTV に登録されているアカウントを管理します。管理者アカウントでログインしている必要があります。';
    }
    return `${section_title.value}を変更するには、管理者アカウントでログインしている必要があります。`;
});
const general_section_title = computed(() => ({
    basic: 'バックエンド・番組情報',
    backend: 'バックエンド・番組情報',
    streaming: '配信・エンコーダー',
    diagnostics: '診断設定',
    all: '全般',
})[props.section as 'basic' | 'backend' | 'streaming' | 'diagnostics' | 'all'] ?? '全般');
const tv_section_title = computed(() => {
    if (props.section === 'basic' || props.section === 'backend') {
        return '受信・チャンネル';
    }
    if (props.section === 'streaming') {
        return '通常放送';
    }
    return 'テレビのライブストリーミング';
});

function isSectionVisible(target_section: Exclude<ServerSettingsSection, 'all'>): boolean {
    if (props.section === 'all' || props.section === target_section) {
        return true;
    }
    return props.section === 'basic' && (target_section === 'backend' || target_section === 'network');
}

// フォームを小さくするかどうか
const is_form_dense = Utils.isSmartphoneHorizontal();

// エンコーダーの選択肢
const encoder_options = [
    {title: 'FFmpeg : ソフトウェアエンコーダー', value: 'FFmpeg'},
    {title: 'QSVEncC : Intel Graphics 搭載 CPU / Intel Arc GPU で利用可能', value: 'QSVEncC'},
    {title: 'NVEncC : NVIDIA GPU で利用可能', value: 'NVEncC'},
    {title: 'VCEEncC : AMD GPU で利用可能', value: 'VCEEncC'},
];

// KonomiTV 互換 API のプロファイル選択肢
const compatibility_profile_options = [
    {title: 'Komorebi V1', value: 'KomorebiV1'},
];

// KonomiTV 互換 API の HTTPS / リバースプロキシ動作モード
const compatibility_https_mode_options = [
    {title: '通常 API と同じ設定を使用', value: 'inherit'},
    {title: 'Akebi で HTTPS', value: 'akebi'},
    {title: '指定した証明書で直接 HTTPS', value: 'certificate'},
    {title: '信頼済みリバースプロキシの背後で HTTP', value: 'reverse_proxy'},
];

// 優先する地デジのエリアの選択肢
const preferred_terrestrial_region_options = [
    { title: '未設定', value: null },
    { title: '北海道（札幌）', value: '北海道（札幌）' },
    { title: '北海道（函館）', value: '北海道（函館）' },
    { title: '北海道（旭川）', value: '北海道（旭川）' },
    { title: '北海道（帯広）', value: '北海道（帯広）' },
    { title: '北海道（釧路）', value: '北海道（釧路）' },
    { title: '北海道（北見）', value: '北海道（北見）' },
    { title: '北海道（室蘭）', value: '北海道（室蘭）' },
    { title: '青森県', value: '青森県' },
    { title: '岩手県', value: '岩手県' },
    { title: '宮城県', value: '宮城県' },
    { title: '秋田県', value: '秋田県' },
    { title: '山形県', value: '山形県' },
    { title: '福島県', value: '福島県' },
    { title: '茨城県', value: '茨城県' },
    { title: '栃木県', value: '栃木県' },
    { title: '群馬県', value: '群馬県' },
    { title: '埼玉県', value: '埼玉県' },
    { title: '千葉県', value: '千葉県' },
    { title: '東京都', value: '東京都' },
    { title: '神奈川県', value: '神奈川県' },
    { title: '新潟県', value: '新潟県' },
    { title: '富山県', value: '富山県' },
    { title: '石川県', value: '石川県' },
    { title: '福井県', value: '福井県' },
    { title: '山梨県', value: '山梨県' },
    { title: '長野県', value: '長野県' },
    { title: '岐阜県', value: '岐阜県' },
    { title: '静岡県', value: '静岡県' },
    { title: '愛知県', value: '愛知県' },
    { title: '三重県', value: '三重県' },
    { title: '滋賀県', value: '滋賀県' },
    { title: '京都府', value: '京都府' },
    { title: '大阪府', value: '大阪府' },
    { title: '兵庫県', value: '兵庫県' },
    { title: '奈良県', value: '奈良県' },
    { title: '和歌山県', value: '和歌山県' },
    { title: '鳥取県', value: '鳥取県' },
    { title: '島根県', value: '島根県' },
    { title: '岡山県', value: '岡山県' },
    { title: '広島県', value: '広島県' },
    { title: '山口県', value: '山口県' },
    { title: '徳島県', value: '徳島県' },
    { title: '香川県', value: '香川県' },
    { title: '愛媛県', value: '愛媛県' },
    { title: '高知県', value: '高知県' },
    { title: '福岡県', value: '福岡県' },
    { title: '佐賀県', value: '佐賀県' },
    { title: '長崎県', value: '長崎県' },
    { title: '熊本県', value: '熊本県' },
    { title: '大分県', value: '大分県' },
    { title: '宮崎県', value: '宮崎県' },
    { title: '鹿児島県', value: '鹿児島県' },
    { title: '沖縄県', value: '沖縄県' },
];

// ユーザー情報を取得し、もし管理者権限であれば無効化を解除
const is_disabled = ref(true);
const user_store = useUserStore();
user_store.fetchUser().then((user) => {
    if (user && user.is_admin) {
        is_disabled.value = false;
    }
});

// ストアには最後に取得・保存した基準値だけを保持し、この画面では section ごとのローカルドラフトを編集する
// /api/settings/server は再起動前の稼働中設定を返すため、基準値の取得は一度だけにして直前の保存内容を維持する
const server_settings_store = useServerSettingsStore();
const { server_settings: base_server_settings } = storeToRefs(server_settings_store);
const server_settings = ref<IServerSettings>(structuredClone(toRaw(base_server_settings.value)));

// inherit の場合は通常 API の動作モードを利用して、実際に使用する物理リッスンポートを判定する
const compatibility_api_effective_https_mode = computed<IServerSettings['server']['https_mode']>(() => {
    const https_mode = server_settings.value.compatibility_api.https_mode;
    return https_mode === 'inherit' ? server_settings.value.server.https_mode : https_mode;
});

const compatibility_api_port_error = computed(() => {
    const compatibility_api_port = Number(server_settings.value.compatibility_api.port);
    const server_port = Number(server_settings.value.server.port);
    if (Number.isInteger(compatibility_api_port) === false || compatibility_api_port < 1024 || compatibility_api_port > 65525) {
        return '1024 ～ 65525 の整数を指定してください。';
    }
    if (server_settings.value.compatibility_api.enabled === false) {
        return '';
    }
    if (Number.isInteger(server_port) === false) {
        return '';
    }

    const server_listener_ports = [server_port];
    if (server_settings.value.server.https_mode === 'akebi') {
        server_listener_ports.push(server_port + 10);
    }
    const compatibility_api_listener_ports = [compatibility_api_port];
    if (compatibility_api_effective_https_mode.value === 'akebi') {
        compatibility_api_listener_ports.push(compatibility_api_port + 10);
    }
    const conflicted_port = compatibility_api_listener_ports.find(port => server_listener_ports.includes(port));
    if (conflicted_port !== undefined) {
        return `通常 API または Akebi の内部通信が使用するポート ${conflicted_port} と重複しています。`;
    }
    return '';
});

const compatibility_api_certificate_error = computed(() => {
    if (server_settings.value.compatibility_api.https_mode !== 'certificate') {
        return '';
    }
    const certificate = server_settings.value.compatibility_api.custom_https_certificate;
    if (typeof certificate !== 'string' || certificate.trim().startsWith('/') === false) {
        return '証明書ファイルの絶対パスを指定してください。';
    }
    return '';
});

const compatibility_api_private_key_error = computed(() => {
    if (server_settings.value.compatibility_api.https_mode !== 'certificate') {
        return '';
    }
    const private_key = server_settings.value.compatibility_api.custom_https_private_key;
    if (typeof private_key !== 'string' || private_key.trim().startsWith('/') === false) {
        return '秘密鍵ファイルの絶対パスを指定してください。';
    }
    return '';
});

const compatibility_api_reverse_proxy_listen_address_error = computed(() => {
    if (server_settings.value.compatibility_api.https_mode !== 'reverse_proxy') {
        return '';
    }
    if (server_settings.value.compatibility_api.reverse_proxy_listen_address.trim() === '') {
        return '互換 API の HTTP リッスンアドレスを指定してください。';
    }
    return '';
});

const compatibility_api_trusted_proxy_cidrs_error = computed(() => {
    if (server_settings.value.compatibility_api.https_mode !== 'reverse_proxy') {
        return '';
    }
    const has_trusted_proxy_cidr = server_settings.value.compatibility_api.trusted_proxy_cidrs
        .some(cidr => cidr.trim() !== '');
    return has_trusted_proxy_cidr ? '' : '信頼するリバースプロキシの CIDR を1件以上指定してください。';
});

const has_compatibility_api_validation_error = computed(() => [
    compatibility_api_port_error.value,
    compatibility_api_certificate_error.value,
    compatibility_api_private_key_error.value,
    compatibility_api_reverse_proxy_listen_address_error.value,
    compatibility_api_trusted_proxy_cidrs_error.value,
].some(error => error !== ''));

function resetServerSettingsDraft(): void {
    server_settings.value = structuredClone(toRaw(base_server_settings.value));
}

server_settings_store.fetchServerSettingsOnce().then((settings) => {
    if (settings !== null) {
        resetServerSettingsDraft();
    }
});

// 同じコンポーネントを使う別 section へ移動した場合、保存していない変更は基準値へ戻す
watch(() => props.section, () => {
    resetServerSettingsDraft();
});

// サーバー設定を更新する関数
async function updateServerSettings() {
    if (props.section === 'basic' && has_compatibility_api_validation_error.value) {
        return;
    }

    // すべての section と BS4K 設定で同じ正規化・更新経路を利用する
    const result = await server_settings_store.updateServerSettings(server_settings.value);

    // 成功した場合のみメッセージを表示
    // エラー処理は Services 層で行われるため、ここではエラー処理は不要
    // 再起動するまでは設定データは反映されないため、再起動せずにページをリロードすると反映されてないように見える点に注意
    if (result === true) {
        resetServerSettingsDraft();
        Message.success('サーバー設定を更新しました。\n変更を反映するためには、KonomiTV サーバーを再起動してください。');
    }
}

// ユーザー管理モーダルの表示状態
const account_manage_settings_modal = ref(false);

</script>
