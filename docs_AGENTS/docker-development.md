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
- Development は `verified-runtime` target を使用する。通常のコード変更を未検証の `runtime` target だけで起動してはいけない。
- コード変更を Development で確認する必要があるときは、ユーザーへ再ビルド・再作成・再起動を依頼せず、エージェントが自ら実行する。
- ビルド中は既存の Development コンテナを稼働させ、ビルド成功後にコンテナだけを再作成して停止時間を最小化する。
- Development のビルドには次のコマンドを使用する（`.env` の `COMPOSE_FILE` が効く前提）。

```bash
docker compose build konomitv
```

- ビルド成功後の再作成には次のコマンドを使用する。

```bash
docker compose up -d --no-build --force-recreate konomitv
```

## 状態保護と事前確認

- `docker/development/state/` 以下の `config.yaml`、`data/`、`logs/`、`recordings/`、`captures/` を Development 専用状態として保持する。
- `docker compose down -v`、volume 削除、状態ディレクトリの削除、Main 状態の流用を行ってはいけない。
- 再作成前に `docker compose config` で解決済み設定を確認し、設定・データ・ログの Development bind source が現在のリポジトリの `docker/development/state/` を指していることを確認する。
- 再作成後にコンテナの Compose working directory、設定ファイル、bind source、イメージ ID、再起動回数を確認する。
- Development API と画面が返す Git commit が現在のワークツリーと一致し、Main コンテナの状態・エンコーダー構成・API 応答が変化していないことを確認する。

## Dockerfileについて

できるだけnalaを活かして並列ダウンロードを試みる。
ただし、nalaにない機能やnalaのバグを踏む場合はaptやapt-getを使用する。
