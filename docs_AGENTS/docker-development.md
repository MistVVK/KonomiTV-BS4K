# Docker 開発運用

Development 環境のビルド・再作成・再起動など、Docker コンテナを操作する作業の前に読むこと。

## 環境の分離

- Main と Development は、Compose project、コンテナ、イメージ、設定、データ、ログを分離する。
- Main は動作確認済みの `main` ブランチを提供する常用環境とし、Development は現在の開発ワークツリーを検証する環境とする。
- エージェントは、ユーザーの明示的な許可なく Main コンテナを起動・停止・再起動・再構築・再設定してはいけない。
- マシン固有の指示で廃止済みと指定された旧パスは、Development の Compose 起点、bind source、状態保存先として使用してはいけない。

## Development Compose

- Development の Compose 操作は、現在のリポジトリルートを working directory として実行する。
- GPU 構成を含む Compose ファイルの組み合わせは `.env` の `COMPOSE_FILE` で切り替える（`.env.example` 参照）。`-f` を毎回手で並べない。
  - Development 例: `COMPOSE_FILE=compose.development.yaml:compose.intel.yaml:compose.nvidia.yaml`
  - 公開・Main 例: `COMPOSE_FILE=compose.yaml` に必要なら `compose.intel.yaml` / `compose.amd.yaml` / `compose.nvidia.yaml` を連結
  - `compose.intel.yaml` / `compose.amd.yaml` は `NONFREE` ビルド引数の既定値をそれぞれ `intel-nonfree` / `amd-nonfree` にし、Intel / AMD 専用イメージのライセンス文書から不要なベンダーの警告を除く。Intel と AMD を併用する場合は `.env` に `KONOMITV_NONFREE=nonfree` を明示する（後勝ちマージで `amd-nonfree` になるため）
  - `compose.intel.yaml` / `compose.amd.yaml` は `devices` に `${KONOMITV_INTEL_DRI_DEVICE:-/dev/dri/}` / `${KONOMITV_AMD_DRI_DEVICE:-/dev/dri/}` を使う。空なら `/dev/dri/` 全体を渡して自動選択、render node を書けばそのノードだけを渡す
- 公開・Main 用の `compose.yaml` に Development の状態を上書きする運用は行わない。
- Development は本体 `konomitv` と専用 sidecar `cloud-storage` の2サービスで構成される。
  - `konomitv` は `verified-runtime` target を使用する。通常のコード変更を未検証の `runtime` target だけで起動してはいけない。
  - `cloud-storage` は `docker/cloud-storage/Dockerfile` を使用し、rclone 1.73.0 と Python 3.14 環境をビルドする。
- コード変更を Development で確認する必要があるときは、ユーザーへ再ビルド・再作成・再起動を依頼せず、エージェントが自ら実行する。
- ビルド中は既存の Development コンテナを稼働させ、ビルド成功後にコンテナだけを再作成して停止時間を最小化する。
- Development のビルドには次のコマンドを使用する（`.env` の `COMPOSE_FILE` が効く前提）。

```bash
docker compose build konomitv cloud-storage
```

- ビルド成功後の再作成には次のコマンドを使用する。

```bash
docker compose up -d --no-build --force-recreate konomitv cloud-storage
```

## 状態保護と事前確認

- `docker/development/state/` 以下の `config.yaml`、`data/`、`logs/`、`recordings/`、`captures/`、および cloud-storage 用の以下のディレクトリを Development 専用状態として保持する。
  - `docker/development/state/cloud-control/` (本体との IPC 用 Unix socket)
  - `docker/development/state/cloud-mounts/` (FUSE マウント伝播用)
  - `docker/development/state/data/konomitv-bs4k-cloud-storage/` (クラウド認証情報保管用)
  - `docker/development/state/data/konomitv-bs4k-cloud-generated-source/` (公開目録・メタデータ等の生成物専用 spool)
  - ※公開・Main 用の `compose.yaml` では `server/data/konomitv-bs4k-cloud-control/`、`server/data/konomitv-bs4k-cloud-mounts/`、`server/data/konomitv-bs4k-cloud-storage/`、`server/data/konomitv-bs4k-cloud-generated-source/` を使用し、Development と共有しない。
