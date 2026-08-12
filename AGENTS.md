# AGENTS.md

## KonomiTV-BS4K の必須指示読込

`KonomiTV-BS4K/` に関係する依頼（質問への回答、調査、計画、
レビュー、編集、コマンド実行を含む）では、対象作業を始める前に、
次の2ファイルをこの順で必ず EOF まで全文読むこと。

1. `KonomiTV-BS4K/AGENTS.md`
2. `KonomiTV-BS4K/AGENTS-BS4K.md`

両ファイルを読み終えるまでは、ファイルの所在確認と読取り以外の
作業を行ってはならない。抜粋、検索結果、過去の記憶、要約で
全文読了を代替してはならない。

両ファイルの指示が矛盾する場合は `AGENTS-BS4K.md` を優先する。

ユーザーから「AGENTS.md を読んで」と指示された場合は、
上記2ファイルも対象に含め、読了後に各ファイルのパスと行数を報告すること。

## プロジェクト固有の注意事項

- yarn や poetry はそれぞれ `client/` と `server/` のディレクトリに移動した状態で実行してください。ルートディレクトリにはパッケージ管理系のファイルは一切配置していません。
- サーバー側では poetry を使っているので、python コマンドは必ず全て poetry run 経由で実行します。python を直接実行すると .venv/ 以下のライブラリがインストールされていないために失敗します。

## 開発環境構成

### 基本方針

- KonomiTV-BS4K の実行ターゲットは Docker Linux のみです。通常の開発・動作確認も Docker の Development 環境で行います
- Main 環境と Development 環境は、コンテナ・イメージ・設定・データ・ログを分離します。片方の操作で他方の状態を変更しないでください
- 実際のホストパス、ドメイン、IP アドレス、リバースプロキシ構成はマシン固有の指示を参照し、Git 管理下の文書やコードへ記載しないでください
- Compose のファイル構成、起動手順、状態保護、検証方法は `AGENTS-BS4K.md` の指示に従ってください

### Main 環境 (port 7000、常にユーザー管理)

- Main 環境は、動作確認済みの `main` ブランチを提供する常用環境です
- エージェントは、ユーザーの明示的な許可なく Main コンテナを起動・停止・再起動・再構築・再設定してはいけません
- 通常のコード変更を Main 環境で直接検証してはいけません。Development 環境で検証してから Main へ反映します

### Development 環境 (port 7100)

- このリポジトリのワークツリーを、Development イメージのビルドコンテキスト兼 Compose project の起点として使用します
- Development 固有の設定・データ・ログ・録画ミラー・キャプチャは `docker/development/state/` 以下へ隔離します
- コンテナ内の実行コードはイメージに格納されています。ホストのソースツリーは参照用の read-only bind であり、コード変更は hot reload されません
- コード変更を反映するときは、検証付き Development イメージをビルドしてから Development コンテナだけを再作成します
- エージェントは必要な動作確認のために Development 環境をビルド・再作成できますが、事前確認と状態保護に関する `AGENTS-BS4K.md` の手順を省略してはいけません

### ブラウザ検証

- ブラウザからは、マシン固有の指示で定義された Development URL にアクセスします
- 検証対象の画面に表示される Git commit と、Development API が返す Git commit が現在のワークツリーと一致することを確認します
- Main URL と Development URL を取り違えないでください

### HTTPS が必須な理由

- KonomiTV はクリップボードなど Secure Context (HTTPS) でしか動作しない API を使用しています
- localhost 以外でも正規の HTTPS で提供できるよう、Akebi HTTPS Server が `akebi.konomi.tv` の keyless server を経由してリバースプロキシを行っています
- HTTP に直接アクセスされると Secure Context API が動かず混乱を招くため、内部の HTTP は loopback アドレスだけでリッスンする構成になっています

## 技術スタック

KonomiTV は、クライアント・サーバーアーキテクチャに基づく Web アプリケーション (PWA) です。
以下の2つの主要部分で構成されています。

