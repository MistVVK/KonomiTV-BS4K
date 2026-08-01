<template>
    <SettingsBase>
        <h2 class="settings__heading">
            <a v-ripple class="settings__back-button" @click="$router.back()">
                <Icon icon="fluent:chevron-left-12-filled" width="27px" />
            </a>
            <Icon icon="fluent:collections-20-filled" width="24px" />
            <span class="ml-2">録画シリーズ</span>
        </h2>
        <div class="settings__description">
            録画のタイトル・概要からシリーズを判定し、同じ作品の録画をまとめます。<br>
            この設定と判定結果はすべてのユーザーと端末で共有されます。管理者だけが変更できます。<br>
        </div>

        <v-progress-linear v-if="is_loading" class="mt-5" color="primary" indeterminate rounded />

        <div v-else-if="authorization_error !== null" class="recorded-series-access-state">
            <Icon icon="fluent:shield-error-20-filled" width="28px" />
            <span v-if="authorization_error === 'AdminRequired'">この設定を表示するには管理者権限が必要です。</span>
            <span v-else>ユーザー情報を取得できませんでした。ページを再読み込みしてください。</span>
        </div>

        <template v-else>
            <div class="settings__content"
                :class="{'settings__content--disabled': is_disabled || is_settings_action_running}"
                :inert="is_settings_action_running">
            <div class="settings__content-heading">
                <Icon icon="fluent:wand-20-filled" width="22px" />
                <span class="ml-2">自動判定</span>
            </div>
            <div class="settings__item settings__item--switch">
                <label class="settings__item-heading" for="recorded_series_enabled">新しい録画を自動でシリーズ判定する</label>
                <label class="settings__item-label" for="recorded_series_enabled">
                    有効にすると、新しい録画の登録後にローカル情報からシリーズを自動判定します。<br>
                    無効にしても保存済みのシリーズ情報は維持され、下のボタンから既存録画を手動判定できます。<br>
                </label>
                <v-switch id="recorded_series_enabled" class="settings__item-switch" color="primary" hide-details
                    v-model="settings.enabled" />
            </div>
            <div class="settings__item settings__item--switch">
                <label class="settings__item-heading" for="recorded_series_ai_enabled">
                    AI でシリーズ名・話数・話名を生成する
                </label>
                <label class="settings__item-label" for="recorded_series_ai_enabled">
                    有効時、Manual / Rule 以外は AI が作品名・シーズン話数・話名を一括生成します。<br>
                    既存シリーズがある場合は、サーバーが ID・Wikipedia・正規化キー・類似度で合流します。<br>
                    既存 Series の表示名は AI では書き換えません。Wikipedia と既存シリーズは参考情報として渡します。<br>
                    無効時は Wikipedia と AI へ接続せず、従来のローカル情報・EPG 経路で判定します。<br>
                    API キーと保存済みの判定結果は削除されません。<br>
                </label>
                <v-switch id="recorded_series_ai_enabled" class="settings__item-switch" color="primary" hide-details
                    v-model="settings.ai_enabled" />
            </div>
            <div class="recorded-series-ai-options"
                :class="{'recorded-series-ai-options--disabled': settings.ai_enabled === false}"
                :inert="settings.ai_enabled === false">
                <div class="settings__item settings__item--switch">
                    <label class="settings__item-heading" for="recorded_series_ai_episode_number_search_enabled">
                        <span>話数不明時の Web 検索に使う</span>
                        <span class="recorded-series-beta-badge">BETA</span>
                    </label>
                    <label class="settings__item-label" for="recorded_series_ai_episode_number_search_enabled">
                        Series が確定していて話数だけ不明な録画を、AI の Web 検索で判定します。<br>
                        既存録画は自動検索せず、下の一括話数判定または録画シリーズ管理から明示的に検索できます。<br>
                        利用前に、選択したバックエンドの「話数 Web 検索」接続テストを実行してください。<br>
                    </label>
                    <v-switch id="recorded_series_ai_episode_number_search_enabled" class="settings__item-switch"
                        color="primary" hide-details :model-value="settings.ai_episode_number_search_enabled"
                        @update:model-value="updateEpisodeNumberSearchEnabled" />
                </div>
                <div class="settings__item"
                    :class="{'recorded-series-ai-options--disabled': settings.ai_episode_number_search_enabled === false}"
                    :inert="settings.ai_episode_number_search_enabled === false">
                    <div class="settings__item-heading">Web 検索結果の受理条件</div>
                    <div class="settings__item-label">
                        「高信頼度のみ」は引用または Web 検索元があり、信頼度 80% 以上の結果だけを確定します。<br>
                        「常に受理」は Web 検索を実行して得た有効な数値を、信頼度にかかわらず確定します。<br>
                    </div>
                    <v-select class="settings__item-form" color="primary" variant="outlined"
                        :density="is_form_dense ? 'compact' : 'default'"
                        :items="episode_acceptance_modes" item-title="title" item-value="value"
                        v-model="settings.ai_episode_number_acceptance_mode" />
                </div>
            </div>

            <div class="settings__content-heading mt-7">
                <Icon icon="fluent:bot-20-filled" width="22px" />
                <span class="ml-2">AI バックエンド</span>
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">バックエンド</div>
                <div class="settings__item-label">
                    AI によるシリーズ情報生成と話数検索に使用するバックエンドを選択します。<br>
                    OpenAI 互換 API は Chat Completions / Responses を使用し、ACP はホスト上の CLI を起動します。<br>
                    シリーズ情報生成と話数 Web 検索は必要な能力が異なるため、それぞれ個別に接続テストしてください。<br>
                    任意コマンド型の AcpCustom は、安全な固定実行契約と能力証明がないため現在は利用できません。<br>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :items="ai_backend_options" item-title="title" item-value="value"
                    v-model="settings.ai_backend" />
            </div>

            <!-- OpenAI 互換 API 設定 -->
            <template v-if="settings.ai_backend === 'OpenAICompatible'">
            <div class="settings__item">
                <div class="settings__item-heading">API のベース URL</div>
                <div class="settings__item-label">
                    OpenAI または OpenAI 互換サービスの API URL を指定します。末尾のスラッシュはどちらでも構いません。<br>
                    インターネット上のサービスには HTTPS を使用してください。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :error-messages="api_base_url_error"
                    placeholder="https://api.openai.com/v1"
                    spellcheck="false" autocapitalize="off"
                    v-model="settings.api_base_url" />
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">モデル</div>
                <div class="settings__item-label">
                    推奨候補から選ぶか、利用する互換サービスのモデル ID を直接入力してください。<br>
                </div>
                <v-combobox class="settings__item-form" color="primary" variant="outlined"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :items="model_presets"
                    :error-messages="model_error"
                    hide-no-data spellcheck="false" autocapitalize="off"
                    v-model="settings.model" />
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">1 日の AI API リクエスト上限</div>
                <div class="settings__item-label">
                    想定外の大量呼び出しを防ぐ上限です。0 を指定すると無制限になります。<br>
                    Wikipedia の検索と AI を使わない判定は数えません。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" type="number"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :min="0" :max="1000" :step="1"
                    :error-messages="daily_ai_request_limit_error"
                    v-model.number="settings.daily_ai_request_limit" />
            </div>
            <div class="settings__item">
                <div class="settings__item-heading">API キー</div>
                <div class="settings__item-label">
                    保存済み設定 URL（{{saved_api_base_url}}）用のキー:
                    <strong>{{settings.api_key_configured ? '設定済み' : '未設定'}}</strong><br>
                    <template v-if="is_saved_api_base_url_draft === false">
                        入力中の URL は未保存のため、保存済みキーの有無は接続テストまたは保存後に確認されます。<br>
                    </template>
                    保存済みのキーはブラウザへ返しません。新しいキーを入力した場合だけサーバー上のキーを置き換えます。<br>
                    認証不要のローカル API を利用する場合は空欄のまま設定できます。<br>
                    ベース URL ごとにキーが保存されるため、過去に設定した URL へ戻すだけでキーが自動選択されます。<br>
                    入力欄を空のまま保存しても、保存済みのキーは維持されます。<br>
                    キーを削除するには「保存済みキーを削除」ボタンを使用してください。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :type="api_key_showing ? 'text' : 'password'"
                    :append-inner-icon="api_key_showing ? 'mdi-eye' : 'mdi-eye-off'"
                    :error-messages="api_key_error"
                    placeholder="新しい API キー"
                    autocomplete="new-password" spellcheck="false" autocapitalize="off"
                    v-model="api_key_input"
                    @click:appendInner="api_key_showing = !api_key_showing" />
                <div class="recorded-series-actions mt-3">
                    <v-btn class="settings__save-button" color="background-lighten-2" variant="flat"
                        :loading="testing_connection_capability === 'CandidateSelection'"
                        :disabled="has_connection_validation_error || testing_connection_capability !== null"
                        @click="testConnection('CandidateSelection')">
                        <Icon icon="fluent:plug-connected-checkmark-20-filled" class="mr-2" width="21px" />シリーズ生成をテスト
                    </v-btn>
                    <v-btn class="settings__save-button" color="background-lighten-2" variant="flat"
                        :loading="testing_connection_capability === 'EpisodeLookup'"
                        :disabled="has_connection_validation_error || testing_connection_capability !== null"
                        @click="testConnection('EpisodeLookup')">
                        <Icon icon="fluent:globe-search-20-filled" class="mr-2" width="21px" />話数 Web 検索をテスト
                    </v-btn>
                    <v-btn v-if="settings.api_key_configured"
                        class="settings__save-button" color="error" variant="flat"
                        :loading="is_deleting_api_key" @click="api_key_delete_dialog = true">
                        <Icon icon="fluent:key-reset-20-filled" class="mr-2" width="21px" />保存済みキーを削除
                    </v-btn>
                </div>
                <div class="settings__item-label mt-2">
                    シリーズ生成は Chat Completions、話数検索は Responses API と Web Search の能力を個別に確認します。<br>
                    本番と同じ API へ最小リクエストを送るため、少量の API 利用が発生します。入力内容は保存されません。<br>
                </div>
            </div>
            </template>

            <!-- ACP 設定 -->
            <template v-if="settings.ai_backend !== 'OpenAICompatible'">
            <!-- モデル名と推論深さは分離。ACP ごとに候補と初期値が異なる -->
            <div class="settings__item">
                <div class="settings__item-heading">モデル</div>
                <div class="settings__item-label">
                    <template v-if="settings.ai_backend === 'AcpGrok'">
                        Grok Build の ACP モデルは <code>grok-4.5</code> 固定です。<br>
                    </template>
                    <template v-else-if="settings.ai_backend === 'AcpCodex'">
                        Codex のモデル系統を選びます。深さは下の「推論の深さ」で別指定します。<br>
                        初期値は <strong>GPT-5.6 Luna</strong> です。<br>
                    </template>
                    <template v-else>
                        Gemini CLI のモデル ID を選びます。<br>
                        Gemini CLI 0.52.0 の ACP では推論の深さを個別指定できないため、モデル側の既定を使います。<br>
                        初期値は <strong>gemini-3.6-flash</strong> です。<br>
                    </template>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :items="acp_model_preset_items"
                    item-title="title"
                    item-value="value"
                    :disabled="settings.ai_backend === 'AcpGrok'"
                    :error-messages="acp_model_error"
                    v-model="acp_model_selection" />
            </div>
            <div class="settings__item" v-if="settings.ai_backend !== 'AcpGemini'">
                <div class="settings__item-heading">推論の深さ</div>
                <div class="settings__item-label">
                    モデル名とは別に、思考の深さを選びます。<br>
                    <template v-if="settings.ai_backend === 'AcpCodex'">
                        Codex は Low〜Ultra。初期値は <strong>Medium</strong> です。<br>
                    </template>
                    <template v-else>
                        Grok は Low / Medium / High。初期値は <strong>High</strong> です。<br>
                    </template>
                </div>
                <v-select class="settings__item-form" color="primary" variant="outlined"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :items="acp_reasoning_effort_options"
                    item-title="title"
                    item-value="value"
                    v-model="acp_reasoning_effort_selection" />
                <div class="settings__item-label mt-2" v-if="acp_wire_preview">
                    適用プレビュー: <code>{{acp_wire_preview}}</code>
                </div>
            </div>
            <!-- ACP タイムアウト -->
            <div class="settings__item">
                <div class="settings__item-heading">ACP タイムアウト（秒）</div>
                <div class="settings__item-label">
                    シリーズ生成と話数 Web 検索の最大実行時間です。30〜600 秒の間で指定してください。<br>
                </div>
                <v-text-field class="settings__item-form" color="primary" variant="outlined" type="number"
                    :density="is_form_dense ? 'compact' : 'default'"
                    :min="30" :max="600" :step="1"
                    :error-messages="acp_timeout_error"
                    v-model.number="settings.acp_timeout_sec" />
            </div>

            <!-- Codex / Grok の明示取り込みと Gemini ADC の固定 mount 状態 -->
            <div class="settings__item" v-if="settings.ai_backend === 'AcpCodex'">
                <div class="settings__item-heading">Codex 認証</div>
                <div class="settings__item-label">
                    ホストで <code>codex login</code> を実行し、Compose override に auth.json の絶対パスを指定します。<br>
                    OS keyring だけを利用している場合は <code>cli_auth_credentials_store = "file"</code> を設定してください。<br>
                </div>
                <div class="recorded-series-auth-status mt-3">
                    <span>
                        ホスト認証ファイル:
                        <strong :class="{'recorded-series-auth-status--ok':
                            acp_credential_status?.codex_host_auth_available}">
                            {{acp_credential_status?.codex_host_auth_available ? '検出済み' : '未検出'}}
                        </strong>
                    </span>
                    <span>
                        KonomiTV-BS4K への取り込み:
                        <strong :class="{'recorded-series-auth-status--ok':
                            acp_credential_status?.codex_auth_imported}">
                            {{acp_credential_status?.codex_auth_imported ? '取り込み済み' : '未取り込み'}}
                        </strong>
                    </span>
                    <small v-if="acp_credential_status?.codex_auth_imported_at">
                        最終取り込み: {{formatACPAuthImportedAt(acp_credential_status.codex_auth_imported_at)}}
                    </small>
                </div>
                <div class="recorded-series-actions mt-3">
                    <v-btn class="settings__save-button" color="background-lighten-2" variant="flat"
                        :disabled="acp_credential_status?.codex_host_auth_available !== true"
                        :loading="is_updating_acp_authentication"
                        @click="openACPAuthenticationDialog('codex', 'Import')">
                        <Icon icon="fluent:key-20-filled" class="mr-2" width="21px" />
                        {{acp_credential_status?.codex_auth_imported ? '認証を再取り込み' : '認証を取り込む'}}
                    </v-btn>
                    <v-btn class="settings__save-button" color="error" variant="flat"
                        :disabled="acp_credential_status?.codex_auth_imported !== true"
                        :loading="is_updating_acp_authentication"
                        @click="openACPAuthenticationDialog('codex', 'Delete')">
                        <Icon icon="fluent:key-reset-20-filled" class="mr-2" width="21px" />取り込んだ認証を削除
                    </v-btn>
                </div>
            </div>
            <div class="settings__item" v-if="settings.ai_backend === 'AcpGrok'">
                <div class="settings__item-heading">Grok Build 認証</div>
                <div class="settings__item-label">
                    ホストで <code>grok login</code> を実行します。ブラウザーを使えない場合は
                    <code>grok login --device-auth</code> を使用できます。<br>
                    Compose override には生成された auth.json の絶対パスを指定します。<br>
                </div>
                <div class="recorded-series-auth-status mt-3">
                    <span>
                        ホスト認証ファイル:
                        <strong :class="{'recorded-series-auth-status--ok':
                            acp_credential_status?.grok_host_auth_available}">
                            {{acp_credential_status?.grok_host_auth_available ? '検出済み' : '未検出'}}
                        </strong>
                    </span>
                    <span>
                        KonomiTV-BS4K への取り込み:
                        <strong :class="{'recorded-series-auth-status--ok':
                            acp_credential_status?.grok_auth_imported}">
                            {{acp_credential_status?.grok_auth_imported ? '取り込み済み' : '未取り込み'}}
                        </strong>
                    </span>
                    <small v-if="acp_credential_status?.grok_auth_imported_at">
                        最終取り込み: {{formatACPAuthImportedAt(acp_credential_status.grok_auth_imported_at)}}
                    </small>
                </div>
                <div class="recorded-series-actions mt-3">
                    <v-btn class="settings__save-button" color="background-lighten-2" variant="flat"
                        :disabled="acp_credential_status?.grok_host_auth_available !== true"
                        :loading="is_updating_acp_authentication"
                        @click="openACPAuthenticationDialog('grok', 'Import')">
                        <Icon icon="fluent:key-20-filled" class="mr-2" width="21px" />
                        {{acp_credential_status?.grok_auth_imported ? '認証を再取り込み' : '認証を取り込む'}}
                    </v-btn>
                    <v-btn class="settings__save-button" color="error" variant="flat"
                        :disabled="acp_credential_status?.grok_auth_imported !== true"
                        :loading="is_updating_acp_authentication"
                        @click="openACPAuthenticationDialog('grok', 'Delete')">
                        <Icon icon="fluent:key-reset-20-filled" class="mr-2" width="21px" />取り込んだ認証を削除
                    </v-btn>
                </div>
            </div>
            <template v-if="settings.ai_backend === 'AcpGemini'">
                <div class="settings__item">
                    <div class="settings__item-heading">Google ADC</div>
                    <div class="settings__item-label">
                        Google Cloud CLI はホストだけにインストールし、
                        <code>gcloud auth application-default login</code> を実行します。<br>
                        ADC は Compose override から読み取り専用で直接参照し、KonomiTV-BS4K へコピーしません。<br>
                    </div>
                    <div class="recorded-series-auth-status mt-3">
                        <span>
                            Google ADC:
                            <strong :class="{'recorded-series-auth-status--ok':
                                acp_credential_status?.google_adc_available}">
                                {{acp_credential_status?.google_adc_available ? '検出済み・読取り可能' : '未検出'}}
                            </strong>
                        </span>
                    </div>
                </div>
                <div class="settings__item">
                    <div class="settings__item-heading">Google Cloud プロジェクト ID</div>
                    <v-text-field class="settings__item-form" color="primary" variant="outlined"
                        :density="is_form_dense ? 'compact' : 'default'"
                        :error-messages="google_cloud_project_error"
                        placeholder="my-google-cloud-project"
                        spellcheck="false" autocapitalize="off"
                        v-model="settings.google_cloud_project" />
                </div>
                <div class="settings__item">
                    <div class="settings__item-heading">Google Cloud リージョン</div>
                    <v-text-field class="settings__item-form" color="primary" variant="outlined"
                        :density="is_form_dense ? 'compact' : 'default'"
                        :error-messages="google_cloud_location_error"
                        placeholder="asia-northeast1"
                        spellcheck="false" autocapitalize="off"
                        v-model="settings.google_cloud_location" />
                </div>
            </template>

            <v-alert class="mb-4" color="warning" variant="tonal">
                この認証は録画シリーズのバックグラウンド AI 処理全体で共有する管理者資格情報です。
                KonomiTV-BS4K の一般ユーザーごとの認証ではありません。<br>
                ホストで再ログインしても自動反映されないため、Codex / Grok は再取り込みが必要です。
                ホストで logout しても取り込み済みコピーは自動削除されません。直ちに無効化する場合は、
                この画面でコピーを削除し、プロバイダー側でもセッションを失効してください。
            </v-alert>

            <!-- ACP 接続試験 -->
            <div class="settings__item">
                <div class="recorded-series-actions mt-3">
                    <v-btn class="settings__save-button" color="background-lighten-2" variant="flat"
                        :loading="testing_connection_capability === 'CandidateSelection'"
                        :disabled="has_connection_validation_error || testing_connection_capability !== null"
                        @click="testConnection('CandidateSelection')">
                        <Icon icon="fluent:plug-connected-checkmark-20-filled" class="mr-2" width="21px" />シリーズ生成をテスト
                    </v-btn>
                    <v-btn class="settings__save-button" color="background-lighten-2" variant="flat"
                        :loading="testing_connection_capability === 'EpisodeLookup'"
                        :disabled="has_connection_validation_error || testing_connection_capability !== null"
                        @click="testConnection('EpisodeLookup')">
                        <Icon icon="fluent:globe-search-20-filled" class="mr-2" width="21px" />話数 Web 検索をテスト
                    </v-btn>
                </div>
                <div class="settings__item-label mt-2">
                    話数検索の接続テストでは、Web 検索の実行・出典取得・構造化結果を一体で確認します。<br>
                    シリーズ生成だけ成功した場合でも、話数検索を利用できるとは限りません。<br>
                </div>
            </div>
            </template>

            <!-- 接続試験結果（共通） -->
            <div v-if="has_connection_test_result" class="recorded-series-connection-results mt-3">
                <template v-for="capability in connection_test_capabilities" :key="capability.value">
                    <div v-if="connection_test_results[capability.value] !== null"
                        class="recorded-series-connection-result"
                        :class="{'recorded-series-connection-result--error':
                            connection_test_results[capability.value]?.success === false}">
                        <Icon :icon="connection_test_results[capability.value]?.success ?
                            'fluent:checkmark-circle-20-filled' : 'fluent:error-circle-20-filled'" width="21px" />
                        <div>
                            <strong>{{capability.title}}</strong>
                            <span>{{connection_test_results[capability.value]?.message}}</span>
                            <small>
                                {{connection_test_results[capability.value]?.model}} /
                                {{connection_test_results[capability.value]?.latency_ms.toLocaleString()}} ms
                            </small>
                            <ul v-if="connection_test_results[capability.value]?.checks !== null"
                                class="recorded-series-connection-checks">
                                <li v-for="check in episode_lookup_connection_checks" :key="check.value"
                                    :class="`recorded-series-connection-checks--${
                                        connectionCheck(connection_test_results[capability.value], check.value)?.status ?? 'NotRun'
                                    }`">
                                    <Icon :icon="connectionCheckIcon(
                                        connectionCheck(connection_test_results[capability.value], check.value)?.status,
                                    )" width="16px" />
                                    <span>
                                        <b>{{check.title}}</b>:
                                        {{connectionCheckStatusLabel(
                                            connectionCheck(connection_test_results[capability.value], check.value)?.status,
                                        )}} —
                                        {{connectionCheck(connection_test_results[capability.value], check.value)?.message}}
                                    </span>
                                </li>
                            </ul>
                        </div>
                    </div>
                </template>
            </div>
            <v-btn class="settings__save-button bg-secondary mt-6" variant="flat"
                :loading="is_saving"
                :disabled="has_settings_validation_error"
                @click="saveSettings()">
                <Icon icon="fluent:save-20-filled" class="mr-2" width="22px" />録画シリーズ設定を更新
            </v-btn>

            <div class="settings__content-heading mt-8">
                <Icon icon="fluent:data-usage-20-filled" width="22px" />
                <span class="ml-2">判定状況</span>
            </div>
            <template v-if="status !== null">
                <div class="settings__item">
                    <div class="settings__item-heading">シリーズ判定</div>
                    <div class="recorded-series-status-grid mt-3">
                        <div class="recorded-series-status-card">
                            <span>録画総数</span><strong>{{status.total.toLocaleString()}}</strong>
                        </div>
                        <div class="recorded-series-status-card">
                            <span>シリーズ確定</span><strong>{{status.resolved.toLocaleString()}}</strong>
                        </div>
                        <div class="recorded-series-status-card">
                            <span>単発番組</span><strong>{{status.not_series.toLocaleString()}}</strong>
                        </div>
                        <div class="recorded-series-status-card">
                            <span>未判定</span><strong>{{status.pending.toLocaleString()}}</strong>
                        </div>
                        <div class="recorded-series-status-card">
                            <span>要確認</span><strong>{{status.needs_review.toLocaleString()}}</strong>
                        </div>
                        <div class="recorded-series-status-card"
                            :class="{'recorded-series-status-card--error': status.failed > 0}">
                            <span>失敗</span><strong>{{status.failed.toLocaleString()}}</strong>
                        </div>
                    </div>
                </div>
                <div class="settings__item">
                    <div class="settings__item-heading">話数判定</div>
                    <div class="recorded-series-status-grid mt-3">
                        <div class="recorded-series-status-card">
                            <span>話数確定</span><strong>{{status.episode_resolved.toLocaleString()}}</strong>
                        </div>
                        <div class="recorded-series-status-card">
                            <span>話数不明</span><strong>{{status.episode_unknown.toLocaleString()}}</strong>
                        </div>
                        <div class="recorded-series-status-card">
                            <span>公式話数なし</span><strong>{{status.episode_not_numbered.toLocaleString()}}</strong>
                        </div>
                        <div class="recorded-series-status-card">
                            <span>要確認</span><strong>{{status.episode_needs_review.toLocaleString()}}</strong>
                        </div>
                        <div class="recorded-series-status-card"
                            :class="{'recorded-series-status-card--error': status.episode_failed > 0}">
                            <span>失敗</span><strong>{{status.episode_failed.toLocaleString()}}</strong>
                        </div>
                    </div>
                </div>
                <div class="settings__item">
                    <div class="settings__item-heading">本日の AI API 利用</div>
                    <div class="settings__item-label">
                        {{status.ai_requests_today.toLocaleString()}} /
                        {{settings.daily_ai_request_limit === 0 ? '無制限' : settings.daily_ai_request_limit.toLocaleString()}} リクエスト<br>
                        内訳: シリーズ生成 {{status.series_ai_requests_today.toLocaleString()}}・
                        話数検索 {{status.episode_ai_requests_today.toLocaleString()}}<br>
                        シリーズ判定の最終実行: {{formatLastRunAt(status.last_run_at)}}<br>
                        話数判定の最終実行: {{formatLastRunAt(status.episode_last_run_at)}}<br>
                    </div>
                </div>
            </template>
            <div v-else-if="is_loading === false" class="settings__item-label mt-5">
                判定状況を取得できませんでした。
            </div>

            <div class="settings__item">
                <div class="settings__item-heading">既存録画をシリーズ判定</div>
                <div class="settings__item-label">
                    未判定の録画を対象に、ローカル照合から順にシリーズを判定します。確定済みの結果は再利用します。<br>
                    現在サーバーに保存されている設定を使用し、AI 生成が有効な場合は Manual / Rule 以外を生成します。<br>
                </div>
            </div>
            <div class="settings__item settings__item--switch">
                <label class="settings__item-heading" for="recorded_series_force_backfill">確定済みも再判定する</label>
                <label class="settings__item-label" for="recorded_series_force_backfill">
                    有効にすると、シリーズ確定・単発番組を含むすべての録画を最新の設定で再判定します。<br>
                    既存の判定結果が更新され、AI API の利用が再度発生する場合があります。<br>
                    新しい確定結果を得られない場合は、最後に確定したシリーズ分けを保持します。<br>
                </label>
                <v-switch id="recorded_series_force_backfill" class="settings__item-switch" color="primary" hide-details
                    :disabled="is_backfill_running || is_episode_backfill_running"
                    v-model="force_backfill" />
            </div>
            <div class="settings__item">
                <v-progress-linear v-if="is_backfill_running" class="mt-4" color="primary" height="7" rounded
                    :indeterminate="backfill_progress === null"
                    :model-value="backfill_progress ?? undefined" />
                <div v-if="backfill_task !== null" class="settings__item-label mt-2">
                    {{stageLabel(backfill_task.stage)}}
                    <template v-if="backfill_progress !== null">・{{backfill_progress.toFixed(0)}}%</template>
                </div>
                <v-btn class="settings__save-button mt-4" color="background-lighten-2" variant="flat"
                    :loading="is_starting_backfill"
                    :disabled="is_backfill_running || is_episode_backfill_running"
                    @click="startBackfill()">
                    <Icon icon="fluent:arrow-sync-20-filled" class="mr-2" width="22px" />
                    {{force_backfill ? 'すべての録画を再判定' : '既存録画の判定を開始'}}
                </v-btn>
            </div>

            <v-divider class="mt-7" />
            <div class="settings__item">
                <div class="settings__item-heading">既存録画の一括話数判定</div>
                <div class="settings__item-label">
                    Series 所属済みで、話数が未処理・ローカル判定で不明・移行データで要確認の既存録画を、<br>
                    保存済みの AI 設定で順番に Web 検索します。<br>
                    手動で訂正した話数は変更せず、1日の AI API リクエスト上限に達した時点で残りを保留します。<br>
                </div>
            </div>
            <div class="settings__item settings__item--switch">
                <label class="settings__item-heading" for="recorded_series_force_episode_backfill">
                    判定済みの話数も再検索する
                </label>
                <label class="settings__item-label" for="recorded_series_force_episode_backfill">
                    有効にすると、AI・ローカル情報・EPG・移行データで確定済みの話数も最新の設定で再検索します。<br>
                    公式話数なし・要確認・失敗の結果も再検索しますが、手動で訂正した話数は対象外です。<br>
                </label>
                <v-switch id="recorded_series_force_episode_backfill" class="settings__item-switch"
                    color="primary" hide-details
                    :disabled="is_episode_backfill_running || is_backfill_running"
                    v-model="force_episode_backfill" />
            </div>
            <div class="settings__item">
                <v-progress-linear v-if="is_episode_backfill_running" class="mt-4" color="primary" height="7" rounded
                    :indeterminate="episode_backfill_progress === null"
                    :model-value="episode_backfill_progress ?? undefined" />
                <div v-if="episode_backfill_task !== null" class="settings__item-label mt-2">
                    {{stageLabel(episode_backfill_task.stage)}}
                    <template v-if="episode_backfill_progress !== null">
                        ・{{episode_backfill_progress.toFixed(0)}}%
                    </template>
                </div>
                <v-btn class="settings__save-button mt-4" color="background-lighten-2" variant="flat"
                    :loading="is_starting_episode_backfill"
                    :disabled="is_episode_backfill_running || is_backfill_running"
                    @click="startEpisodeBackfill()">
                    <Icon icon="fluent:globe-search-20-filled" class="mr-2" width="22px" />
                    {{force_episode_backfill ? '判定済みを含めて話数を再検索' : '既存録画の話数判定を開始'}}
                </v-btn>
            </div>

            </div>

            <div class="settings__content">
                <v-divider class="mt-7"></v-divider>
                <div class="settings__item">
                    <div class="settings__item-heading">録画シリーズ管理</div>
                    <div class="settings__item-label">
                        判定済みのシリーズを検索し、表示するタイトル・説明と、録画ごとのシーズン・話数を編集できます。<br>
                        録画ごとの所属先は、各録画の再生画面にある「シリーズを訂正」から変更できます。<br>
                    </div>
                    <v-btn class="settings__save-button mt-4" variant="flat"
                        to="/settings/server/recorded-series/series">
                        <Icon icon="fluent:collections-20-filled" class="mr-2" width="22px" />録画シリーズ管理を開く
                    </v-btn>
                </div>
            </div>
        </template>

        <v-dialog v-model="api_key_delete_dialog" max-width="480">
            <v-card class="recorded-series-dialog">
                <v-card-title>保存済み API キーを削除</v-card-title>
                <v-card-text>
                    保存済み設定 URL「{{saved_api_base_url}}」に紐付いた API キーだけを削除します。<br>
                    入力中の未保存 URL や、他の URL のキーは変更しません。
                </v-card-text>
                <v-card-actions>
                    <v-spacer />
                    <v-btn variant="text" @click="api_key_delete_dialog = false">キャンセル</v-btn>
                    <v-btn color="error" variant="flat" :loading="is_deleting_api_key" @click="deleteAPIKey()">削除</v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>
        <v-dialog v-model="acp_authentication_dialog" max-width="560">
            <v-card class="recorded-series-dialog">
                <v-card-title>
                    {{pending_acp_authentication_action?.action === 'Delete' ?
                        '取り込んだ ACP 認証を削除' :
                        'ホストの ACP 認証を取り込む'}}
                </v-card-title>
                <v-card-text v-if="pending_acp_authentication_action !== null">
                    <template v-if="pending_acp_authentication_action.action === 'Import'">
                        Compose で読み取り専用 mount した
                        {{pending_acp_authentication_action.provider === 'codex' ? 'Codex' : 'Grok Build'}}
                        の auth.json を、KonomiTV-BS4K 専用プロファイルへ取り込みます。<br>
                        既存の取り込み済みコピーがある場合は atomic に置き換えます。続行しますか？
                    </template>
                    <template v-else>
                        KonomiTV-BS4K 専用プロファイルの
                        {{pending_acp_authentication_action.provider === 'codex' ? 'Codex' : 'Grok Build'}}
                        auth.json コピーだけを削除します。ホスト側ファイルは変更しません。続行しますか？
                    </template>
                </v-card-text>
                <v-card-actions>
                    <v-spacer />
                    <v-btn variant="text" :disabled="is_updating_acp_authentication"
                        @click="closeACPAuthenticationDialog()">キャンセル</v-btn>
                    <v-btn :color="pending_acp_authentication_action?.action === 'Delete' ? 'error' : 'primary'"
                        variant="flat" :loading="is_updating_acp_authentication"
                        @click="confirmACPAuthenticationAction()">
                        {{pending_acp_authentication_action?.action === 'Delete' ? '削除' : '取り込む'}}
                    </v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>
        <v-dialog v-model="episode_number_search_warning_dialog" max-width="560">
            <v-card class="recorded-series-dialog">
                <v-card-title>話数 Web 検索（BETA）を有効化</v-card-title>
                <v-card-text>
                    <v-alert class="mb-4" color="warning" variant="tonal">
                        この機能は未成熟な BETA オプションのため、使用を推奨しません。
                    </v-alert>
                    Web 検索結果や AI の判定が誤っていても、受理条件によっては誤った話数を確定する可能性があります。<br>
                    内容を理解した上で、それでも利用する場合だけ有効にしてください。
                </v-card-text>
                <v-card-actions>
                    <v-spacer />
                    <v-btn variant="text" @click="episode_number_search_warning_dialog = false">キャンセル</v-btn>
                    <v-btn color="warning" variant="flat" @click="confirmEpisodeNumberSearchEnabled()">
                        それでも有効にする
                    </v-btn>
                </v-card-actions>
            </v-card>
        </v-dialog>
    </SettingsBase>
</template>

<script setup lang="ts">

import { computed, onMounted, onUnmounted, ref, watch } from 'vue';

import Message from '@/message';
import AnalysisTasks, { type IAnalysisTaskExecution } from '@/services/AnalysisTasks';
import RecordedSeries, {
    type AcpReasoningEffort,
    type AIBackendKind,
    type IKonomiTVBS4KACPCredentialStatus,
    type IRecordedSeriesConnectionTestCheck,
    type IRecordedSeriesConnectionTestRequest,
    type IRecordedSeriesConnectionTestResult,
    type IRecordedSeriesSettings,
    type IRecordedSeriesSettingsUpdate,
    type IRecordedSeriesStatus,
    type KonomiTVBS4KACPImportProvider,
    type RecordedEpisodeNumberAcceptanceMode,
    type RecordedSeriesConnectionTestCheckStatus,
    type RecordedSeriesConnectionTestCapability,
    type RecordedSeriesEpisodeLookupConnectionCheckName,
} from '@/services/RecordedSeries';
import { stageLabel } from '@/stores/AnalysisTasksStore';
import useUserStore from '@/stores/UserStore';
import Utils, { dayjs } from '@/utils';
import SettingsBase from '@/views/Settings/Base.vue';


const model_presets = [
    'gpt-5.6-luna',
    'gpt-5.4-nano',
    'gpt-5-nano',
];
/** バックエンド別のモデル候補（角括弧なし）。Grok は 4.5 固定表示。 */
const acp_model_presets_by_backend: Record<string, {title: string; value: string;}[]> = {
    AcpCodex: [
        {title: 'GPT-5.6 Luna', value: 'gpt-5.6-luna'},
        {title: 'GPT-5.6 Terra', value: 'gpt-5.6-terra'},
        {title: 'GPT-5.6 Sol', value: 'gpt-5.6-sol'},
        {title: 'GPT-5.5', value: 'gpt-5.5'},
        {title: 'GPT-5.4', value: 'gpt-5.4'},
        {title: 'GPT-5.4 Mini', value: 'gpt-5.4-mini'},
        {title: 'GPT-5.3 Codex Spark', value: 'gpt-5.3-codex-spark'},
    ],
    AcpGrok: [
        {title: 'Grok 4.5（固定）', value: 'grok-4.5'},
    ],
    AcpGemini: [
        {title: 'Gemini 3.6 Flash', value: 'gemini-3.6-flash'},
        {title: 'Gemini 3.5 Flash', value: 'gemini-3.5-flash'},
        {title: 'Gemini 2.5 Flash', value: 'gemini-2.5-flash'},
        {title: 'Gemini 2.5 Pro', value: 'gemini-2.5-pro'},
        {title: 'Gemini 3.1 Pro Preview', value: 'gemini-3.1-pro-preview'},
        {title: 'auto', value: 'auto'},
    ],
};
/** backend 別の推論深さ候補。 */
const acp_reasoning_effort_presets_by_backend: Record<string, {title: string; value: AcpReasoningEffort;}[]> = {
    AcpCodex: [
        {title: 'Low（速い）', value: 'Low'},
        {title: 'Medium', value: 'Medium'},
        {title: 'High（深い）', value: 'High'},
        {title: 'XHigh', value: 'XHigh'},
        {title: 'Max', value: 'Max'},
        {title: 'Ultra', value: 'Ultra'},
    ],
    AcpGrok: [
        {title: 'Low（速い）', value: 'Low'},
        {title: 'Medium', value: 'Medium'},
        {title: 'High（深い）', value: 'High'},
    ],
};
/** backend 初回選択時のデフォルト。 */
const acp_defaults_by_backend: Record<string, {model: string | null; effort: AcpReasoningEffort | null;}> = {
    AcpCodex: {model: 'gpt-5.6-luna', effort: 'Medium'},
    AcpGrok: {model: null, effort: 'High'},
    AcpGemini: {model: 'gemini-3.6-flash', effort: null},
};
const ai_backend_options: {title: string; value: AIBackendKind;}[] = [
    {title: 'OpenAI 互換 API', value: 'OpenAICompatible'},
    {title: 'ACP / Codex', value: 'AcpCodex'},
    {title: 'ACP / Grok Build', value: 'AcpGrok'},
    {title: 'ACP / Gemini CLI', value: 'AcpGemini'},
];
const episode_acceptance_modes: {title: string; value: RecordedEpisodeNumberAcceptanceMode;}[] = [
    {title: '高信頼度の結果のみ受理', value: 'HighConfidenceOnly'},
    {title: '有効な数値なら常に受理', value: 'Always'},
];
const connection_test_capabilities: {title: string; value: RecordedSeriesConnectionTestCapability;}[] = [
    {title: 'シリーズ情報生成', value: 'CandidateSelection'},
    {title: '話数 Web 検索', value: 'EpisodeLookup'},
];
const episode_lookup_connection_checks: {
    title: string;
    value: RecordedSeriesEpisodeLookupConnectionCheckName;
}[] = [
    {title: 'バックエンド接続', value: 'backend_connection'},
    {title: 'Web 検索の実行', value: 'web_search'},
    {title: '検索元 URL', value: 'source_url'},
    {title: 'strict schema', value: 'strict_schema'},
    {title: 'timeout / cancel', value: 'timeout_cancel'},
    {title: 'permission policy', value: 'permission_policy'},
];
const connection_check_status_labels: Record<RecordedSeriesConnectionTestCheckStatus, string> = {
    Passed: '確認済み',
    Failed: '失敗',
    NotRun: '未実行',
    NotApplicable: '対象外',
};
type ConnectionTestResults = Record<RecordedSeriesConnectionTestCapability, IRecordedSeriesConnectionTestResult | null>;
type ACPAuthenticationAction = {
    provider: KonomiTVBS4KACPImportProvider;
    action: 'Import' | 'Delete';
};

// API 取得前は入力欄を操作できないため、安全側の無効値を初期値にする。
const settings = ref<IRecordedSeriesSettings>({
    enabled: true,
    ai_enabled: false,
    ai_episode_number_search_enabled: false,
    ai_episode_number_acceptance_mode: 'Always',
    daily_ai_request_limit: 20,
    ai_backend: 'OpenAICompatible',
    api_base_url: 'https://api.openai.com/v1',
    model: 'gpt-5.6-luna',
    acp_model: null,
    acp_reasoning_effort: null,
    acp_timeout_sec: 120,
    google_cloud_project: null,
    google_cloud_location: null,
    api_key_configured: false,
});
const saved_api_base_url = ref(settings.value.api_base_url);
const status = ref<IRecordedSeriesStatus | null>(null);
const acp_credential_status = ref<IKonomiTVBS4KACPCredentialStatus | null>(null);

// API キーは共有ストアへ入れず、この画面が開いている間だけローカルメモリに保持する。
const api_key_input = ref('');
const api_key_showing = ref(false);
const api_key_delete_dialog = ref(false);
const acp_authentication_dialog = ref(false);
const pending_acp_authentication_action = ref<ACPAuthenticationAction | null>(null);
const episode_number_search_warning_dialog = ref(false);

const is_loading = ref(true);
const is_disabled = ref(true);
const is_saving = ref(false);
const testing_connection_capability = ref<RecordedSeriesConnectionTestCapability | null>(null);
const connection_test_results = ref<ConnectionTestResults>({
    CandidateSelection: null,
    EpisodeLookup: null,
});
const is_deleting_api_key = ref(false);
const is_updating_acp_authentication = ref(false);
const is_starting_backfill = ref(false);
const is_monitoring_backfill = ref(false);
const is_starting_episode_backfill = ref(false);
const is_monitoring_episode_backfill = ref(false);
const is_refreshing_status = ref(false);
const authorization_error = ref<'AdminRequired' | 'UserUnavailable' | null>(null);
const backfill_task = ref<IAnalysisTaskExecution | null>(null);
const episode_backfill_task = ref<IAnalysisTaskExecution | null>(null);
const force_backfill = ref(false);
const force_episode_backfill = ref(false);

const is_form_dense = Utils.isSmartphoneHorizontal();
const user_store = useUserStore();

let backfill_abort_controller: AbortController | null = null;
let episode_backfill_abort_controller: AbortController | null = null;
let status_polling_timer: number | null = null;


/** BETA 機能は警告への明示的な同意が得られるまで有効化しない。 */
function updateEpisodeNumberSearchEnabled(enabled: boolean | null): void {
    if (enabled !== true) {
        settings.value.ai_episode_number_search_enabled = false;
        episode_number_search_warning_dialog.value = false;
        return;
    }
    if (settings.value.ai_episode_number_search_enabled === false) {
        episode_number_search_warning_dialog.value = true;
    }
}

/** BETA 警告で利用継続を選んだ場合だけ、話数 Web 検索を有効化する。 */
function confirmEpisodeNumberSearchEnabled(): void {
    settings.value.ai_episode_number_search_enabled = true;
    episode_number_search_warning_dialog.value = false;
}


/** API の URL が OpenAI 互換 API の接続先として扱える HTTP(S) URL かを確認する。 */
function isValidAPIBaseURL(value: string): boolean {
    try {
        const parsed_url = new URL(value);
        return (
            (parsed_url.protocol === 'http:' || parsed_url.protocol === 'https:') &&
            parsed_url.username === '' &&
            parsed_url.password === '' &&
            parsed_url.search === '' &&
            parsed_url.hash === ''
        );
    } catch {
        return false;
    }
}

function normalizeAPIBaseURL(value: string): string {
    return value.trim().replace(/\/+$/, '');
}

const normalized_draft_api_base_url = computed(() => normalizeAPIBaseURL(settings.value.api_base_url));
const is_saved_api_base_url_draft = computed(() =>
    normalized_draft_api_base_url.value === saved_api_base_url.value,
);
const api_base_url_error = computed(() => {
    const value = settings.value.api_base_url.trim();
    if (value === '') return 'API のベース URL を入力してください。';
    if (value.length > 2048) return 'API のベース URL は 2048 文字以内で入力してください。';
    if (isValidAPIBaseURL(value) === false) return 'ユーザー情報・クエリ・フラグメントを含まない HTTP(S) URL を入力してください。';
    return '';
});
const model_error = computed(() => {
    const value = settings.value.model.trim();
    if (value === '') return 'モデル ID を入力してください。';
    if (value.length > 255) return 'モデル ID は 255 文字以内で入力してください。';
    return '';
});
const acp_model_error = computed(() =>
    (settings.value.acp_model?.trim().length ?? 0) > 255 ? 'ACP モデル ID は 255 文字以内で入力してください。' : '',
);
/** 現在の ACP バックエンド向けモデル候補。 */
const acp_model_preset_items = computed(() => {
    return acp_model_presets_by_backend[settings.value.ai_backend]
        ?? [];
});
/** 現在の ACP バックエンド向け推論深さ候補。 */
const acp_reasoning_effort_options = computed(() => {
    return acp_reasoning_effort_presets_by_backend[settings.value.ai_backend]
        ?? [];
});
/**
 * モデル選択。Grok は保存値が null でも表示上 grok-4.5 を出す。
 */
const acp_model_selection = computed<string | null>({
    get() {
        if (settings.value.ai_backend === 'AcpGrok') {
            return 'grok-4.5';
        }
        const current = settings.value.acp_model?.trim() ?? '';
        const matched = acp_model_preset_items.value.find(item => item.value === current);
        if (matched) return matched.value;
        if (current === '') {
            // 未設定時は backend 既定の先頭候補を表示する。
            return acp_model_preset_items.value[0]?.value ?? null;
        }
        // 旧バージョンなどで保存された一覧外 ID は、そのまま文字列で返す。
        return current;
    },
    set(value) {
        if (settings.value.ai_backend === 'AcpGrok') {
            // Grok は常にモデル固定。保存は null。
            settings.value.acp_model = null;
            return;
        }
        if (value === null || value === undefined) {
            settings.value.acp_model = null;
            return;
        }
        const trimmed = value.trim();
        settings.value.acp_model = trimmed === '' ? null : trimmed;
    },
});
/** 推論深さ。未設定時は backend 既定を表示し、set で settings へ書き戻す。 */
const acp_reasoning_effort_selection = computed<AcpReasoningEffort | null>({
    get() {
        if (settings.value.acp_reasoning_effort !== null) {
            return settings.value.acp_reasoning_effort;
        }
        return acp_defaults_by_backend[settings.value.ai_backend]?.effort ?? null;
    },
    set(value) {
        settings.value.acp_reasoning_effort = value;
    },
});
/** 実際に ACP agent へ適用するモデルと推論深さ、または Grok CLI 引数を表示する。 */
const acp_wire_preview = computed(() => {
    const backend = settings.value.ai_backend;
    const effort = acp_reasoning_effort_selection.value;
    if (backend === 'AcpGrok') {
        const effort_cli = (effort ?? 'High').toLowerCase();
        return `grok --reasoning-effort ${effort_cli} agent stdio`;
    }
    let model = settings.value.acp_model?.trim() ?? '';
    if (model === '') {
        model = acp_defaults_by_backend[backend]?.model ?? '';
    }
    if (model === '') return '';
    if (backend === 'AcpCodex' && effort) {
        return `${model} / reasoning_effort=${effort.toLowerCase()}`;
    }
    return model;
});
const google_cloud_project_error = computed(() => {
    if (settings.value.ai_backend !== 'AcpGemini') return '';
    const project = settings.value.google_cloud_project?.trim() ?? '';
    if (project === '') return 'Google Cloud プロジェクト ID を入力してください。';
    return project.length <= 255 ? '' : 'Google Cloud プロジェクト ID は 255 文字以内で入力してください。';
});
const google_cloud_location_error = computed(() => {
    if (settings.value.ai_backend !== 'AcpGemini') return '';
    const location = settings.value.google_cloud_location?.trim() ?? '';
    if (location === '') return 'Google Cloud リージョンを入力してください。';
    return location.length <= 255 ? '' : 'Google Cloud リージョンは 255 文字以内で入力してください。';
});
const acp_timeout_error = computed(() => {
    const timeout = Number(settings.value.acp_timeout_sec);
    return Number.isInteger(timeout) && timeout >= 30 && timeout <= 600 ?
        '' :
        'ACP タイムアウトは 30 ～ 600 秒の整数で入力してください。';
});
const daily_ai_request_limit_error = computed(() => {
    if (String(settings.value.daily_ai_request_limit).trim() === '') return '0 ～ 1000 の整数を入力してください。';
    const value = Number(settings.value.daily_ai_request_limit);
    return Number.isInteger(value) && value >= 0 && value <= 1000 ? '' : '0 ～ 1000 の整数を入力してください。';
});
// OpenAI 互換 API はローカル運用などで認証不要の場合もあるため、API キーは必須にしない。
const api_key_length_error = computed(() =>
    api_key_input.value.length > 8192 ? 'API キーは 8192 文字以内で入力してください。' : '',
);
const api_key_error = computed(() => api_key_length_error.value);
const has_acp_validation_error = computed(() =>
    acp_model_error.value !== '' ||
    acp_timeout_error.value !== '' ||
    google_cloud_project_error.value !== '' ||
    google_cloud_location_error.value !== '',
);
const has_provider_validation_error = computed(() => settings.value.ai_backend === 'OpenAICompatible' ?
    api_base_url_error.value !== '' || model_error.value !== '' :
    has_acp_validation_error.value,
);
const has_connection_validation_error = computed(() =>
    has_provider_validation_error.value ||
    (settings.value.ai_backend === 'OpenAICompatible' && api_key_length_error.value !== ''),
);
const has_settings_validation_error = computed(() =>
    has_provider_validation_error.value ||
    daily_ai_request_limit_error.value !== '' ||
    (settings.value.ai_backend === 'OpenAICompatible' && api_key_error.value !== ''),
);
const has_connection_test_result = computed(() =>
    connection_test_results.value.CandidateSelection !== null ||
    connection_test_results.value.EpisodeLookup !== null,
);
const is_settings_action_running = computed(() =>
    is_saving.value ||
    testing_connection_capability.value !== null ||
    is_deleting_api_key.value ||
    is_updating_acp_authentication.value,
);
const is_backfill_running = computed(() =>
    is_starting_backfill.value || is_monitoring_backfill.value || status.value?.is_running === true,
);
const is_episode_backfill_running = computed(() =>
    is_starting_episode_backfill.value ||
    is_monitoring_episode_backfill.value ||
    status.value?.is_episode_running === true,
);
const backfill_progress = computed(() => {
    if (backfill_task.value?.progress === null || backfill_task.value?.progress === undefined) return null;
    return Math.max(0, Math.min(100, backfill_task.value.progress * 100));
});
const episode_backfill_progress = computed(() => {
    if (episode_backfill_task.value?.progress === null || episode_backfill_task.value?.progress === undefined) {
        return null;
    }
    return Math.max(0, Math.min(100, episode_backfill_task.value.progress * 100));
});

function nullableTrimmed(value: string | null): string | null {
    const normalized = value?.trim() ?? '';
    return normalized === '' ? null : normalized;
}

/** 画面上の全ドラフトを backend 非依存の保存 payload へ変換する。 */
function buildSettingsRequest(): IRecordedSeriesSettingsUpdate {
    return {
        enabled: settings.value.enabled,
        ai_enabled: settings.value.ai_enabled,
        ai_episode_number_search_enabled: settings.value.ai_episode_number_search_enabled,
        ai_episode_number_acceptance_mode: settings.value.ai_episode_number_acceptance_mode,
        daily_ai_request_limit: Number(settings.value.daily_ai_request_limit),
        ai_backend: settings.value.ai_backend,
        api_base_url: normalizeAPIBaseURL(settings.value.api_base_url),
        model: settings.value.model.trim(),
        // モデル名と推論深さは分離して送る。Grok の model はサーバー側でも null に正規化される。
        acp_model: settings.value.ai_backend === 'AcpGrok' ?
            null :
            nullableTrimmed(settings.value.acp_model)
                ?? acp_defaults_by_backend[settings.value.ai_backend]?.model
                ?? null,
        acp_reasoning_effort: ['AcpCodex', 'AcpGrok'].includes(settings.value.ai_backend) ?
            (settings.value.acp_reasoning_effort
                ?? acp_defaults_by_backend[settings.value.ai_backend]?.effort
                ?? null) :
            null,
        acp_timeout_sec: Number(settings.value.acp_timeout_sec),
        google_cloud_project: nullableTrimmed(settings.value.google_cloud_project),
        google_cloud_location: nullableTrimmed(settings.value.google_cloud_location),
    };
}

// 設定 GET 反映中は backend 切替 watch の初期値注入を抑止する。
let suppress_acp_default_injection = false;

/** GET の保存済み snapshot を、backend 切替 watch の初期値注入を抑止しながら反映する。 */
function applyFetchedSettings(fetched_settings: IRecordedSeriesSettings): void {
    suppress_acp_default_injection = true;
    // ローリング更新中の旧サーバーや古い mock が廃止済み backend を返しても、
    // 一覧外の値を表示したり任意コマンド設定へ戻ったりしないようクライアントでも fail-closed にする。
    const is_supported_backend = ai_backend_options.some(option => option.value === fetched_settings.ai_backend);
    settings.value = is_supported_backend ?
        fetched_settings :
        {
            ...fetched_settings,
            ai_backend: 'OpenAICompatible',
            ai_enabled: false,
            acp_reasoning_effort: null,
        };
    // 同期的に watch が走ったあとにフラグを戻す。
    queueMicrotask(() => {
        suppress_acp_default_injection = false;
    });
    saved_api_base_url.value = normalizeAPIBaseURL(fetched_settings.api_base_url);
}


/** 設定画面のドラフトから、保存または接続テストに使う API キーを必要な場合だけ追加する。 */
function appendDraftAPIKey<T extends object>(request: T): T & {api_key?: string} {
    const api_key = api_key_input.value.trim();
    if (api_key === '') return request;
    return {...request, api_key};
}

/** 判定状況を更新し、バックフィル終了後は定期取得を止める。 */
async function refreshStatus(show_error = false): Promise<void> {
    if (is_refreshing_status.value) return;
    is_refreshing_status.value = true;
    const fetched_status = await RecordedSeries.fetchStatus(show_error);
    is_refreshing_status.value = false;
    if (fetched_status === null) return;

    status.value = fetched_status;
    if (
        fetched_status.is_running === false &&
        fetched_status.is_episode_running === false &&
        is_monitoring_backfill.value === false &&
        is_monitoring_episode_backfill.value === false
    ) {
        stopStatusPolling();
    }
}

/** 実行中の一括判定を、画面再読み込み後も状況 API から追跡する。 */
function startStatusPolling(): void {
    if (status_polling_timer !== null) return;
    status_polling_timer = window.setInterval(() => void refreshStatus(), 3000);
}

function stopStatusPolling(): void {
    if (status_polling_timer === null) return;
    window.clearInterval(status_polling_timer);
    status_polling_timer = null;
}

/** サーバー共有設定を保存する。API キー入力は保存成功後だけ破棄する。 */
async function saveSettings(): Promise<void> {
    if (has_settings_validation_error.value || is_settings_action_running.value) return;

    is_saving.value = true;
    let request = buildSettingsRequest();
    if (settings.value.ai_backend === 'OpenAICompatible') request = appendDraftAPIKey(request);
    const result = await RecordedSeries.updateSettings(request);
    if (result) {
        // サーバーから最新の設定を再取得し、URL 切替後も正しい api_key_configured を表示する。
        const fetched_settings = await RecordedSeries.fetchSettings();
        if (fetched_settings !== null) {
            applyFetchedSettings(fetched_settings);
        }
        api_key_input.value = '';
        api_key_showing.value = false;
        Message.success('録画シリーズ判定設定を更新しました。');
    }
    is_saving.value = false;
}

/** 固定6項目のうち指定した接続試験結果を安全に取得する。 */
function connectionCheck(
    result: IRecordedSeriesConnectionTestResult | null,
    check_name: RecordedSeriesEpisodeLookupConnectionCheckName,
): IRecordedSeriesConnectionTestCheck | null {
    return result?.checks?.[check_name] ?? null;
}

/** 接続試験項目の状態を日本語表示へ変換する。 */
function connectionCheckStatusLabel(
    status: RecordedSeriesConnectionTestCheckStatus | undefined,
): string {
    return status === undefined ? '未実行' : connection_check_status_labels[status];
}

/** 接続試験項目の状態に対応するアイコンを返す。 */
function connectionCheckIcon(
    status: RecordedSeriesConnectionTestCheckStatus | undefined,
): string {
    if (status === 'Passed') return 'fluent:checkmark-circle-20-filled';
    if (status === 'Failed') return 'fluent:error-circle-20-filled';
    if (status === 'NotApplicable') return 'fluent:subtract-circle-20-filled';
    return 'fluent:clock-20-filled';
}

/** 現在のドラフトを保存せず、指定した本番能力と同じ最小リクエストを試す。 */
async function testConnection(capability: RecordedSeriesConnectionTestCapability): Promise<void> {
    if (has_connection_validation_error.value || is_settings_action_running.value) return;

    testing_connection_capability.value = capability;
    connection_test_results.value[capability] = null;
    let request: IRecordedSeriesConnectionTestRequest = {
        ...buildSettingsRequest(),
        capability,
    };
    if (settings.value.ai_backend === 'OpenAICompatible') request = appendDraftAPIKey(request);
    const result = await RecordedSeries.testConnection(request);
    const capability_title = connection_test_capabilities.find(item => item.value === capability)?.title ?? capability;
    connection_test_results.value[capability] = result;
    if (result?.success) {
        Message.success(`${capability_title}の接続テストに成功しました。（${result.model} / ${result.latency_ms.toLocaleString()} ms）`);
    } else if (result !== null) {
        Message.error(`${capability_title}の接続テストに失敗しました。\n${result.message}`);
    }
    testing_connection_capability.value = null;
}

/** 保存済み API キーを、確認ダイアログから明示的に削除する。 */
async function deleteAPIKey(): Promise<void> {
    if (is_settings_action_running.value) return;
    is_deleting_api_key.value = true;
    if (await RecordedSeries.deleteAPIKey(saved_api_base_url.value)) {
        settings.value.api_key_configured = false;
        api_key_input.value = '';
        api_key_showing.value = false;
        api_key_delete_dialog.value = false;
        Message.success('保存済みの API キーを削除しました。');
    }
    is_deleting_api_key.value = false;
}

/** Codex / Grok の import / delete を確認ダイアログで明示する。 */
function openACPAuthenticationDialog(
    provider: KonomiTVBS4KACPImportProvider,
    action: ACPAuthenticationAction['action'],
): void {
    if (is_settings_action_running.value) return;
    pending_acp_authentication_action.value = {provider, action};
    acp_authentication_dialog.value = true;
}

/** 実行中でない認証確認ダイアログを閉じ、対象 provider を破棄する。 */
function closeACPAuthenticationDialog(): void {
    if (is_updating_acp_authentication.value) return;
    acp_authentication_dialog.value = false;
    pending_acp_authentication_action.value = null;
}

/** 確認済みの auth.json import / 専用コピー delete を管理 API へ送信する。 */
async function confirmACPAuthenticationAction(): Promise<void> {
    const pending_action = pending_acp_authentication_action.value;
    if (pending_action === null || is_updating_acp_authentication.value) return;

    is_updating_acp_authentication.value = true;
    const updated_status = pending_action.action === 'Import' ?
        await RecordedSeries.importACPAuthentication(pending_action.provider) :
        await RecordedSeries.deleteACPAuthentication(pending_action.provider);
    is_updating_acp_authentication.value = false;
    if (updated_status === null) return;

    acp_credential_status.value = updated_status;
    acp_authentication_dialog.value = false;
    pending_acp_authentication_action.value = null;
    const provider_name = pending_action.provider === 'codex' ? 'Codex' : 'Grok Build';
    if (pending_action.action === 'Import') {
        Message.success(`${provider_name} 認証を KonomiTV-BS4K 専用プロファイルへ取り込みました。`);
    } else {
        Message.success(`KonomiTV-BS4K の ${provider_name} 認証コピーを削除しました。`);
    }
}

/** サーバー側で記録した認証取り込み日時をローカル表示へ変換する。 */
function formatACPAuthImportedAt(imported_at: string): string {
    return dayjs(imported_at).format('YYYY/M/D HH:mm:ss');
}

/** 既存録画を対象に、バックグラウンドでシリーズ判定を実行する。 */
async function startBackfill(): Promise<void> {
    if (is_episode_backfill_running.value) return;
    const confirmation_message = force_backfill.value ?
        'シリーズ確定・単発番組を含むすべての録画を再判定します。既存の判定結果が更新され、AI API が有効な場合は API 利用が再度発生することがあります。続行しますか？' :
        '確定済みの結果を再利用して、未判定の既存録画をシリーズ判定します。AI 生成が有効な場合は、Manual / Rule 以外で API 利用が発生します。続行しますか？';
    const confirmed = window.confirm(confirmation_message);
    if (confirmed === false) return;

    is_starting_backfill.value = true;
    const accepted = await RecordedSeries.startBackfill(force_backfill.value);
    is_starting_backfill.value = false;
    if (accepted === null) return;

    status.value = status.value === null ? null : {...status.value, is_running: true};
    is_monitoring_backfill.value = true;
    startStatusPolling();
    Message.info(accepted.reused ? '実行中のシリーズ判定を引き続き監視します。' : '既存録画のシリーズ判定を開始しました。');

    // 同じ画面から再実行した場合に古い監視を残さない。
    backfill_abort_controller?.abort();
    backfill_abort_controller = new AbortController();
    const execution = await AnalysisTasks.waitForCompletion(
        accepted.execution_id,
        backfill_abort_controller.signal,
        task => backfill_task.value = task,
    );
    if (backfill_abort_controller.signal.aborted) return;

    is_monitoring_backfill.value = false;
    await refreshStatus();
    if (execution?.status === 'Succeeded') {
        Message.success('既存録画のシリーズ判定が完了しました。');
    } else if (execution?.status === 'Interrupted') {
        Message.warning('既存録画のシリーズ判定が中断されました。');
    } else if (execution !== null) {
        Message.error(`既存録画のシリーズ判定に失敗しました。${execution.error_message ? `\n${execution.error_message}` : ''}`);
    }
}

/** Series所属済みの既存録画を対象に、バックグラウンドで話数Web検索を実行する。 */
async function startEpisodeBackfill(): Promise<void> {
    if (is_backfill_running.value) return;
    const confirmation_message = force_episode_backfill.value ?
        '手動訂正を除く判定済みの話数も、最新の AI 設定で再検索します。録画件数に応じて AI API 利用が発生します。続行しますか？' :
        '話数が未確定の既存録画を、保存済みの AI 設定で Web 検索します。録画件数に応じて AI API 利用が発生します。続行しますか？';
    const confirmed = window.confirm(confirmation_message);
    if (confirmed === false) return;

    is_starting_episode_backfill.value = true;
    const accepted = await RecordedSeries.startEpisodeBackfill(force_episode_backfill.value);
    is_starting_episode_backfill.value = false;
    if (accepted === null) return;

    status.value = status.value === null ? null : {...status.value, is_episode_running: true};
    is_monitoring_episode_backfill.value = true;
    startStatusPolling();
    Message.info(
        accepted.reused ?
            '実行中の一括話数判定を引き続き監視します。' :
            '既存録画の一括話数判定を開始しました。',
    );

    // シリーズ一括判定とは別の履歴IDを追跡し、進捗と完了通知を混同しない。
    episode_backfill_abort_controller?.abort();
    episode_backfill_abort_controller = new AbortController();
    const execution = await AnalysisTasks.waitForCompletion(
        accepted.execution_id,
        episode_backfill_abort_controller.signal,
        task => episode_backfill_task.value = task,
    );
    if (episode_backfill_abort_controller.signal.aborted) return;

    is_monitoring_episode_backfill.value = false;
    await refreshStatus();
    if (execution?.status === 'Succeeded') {
        Message.success('既存録画の一括話数判定が完了しました。');
    } else if (execution?.status === 'Interrupted') {
        Message.warning('既存録画の一括話数判定が中断されました。');
    } else if (execution !== null) {
        Message.error(
            `既存録画の一括話数判定に失敗しました。${execution.error_message ? `\n${execution.error_message}` : ''}`,
        );
    }
}

function formatLastRunAt(value: string | null): string {
    return value === null ? '未実行' : dayjs(value).format('YYYY/M/D HH:mm:ss');
}

watch(() => settings.value.ai_backend, (backend, previous) => {
    connection_test_results.value = {CandidateSelection: null, EpisodeLookup: null};
    // 設定取得反映中や初回マウントでは初期値注入しない。
    if (suppress_acp_default_injection || previous === undefined || previous === backend) {
        return;
    }
    if (backend === 'OpenAICompatible') {
        settings.value.acp_reasoning_effort = null;
        return;
    }
    // ユーザーが backend を切り替えたときだけ、その ACP の推奨初期値を埋める。
    const defaults = acp_defaults_by_backend[backend];
    if (defaults === undefined) return;
    settings.value.acp_model = defaults.model;
    settings.value.acp_reasoning_effort = defaults.effort;
});


onMounted(async () => {
    // API キーの設定状態を含むため、管理者と確認できるまでは設定 API へアクセスしない。
    const fetched_user = await user_store.fetchUser();
    // fetchUser() はアイコン取得だけが失敗した場合も null を返すが、ユーザー本体は Store に残る。
    const user = fetched_user ?? user_store.user;
    if (user === null) {
        authorization_error.value = 'UserUnavailable';
        is_loading.value = false;
        return;
    }
    if (user.is_admin !== true) {
        authorization_error.value = 'AdminRequired';
        is_loading.value = false;
        return;
    }
    const [fetched_settings, fetched_status, fetched_acp_credential_status] = await Promise.all([
        RecordedSeries.fetchSettings(),
        RecordedSeries.fetchStatus(),
        RecordedSeries.fetchACPCredentialStatus(),
    ]);
    if (fetched_settings !== null) {
        applyFetchedSettings(fetched_settings);
        is_disabled.value = false;
    }
    if (fetched_status !== null) {
        status.value = fetched_status;
        if (fetched_status.is_running || fetched_status.is_episode_running) startStatusPolling();
    }
    if (fetched_acp_credential_status !== null) {
        acp_credential_status.value = fetched_acp_credential_status;
    }
    is_loading.value = false;
});

onUnmounted(() => {
    // 画面を離れたら API キー入力とポーリングを即座に破棄する。
    api_key_input.value = '';
    backfill_abort_controller?.abort();
    episode_backfill_abort_controller?.abort();
    stopStatusPolling();
});

</script>

<style lang="scss" scoped>

.recorded-series-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
}

.recorded-series-auth-status {
    display: flex;
    flex-direction: column;
    gap: 5px;
    padding: 11px 13px;
    border-radius: 6px;
    background: rgb(var(--v-theme-background-lighten-1));
    color: rgb(var(--v-theme-text-darken-1));
    font-size: 12px;

    strong {
        color: rgb(var(--v-theme-error-readable));
    }

    &--ok {
        color: rgb(var(--v-theme-success-readable)) !important;
    }
}

.recorded-series-beta-badge {
    flex-shrink: 0;
    margin-left: 8px;
    padding: 1px 6px;
    border-radius: 4px;
    background: rgb(var(--v-theme-warning));
    color: rgb(var(--v-theme-on-warning));
    font-size: 10px;
    font-weight: 700;
    line-height: 18px;
    letter-spacing: 0.08em;
}

.recorded-series-ai-options {
    margin-left: 18px;
    padding-left: 17px;
    border-left: 3px solid rgba(var(--v-theme-primary), 0.35);
    transition: opacity 0.2s;

    &--disabled {
        opacity: 0.5;
    }
}

.recorded-series-connection-results {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.recorded-series-connection-result {
    display: flex;
    align-items: flex-start;
    gap: 9px;
    padding: 10px 12px;
    border-radius: 6px;
    color: rgb(var(--v-theme-success-readable));
    background: rgba(var(--v-theme-success), 0.1);

    > div {
        display: flex;
        flex-direction: column;
        min-width: 0;
    }

    strong,
    span,
    small {
        overflow-wrap: anywhere;
    }

    strong {
        color: rgb(var(--v-theme-text));
        font-size: 12.5px;
    }

    span {
        margin-top: 2px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 11.5px;
    }

    small {
        margin-top: 3px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 10.5px;
    }

    &--error {
        color: rgb(var(--v-theme-error-readable));
        background: rgba(var(--v-theme-error), 0.1);
    }
}

.recorded-series-connection-checks {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin: 8px 0 0;
    padding: 0;
    list-style: none;

    li {
        display: flex;
        align-items: flex-start;
        gap: 5px;
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 11px;

        svg {
            flex-shrink: 0;
            margin-top: 1px;
        }
    }

    &--Passed,
    &--Passed span {
        color: rgb(var(--v-theme-success-readable)) !important;
    }

    &--Failed,
    &--Failed span {
        color: rgb(var(--v-theme-error-readable)) !important;
    }
}

.recorded-series-access-state {
    display: flex;
    align-items: center;
    min-height: 150px;
    gap: 10px;
    color: rgb(var(--v-theme-text-darken-1));
    font-size: 13px;
}

.recorded-series-status-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 10px;
}

.recorded-series-status-card {
    display: flex;
    flex-direction: column;
    min-width: 0;
    padding: 13px 15px;
    border-radius: 7px;
    background: rgb(var(--v-theme-background-lighten-1));

    span {
        color: rgb(var(--v-theme-text-darken-1));
        font-size: 12.5px;
    }

    strong {
        margin-top: 2px;
        color: rgb(var(--v-theme-text));
        font-size: 21px;
    }

    &--error strong {
        color: rgb(var(--v-theme-error-readable));
    }
}

.recorded-series-dialog {
    background: rgb(var(--v-theme-background-lighten-1));
}

@include smartphone-vertical {
    .recorded-series-ai-options {
        margin-left: 8px;
        padding-left: 11px;
    }

    .recorded-series-status-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
}

</style>