- 上記の cloud-storage 関連 bind は `create_host_path: false` で定義されている。初回準備などでホスト上にディレクトリが存在しない場合のみ、Compose の `KONOMITV_UID` / `KONOMITV_GID` に合わせて mode 0700 で作成する。
- 既存の認証情報・保存データ・状態ディレクトリを初期化、上書き、削除してはいけない。また、既存ディレクトリへの再帰的な `chmod` や `chown` は絶対に行わない。
- マウント伝播を利用するため、mount 共有の親ディレクトリが `shared` に設定されていることを事前に確認する。条件を満たさない場合でも、ホストの `/` 全体を勝手に再設定してはいけない。
- `docker compose down -v`、volume 削除、状態ディレクトリの削除、Main 状態の流用を行ってはいけない。
- 再作成前に `docker compose config` で解決済み設定を確認し、設定・データ・ログ・sidecar 用ディレクトリの Development bind source が現在のリポジトリの `docker/development/state/` 配下を正しく指していることを確認する。
- 再作成後にコンテナの Compose working directory、設定ファイル、bind source、イメージ ID、再起動回数を確認する。
- Development API と画面が返す Git commit が現在のワークツリーと一致し、Main コンテナの状態・エンコーダー構成・API 応答が変化していないことを確認する。

- **生成物専用 spool の共有境界と保護**:
  - `konomitv-bs4k-cloud-generated-source/` は、公開目録・変更記録・削除マーカー・完成した解析結果など、サイズ既知の通常ファイルを sidecar へ渡すための専用 spool ディレクトリです。本体からは既存 `DATA_DIR` 配下の通常パスとして書き込み、sidecar からは `/cloud-generated-source/` への read-only bind として参照されます。
  - このディレクトリに DB、JWT、OAuth 設定、暗号化鍵、`.env` などを格納してはいけません。また、`DATA_DIR` 全体を sidecar へ共有してはいけません。
  - 録画原本の転送には引き続き録画フォルダの read-only 共有を使用します。本 spool の固定 bind は録画共有定義の生成と独立しており、`.env` や生成済みの録画共有定義ファイルを上書き・変更するものではありません。
  - 本 bind は `create_host_path: false` で定義されているため、初回準備または更新時に未作成の場合は、Compose の `KONOMITV_UID` / `KONOMITV_GID` に合わせて mode 0700 で作成してから再作成を行います（既存ディレクトリの初期化や再帰的な `chmod` / `chown` は行わない）。
  - 設定確認時は本 bind の `source`、`target`、`read_only`、`create_host_path` の定義のみを確認し、設定全文、認証情報、暗号化鍵、生成物本文を表示させないでください。

## cloud-storage sidecar の運用と生存確認

- **権限分離とセキュリティ**:
  - FUSE デバイス (`/dev/fuse`)、`SYS_ADMIN` および `DAC_READ_SEARCH` ケイパビリティ、`security_opt: [apparmor=unconfined]` は `cloud-storage` sidecar サービスだけに付与する。mode 0700 の子マウント先ディレクトリ探索のために `DAC_READ_SEARCH` を使用し、`DAC_OVERRIDE` は付与しない。本体 `konomitv` にはこれらの権限を付与しない。
  - sidecar の常駐プロセス（監督スクリプトおよび rclone）は非 root ユーザーで実行し、マウント操作には setuid ヘルパーである `fusermount3` を使用する（実行ユーザーは製品 Dockerfile 内で Compose の `KONOMITV_UID` / `KONOMITV_GID` に合わせて定義される）。
  - sidecar に `privileged`、Docker socket のマウント、ホスト TCP ポートの公開は不要であり、付与してはいけない。
- **IPC とプロセス回収**:
  - 本体と sidecar の通信は、専用の Unix domain socket (`/run/konomitv-bs4k-cloud/supervisor.sock`) を経由して行う。
  - sidecar は自身が所有する rclone プロセスの終了を確認してから古い socket とマウントを回収する。運用中に稼働中の socket や `.active` マーカーファイルを手動で削除してはいけない。