KonomiTV が一般的な Web サービスと異なる点は、フロントエンドと API サーバーの両方が各ユーザーの PC 環境で動作する点です。
upstream KonomiTV は Windows と Linux の双方を対象としていますが、KonomiTV-BS4K は Docker Linux のみを対象とします。

- `client/`: KonomiTV のフロントエンドアプリケーション (PWA)
  - TypeScript
  - yarn v1
  - Vite
  - Vue.js 3.x
    - Vuetify 3.x
    - Pinia
- `server/`: KonomiTV のバックエンド API サーバー
  - Python 3.11
  - Poetry
  - Uvicorn
  - FastAPI
    - Pydantic v2
  - Tortoise ORM
    - SQLite (ローカル動作が必要なため MySQL や PostgreSQL は採用できなかった)
    - Aerich

## ディレクトリ構成

### クライアント (`client/`)

- `public/`: 直接提供される静的ファイル
- `src/`: ソースコード
  - `views/`: Vue ルートコンポーネント/ページ
    - `TV/`: テレビ視聴関連ページ
    - `Videos/`: 動画関連ページ
    - `Reservations/`: 予約関連ページ
    - `Settings/`: アプリケーション設定ページ
    - `Login.vue`: ログインページ
    - `Register.vue`: アカウント登録ページ
    - `MyList.vue`: マイリストページ
    - `WatchedHistory.vue`: 視聴履歴ページ
    - `MyPage.vue`: マイページ
    - `NotFound.vue`: 404 エラーページ
  - `components/`: Vue コンポーネント
    - `Watch/`: テレビ・録画番組視聴画面向けコンポーネント群
      - `Panel/`: 視聴画面右側のパネル内表示用コンポーネント群
        - `Twitter/`: ツイート検索/タイムライン表示/キャプチャ管理/ツイート表示用コンポーネント群
    - `Settings/`: 設定ページから呼び出されるダイアログコンポーネント群
    - `HeaderBar.vue`: ヘッダーバー
    - `SPHeaderBar.vue`: スマートフォン用ヘッダーバー
    - `Navigation.vue`: ナビゲーション
    - `BottomNavigation.vue`: スマートフォン用下部ナビゲーション
    - `Snackbars.vue`: 通知メッセージ表示コンポーネント
    - `Breadcrumbs.vue`: パンくずリスト表示コンポーネント
  - `stores/`: 状態管理 (Pinia ストア)
  - `services/`: サーバー API へのサービスクライアント
    - `player/`: KonomiTV の視聴画面で用いられるライブ/ビデオプレイヤーのロジック (重要)
      - `managers/`: PlayerController に紐づく様々な機能のロジックを提供し、各機能に責任を持つ PlayerManager 群
      - `PlayerController.ts`: 動画プレイヤーである DPlayer に関連するロジックを丸ごとラップするクラスで、KonomiTV の再生系ロジックの中核を担う
  - `utils/`: ユーティリティ関数とヘルパー
  - `workers/`: 重い処理をバックグラウンドで実行するための Web Workers コード (with Comlink)
  - `styles/`: グローバル CSS の定義 (グローバル CSS は `App.vue` の方がメイン)
  - `router/`: Vue Router 設定
  - `plugins/`: Vue プラグインの初期化定義
  - `App.vue`: アプリケーションのルートコンポーネント (グローバル CSS 定義もここに含まれる)
  - `main.ts`: アプリケーションのエントリーポイント・初期化処理
- `package.json`: Node.js プロジェクト設定と依存関係 (yarn)
- `vite.config.mts`: Vite ビルド設定
- `tsconfig.json`: TypeScript 設定
- `.eslintrc.json`: ESLint コードスタイル設定

### サーバー (`server/`)

- `app/`: FastAPI アプリケーションコード
  - `routers/`: API ルートハンドラー
    - `ChannelsRouter.py`: チャンネル関連メタデータ取得 API
    - `ProgramsRouter.py`: 番組関連メタデータ取得 API
    - `VideosRouter.py`: 録画番組メタデータ取得 API
    - `SeriesRouter.py`: 番組シリーズ関連 API
    - `LiveStreamsRouter.py`: 放送中テレビ放送のライブストリーミング配信関連 API
    - `VideoStreamsRouter.py`: 録画番組のストリーミング配信関連 API
    - `ReservationsRouter.py`: EDCB と連携したテレビ番組の録画予約関連 API
    - `ReservationConditionsRouter.py`: EDCB と連携したテレビ番組の自動録画予約条件 (EPG 自動予約) 関連 API
    - `DataBroadcastingRouter.py`: データ放送のインターネット接続機能向け API
    - `CapturesRouter.py`: キャプチャ画像管理 API
    - `TwitterRouter.py`: Twitter 連携 API
    - `NiconicoRouter.py`: ニコニコ実況連携 API
    - `UsersRouter.py`: ユーザーアカウント管理 API
    - `SettingsRouter.py`: クライアント・サーバー設定管理 API
    - `MaintenanceRouter.py`: サーバーメンテナンス用 API
    - `VersionRouter.py`: バージョン情報 API
  - `models/`: データベースモデルとスキーマ
    - `Channel.py`: チャンネル情報を管理するモデル（放送局情報、チャンネル番号、ロゴ、ストリーム設定など）
    - `Program.py`: 放送番組情報を管理するモデル（番組メタデータ、EPG 番組情報、タイトル、番組詳細、ジャンルなど）
    - `RecordedProgram.py`: 録画済み番組のメタデータを管理するモデル（EPG 録画番組情報、録画開始/終了時刻など）
    - `RecordedVideo.py`: 録画済み番組の動画ファイル情報を管理するモデル（ファイルパス、映像/音声コーデック、ファイルサイズなど）
    - `Series.py`: 番組シリーズ情報を管理するモデル（シリーズ名、シリーズ ID など）
    - `SeriesBroadcastPeriod.py`: 番組シリーズの放送期間情報を管理するモデル
    - `TwitterAccount.py`: Twitter アカウント連携情報を管理するモデル（トークン、認証情報など）
    - `User.py`: ユーザーアカウント情報を管理するモデル（認証情報、権限など）
  - `migrations/`: Tortoise ORM のマイグレーションツール: Aerich 向けの DB マイグレーション定義 (Aerich で自動生成されたコードを修正したもの)
  - `streams/`: テレビ放送のライブストリーミング・録画番組のオンデマンドストリーミング関連の実装
    - `LiveEncodingTask.py`: ライブストリーミング用のエンコード・ストリーミングタスクを管理
    - `VideoEncodingTask.py`: 録画番組用のエンコード・ストリーミングタスクを管理
    - `LiveStream.py`: 放送波のライブストリーミングの状態管理
    - `VideoStream.py`: 録画番組のオンデマンドストリーミングの状態管理
    - `LivePSIDataArchiver.py`: 放送波から PSI/SI データを抽出・アーカイブする機能の実装
  - `metadata/`: 録画番組データから番組情報などのメタデータを抽出・保存するための実装
    - `RecordedScanTask.py`: 録画フォルダの監視とメタデータの DB への同期を行うタスク
    - `MetadataAnalyzer.py`: 録画ファイルのメタデータを解析するクラス
    - `TSInfoAnalyzer.py`: 録画 TS ファイルや録画データ関連ファイルに含まれる番組情報を解析するクラス
    - `ThumbnailGenerator.py`: プレイヤーのシークバー用タイル画像と、候補区間内で最も良い1枚の代表サムネイルを生成するクラス
    - `CMSectionsDetector.py`: 録画 TS ファイルに含まれる CM 区間を検出するクラス
  - `utils/`: ユーティリティ関数とヘルパー
    - `edcb/`: EDCB 連携用の API クライアント実装
    - `JikkyoClient.py`: ニコニコ実況・NX-Jikkyo 連携用の API クライアント実装
    - `TwitterGraphQLAPI.py`: Twitter API 連携用にリバースエンジニアリングして開発した API クライアント実装
    - `TSInformation.py`: 日本のテレビ放送で用いられている MPEG2-TS から情報を取得する際に役立つユーティリティ群
    - `DriveIOLimiter.py`: ドライブごとの同時実行数を制限するためのユーティリティクラス
    - `ProcessLimiter.py`: プロセスごとの同時実行数を制限するためのユーティリティクラス
  - `app.py`: FastAPI アプリケーションやルーターの初期化・バックグラウンドタスクの定義
  - `config.py`: サーバー設定 (`config.yaml`) のロードとバリデーション
  - `constants.py`: サーバー全体で用いられるグローバル定数
  - `logging.py`: ロギング設定
  - `schemas.py`: API リクエスト/レスポンス型に用いる Pydantic スキーマ