- **生存確認 (Health Check) の範囲**:
  - Unix socket に対する `GET /health` の HTTP 200 (`{"ready": true}`) は、sidecar 内の監督プロセスの生存のみを示す。クラウドストレージへの到達性や録画データの再生成功を証明するものではない。
- **展開確認時の非破壊性**:
  - デプロイや展開の確認において、実接続の起動 (`/start`)、実際の鍵取り出し、認証の再取り込みや解除、録画ファイルの削除をエージェントが手動実行する必要はない。通常の接続確認 API が必要に応じて内部で接続別 rclone を起動・検証する。

## 録画フォルダの read-only 共有定義の生成と適用

録画原本をクラウドストレージへ移動・アップロードする際、`cloud-storage` sidecar サービスへ録画領域を共有する必要があります。
KonomiTV-BS4K では、`config.yaml` の `video.recorded_folders` を正本とし、`.env` に録画パスを二重登録することなく、Compose 継承用の共有定義ファイルを自動生成して適用します。

### 1. 基本方針

- root `.env` の `COMPOSE_FILE` 設定は維持し、変更しません。
- 録画パスの追加・変更は `config.yaml` の `video.recorded_folders` だけで行います。
- 共有定義は Git 管理外のデータディレクトリ内に出力されます。
  - Development: `docker/development/state/data/konomitv-bs4k-recording-binds.yaml`
  - 公開・Main 環境: `server/data/konomitv-bs4k-recording-binds.yaml`
- 生成される bind はすべて `read_only: true` かつ `create_host_path: false` であり、外部の録画領域に対する SELinux relabel (`:z`) や `chmod` / `chown` などの属性変更は一切行いません。
- 設定外のディスクやホスト全体 (`/`)・HOME 全体へ共有範囲を広げないでください。

### 2. 共有定義ファイルの生成

本体 `konomitv` および `cloud-storage` のビルド完了後、サーバーを起動しない一時的な `konomitv` コンテナを実行して定義ファイルを生成します。

```bash
docker compose run --rm --no-deps --workdir /code/server \
  --entrypoint /code/server/thirdparty/Python/bin/python \
  konomitv -m poetry run python -m app.utils.KonomiTVBS4KRecordingBinds
```

### 3. Compose への適用手順

`.env` ファイルの編集は不要です。実行時環境変数 `KONOMITV_BS4K_RECORDING_BINDS_FILE` に、生成された定義ファイルのホスト側絶対パスを指定して設定確認と再作成を行います。

```bash
# 生成された定義ファイルのホスト絶対パスを取得
BINDS_FILE="$(pwd)/docker/development/state/data/konomitv-bs4k-recording-binds.yaml"

# 1. 解決済み設定の確認（録画 bind の項目だけを出力）
KONOMITV_BS4K_RECORDING_BINDS_FILE="$BINDS_FILE" docker compose config --format json \
  | jq '[.services["cloud-storage"].volumes[] | select(.target | startswith("/host-rootfs/")) | {source, target, read_only}]'

# 2. コンテナの再作成
KONOMITV_BS4K_RECORDING_BINDS_FILE="$BINDS_FILE" docker compose up -d --no-build --force-recreate konomitv cloud-storage
```

### 4. 未指定時の挙動

`KONOMITV_BS4K_RECORDING_BINDS_FILE` を指定しない場合でも、追跡されている空の定義ファイル (`docker/cloud-storage/recording-binds.empty.yaml`) が自動参照されるため、Compose はエラーなく起動します。
ただし、この状態では sidecar へ録画領域が共有されないため、クラウドへの録画移動は行えません（アップロード準備が整っていることを意味しません）。

### 5. 運用上の留意点

- `video.recorded_folders` を変更した場合は、共有定義の再生成とコンテナの再作成を必ず行ってください。
- `docker compose config` の確認時は、録画フォルダに対応する `source`、`target`、および `read_only: true` が正しく設定されていることのみを確認し、設定ファイル全文、認証情報、暗号化鍵、録画ファイル一覧などの不要な出力を行わないでください。

## Dockerfileについて

できるだけnalaを活かして並列ダウンロードを試みる。
ただし、nalaにない機能やnalaのバグを踏む場合はaptやapt-getを使用する。