- `data/`: アプリケーションデータ用ディレクトリ
  - `database.sqlite`: SQLite データベースファイル
- `logs/`: アプリケーションログ用ディレクトリ
- `misc/`: メンテナンス・デバッグ用 Pythonスクリプト群
- `static/`: サーバー API によって提供される静的ファイル (Git 管理下にあり、放送局ロゴなどが含まれる)
- `thirdparty/`: FFmpeg や QSVEncC などのエンコーダーをはじめとした、ビルド済みのサードパーティー実行ファイル (Git 管理外で、`poetry run task update-thirdparty` で更新する)
- `pyproject.toml`: Python プロジェクト設定と依存関係 (Poetry)
- `KonomiTV.py`: KonomiTV サーバーのエントリーポイント
- `KonomiTV-Service.py`: Windows サービス管理スクリプト & Windows サービスのエントリーポイント

## アーキテクチャ上の設計判断と既知の制約

### ライブストリーミング (LiveStream / LiveEncodingTask) の協調動作

- `LiveStream.connect()` と `LiveEncodingTask.run()` は相互依存の関係にある。チャンネル切り替え時は `connect()` が旧タスクを `cancel()` し、`run()` がチューナー起動フェーズから `Controller()` までの `CancelledError` を捕捉してクリーンアップに到達する
- `EDCBTuner._isOwner()` チェックは二重操作を防ぐガードレールで、`handoff()` で所有権を移譲した後は旧ストリームからの `close()` / `disconnect()` はこのチェックで弾かれる
- Python 3.11 では `CancelledError` を捕捉するとキャンセルカウンターがデクリメントされ、以降の `await` は正常に動作する。`asyncio.wait()` はタスク状態を変更しないが、`asyncio.wait_for()` はタイムアウト時にタスクを再度 cancel するので挙動が異なる点に注意する
- より詳細な処理の流れは `server/app/streams/LiveEncodingTask.py` 内のコメントを参照すること

### 録画再生 (RecordedFMP4Stream / RecordedFMP4Cache)

- このフォークの録画再生は `server/app/streams/RecordedFMP4Stream.py` が FFmpeg 8 で fMP4 をオンデマンド生成し、`RecordedFMP4CacheManager` が生成物の参照・排他・遅延削除を管理する。upstream の `VideoStream` / `VideoEncodingTask` を前提にした変更をそのまま持ち込まないこと
- 同一セッションでは録画・画質・映像コーデック・bit depth・24fps モード・音声コーデックを固定する。HLS の子 API も初回と同じ生成条件を渡し、同じ session ID を異なる条件で再利用しない
- fMP4 キャッシュの識別子には録画 ID・ファイルハッシュ・生成条件・パイプライン改訂値を含める。書き込みは同一ディレクトリへの一時ファイル作成、`fsync()`、atomic rename の順を維持し、最後のセッション参照が外れた後に遅延削除する
- エンコーダー能力検査と実再生は `RecordedPlaybackBackend` の定義を共有する。設定上選べる組み合わせと、実機で利用可能と判定された組み合わせを混同しない

### 録画フォルダスキャン (RecordedScanTask) の設計判断

- `RecordedScanTask.run()` は起動時の全件スキャン (`runBatchScan()`) と新規ファイルの監視 (`watchRecordedFolders()`) を `asyncio.gather()` で同時に実行しており、起動時スキャンが完了する前から新規録画の監視は動いている
- `runBatchScan()` は `iterRecordedFolderPaths()` から見つかった順に録画を処理し、CM 解析 workspace を列挙段階で枝刈りする。録画ごとの処理は `BATCH_PIPELINE_CONCURRENCY` を上限に重ね、`processRecordedFile()` はファイル情報が DB と一致する録画の重い解析をスキップする
- 起動時に全パスを先に収集・ソートする構造へ変更しない。HDD・NAS・SMB では最初の録画を処理するまでの固定 IO コストが大きくなるため、列挙済みの録画から処理しつつ新規録画の監視も並行する設計を維持する

## コーディング規約

### 全般
- サービス名はコード・型・コメント・UI・文書のすべてで一貫して `Twitter` と表記する。`X` は `X Premium` などの正式な商品名、`x.com` などのホスト名、HTTP ヘッダー名のように原表記が技術的に必要な場合だけ使用する
- コードをざっくり斜め読みした際の可読性を高めるため、日本語のコメントを多めに記述する
- コードを変更する際、既存のコメントは、変更によりコメント内容がコードの記述と合わなくなった場合を除き、コメント量に関わらずそのまま保持する
- ログメッセージに関しては文字化けを避けるため、必ず英語で記述する
- それ以外のコーディングスタイルは、原則変更箇所周辺のコードスタイルに合わせる
- 不要な薄いラッパーや別名関数は作らず、責務のあるコンポーネントだけを追加する。
- コメントは冗長なくらいでちょうどよい。条件分岐・ループ・例外処理の直前にはその意図を書き、Python では `__init__()` で代入するインスタンス変数には「保持する情報」「参照されるメソッド」「前提条件」を必ずコメントとして記す。クラス Docstring には責務のみを記載し、引数説明は `__init__()` の Docstring に集約する
- Enum・Literal・Union 型の文字列表現は `tweet_capture_watermark_position: 'None' | 'TopLeft' | 'TopRight' | 'BottomLeft' | 'BottomRight';` のように基本的に UpperCamelCase で命名する必要がある
- 通常の Web サービスではないかなり特殊なソフトウェアなので、コンテキストとして分からないことがあれば別途 Readme.md を読むか、私に質問すること
- DB レコードの Pydantic / TypeScript 定義では、親となるレコード本体のスキーマを最上位に配置し、その下に子スキーマをフィールドの定義順に従って並べる
- JSON フィールドの値を生成する際は、辞書リテラル (`{}`) を直接書くのではなく、TypedDict のコンストラクタを使用して型構造を明示的に示す
- 画像の幅・高さ・総数・間隔など、視覚的に重要な情報を持つフィールドは定義の上部に集約し、重要度の高い順に配置することで一目で把握できるようにする
- 親スキーマから子スキーマへの並び順を徹底し、関連する子スキーマは親となる DB レコードスキーマの直下にまとめて配置する。可読性を損なうような配置変更は行わない
- TypeScript 側のスキーマ定義も Python 側と同じ順序を維持する。もし差分が発生する場合は、その理由をコメントで明記する

### Python コード
- **コードの編集後には、必ず `poetry run task lint` コマンドで、Ruff によるコードリンターと Pyright による型チェッカーを実行すること**
- 文字列にはシングルクォートを用いる (Docstring を除く)
- Python 3.11 の機能を使う (3.10 以下での動作は考慮不要)
- ビルトイン型を使用した Type Hint で実装する (from typing import List, Dict などは避ける)
- Pydantic モデル定義では必ず Annotated 記法を使う。`= Field()` 型の定義は行わずに全て Annotated 記法で定義すること
- 変数・インスタンス変数は snake_case で命名する
- 関数・クラス名は UpperCamelCase で命名する (例: `class VideoEncodingTask:`, `def GetClientURL():`)
  - FastAPI で定義するエンドポイントの関数名も UpperCamelCase で命名する必要がある
  - FastAPI で定義するエンドポイント名は、文法的に比較的正しくなるようパス名や操作を並び替えた上で、「〇〇API」の形で命名すること
    - 例: GET `/streams/live/{display_channel_id}/{quality}/mpegts` -> `LiveMPEGTSStreamAPI`
    - 例: PUT `/users/me` -> `UserUpdateAPI`
- クラスに生えたメソッド名は lowerCamelCase で命名する (例: `LiveStream.getONAirLiveStreams()`)
- 複数行のコレクションには末尾カンマを含める
- `getattr()` で型チェッカーを黙らせるのは禁止。参照する属性は型ヒントやプロパティできちんと公開し、どうしても `getattr()` が必要な場合は「その属性が必ず存在する根拠」を詳細にコメントする
- すべての Docstring には Args / Returns を明記し、コメントは処理のまとまりごとに必ず加えて「なぜそうするのか」「何を意図した値なのか」を丁寧に説明する。コードを読まなくてもコメントから処理の流れを追えるようにする
- このプロジェクトでは必ずロギングモジュールとして `import logging` の代わりに `from app import logging` を使うべき

### Vue / TypeScript コード

- **コードの編集後には、必ず `yarn lint; yarn typecheck` コマンドで、ESLint によるコードリンターと TypeScript による型チェッカーを実行すること**
- **`window.confirm()` / `window.alert()` などのブラウザ標準ダイアログは絶対に使用しないこと。Vuetify で既存 UI と一貫したダイアログを実装する。標準ダイアログで済ませるのは妥協・甘え・ボケナス実装であり、KonomiTV の UI として許容しない**
- 文字列にはシングルクォートを用いる
- 新規で実装する箇所に関しては Vue 3 Composition API パターンに従う
  - Vue.js 2 から移行した関係で Options API で書かれているコンポーネントがあるが、それらは Options API のまま維持する
- 新規で実装する Vue 3 Composition API のコンポーネントでは、原則として変数を lowerCamelCase で命名する
  - FastAPI サーバーでは snake_case で命名している関係で外部 API のフィールドは全てスネークケースになっているが、これはそのまま参照して良い
- TypeScript による型安全性を確保する
- コンポーネント属性は可能な限り1行に記述 (約100文字まで)
- 必ず day.js を utils/index.ts からインポートして使うこと！！！new Date() を絶対に使うな！！！
- クライアント側で新たに永続化したい値が出てきた場合、`localStorage.setItem` / `getItem` を直接呼ばず、必ず `client/src/stores/SettingsStore.ts` の `ILocalClientSettings` / `ILocalClientSettingsDefault` に集約する。SettingsStore は LocalStorage への永続化と、KonomiTV アカウントによるサーバー側設定との双方向同期を一手に引き受けており、独自キーを直書きするとこの同期の枠組みから外れてしまう
  - DB の連番 ID に依存する値 (`selected_twitter_panel_account` など) は環境が変わると意味を失うため、`ENVIRONMENT_SPECIFIC_SETTINGS_KEYS` に加えて同期無効にする
  - 他者の実装をレビューする際は `localStorage` / `sessionStorage` を grep し、この集約を迂回した直接アクセスが紛れ込んでいないか必ず確認する
  - なお、アクセストークン (`Utils.ts`)・データ放送 NVRAM エミュレーション (`DataBroadcasting.vue`)・DPlayer 側のキー (`Jikkyo.vue` の `dplayer-danmaku-*`) は、ユーザー設定ではなく認証状態・ハードウェアエミュレーション・サードパーティ側の永続化キーであるため、この集約ルールの対象外として既存のまま残っている

### CSS / SCSS スタイリング
- このプロジェクトで使用している色 (CSS 変数) などは `client/src/App.vue` や `client/src/plugins/vuetify.ts` に定義しているので、それを参照すること
- 新規に UI を実装する際は、すでに実装されている他のコンポーネントやページの大まかなデザインの方向性を踏襲すること
