# AGENTS-BS4K.md

## プロジェクト固有の注意事項

AGENTS.mdよりもAGENTS-BS4K.mdに記載された事項のほうが優先される。
矛盾する場合は、AGENTS-BS4K.mdが優先。

### ブラウザ検証

UIなどのデバッグでwebブラウザを使用するときは、 `Chrome DevTools MCP` と `Playwright(Firefox)` の双方で動作確認する。

### 製品名・識別子の命名規則

- 利用者から見える製品名は `KonomiTV-BS4K` とする。
- WebUI、PWA、ページタイトル、ナビゲーション、マイページ、CLI 出力、起動ログ、キャプチャ画像の EXIF、CM 解析 YAML、メインのバージョン情報 API など、利用者が目にする表示や外部出力では `KonomiTV-BS4K` を使用する。
- 今後、upstream KonomiTV に存在しない新機能や要素を KonomiTV-BS4K で独自に追加する場合は、外部出力だけでなく、ファイル名、スキーマ名、クラス名、関数名、定数名、内部識別子を含めて `KonomiTV-BS4K` または言語・形式に適した `KonomiTVBS4K` / `konomitv-bs4k` を使用する。
- upstream KonomiTV に存在する機能や要素では、upstream 互換性とマージ容易性を維持するため、API パス、DB テーブル名、LocalStorage キー、Python の内部モジュール名などに含まれる既存の `KonomiTV` を原則として維持する。
- 新機能や要素が upstream に存在するか不明な場合は、命名前に `upstream/master` の同じパスや履歴を確認する。BS4K 独自の新機能や要素であることを確認できた場合は、upstream 互換を理由として `KonomiTV` の名前を使用しない。
- Komorebi など upstream KonomiTV 向けクライアントに公開する互換 API では、KonomiTV としての互換性を維持する。

### アプリケーションバージョン

- upstream KonomiTV のバージョンと KonomiTV-BS4K のバージョンは、単一の文字列へ結合せず別々に管理する。
- upstream 側の既存 `VERSION` は upstream KonomiTV のバージョンとして維持し、upstream 取り込み時に更新する。
- KonomiTV-BS4K 固有のバージョンは `BS4K_VERSION` として管理する。最初の KonomiTV-BS4K バージョンは `1.0.0` とする。
- upstream の Git コミットはバージョン情報として保持・表示しない。
- WebUI などでは `KonomiTV-BS4K 1.0.0` と `upstream: KonomiTV 0.14.1` のように、両方のバージョンが分かる形で表示する。
- KonomiTV-BS4K の Git タグと更新確認は、BS4K 側のバージョンだけを使う。
- メインのバージョン情報 API では、BS4K 側を `version`、upstream 側を `upstream_version` として別々に返す。
- upstream 互換 API の既存 `version` には upstream 側の `VERSION` を返す。

## 互換 API について

KonomiTV-BS4K は次の二層を分ける。

1. **本線（BS4K WebUI とその API / 再生経路）**
   BS4K 向けに効率・機能を優先してよい。
   Komorebi など upstream KonomiTV クライアント互換はここでは要求しない。

2. **互換 API（`compatibility_api`、現状プロファイル `KomorebiV1`）**
   upstream KonomiTV 相当の API 面を別ポート等で提供し、Komorebi 等の互換クライアント向け契約を維持する。
   既存のパス・レスポンス形・認証挙動・`version`（upstream 側 `VERSION`）などを不用意に変えない。

新機能や本線の大きな変更を入れるときは、互換 API 経路が壊れていないことを確認する
（少なくとも `server/tests/test_compatibility_api.py` 等の互換関連テスト）。
本線の最適化のために互換 API の契約を緩めない。逆に、互換 API のために本線を upstream 形状に縛らない。

## ターゲット

Docker Linuxオンリーとする

## Dockerfileについて

できるだけnalaを活かして並列ダウンロードを試みる。
ただし、nalaにない機能やnalaのバグを踏む場合はaptやapt-getを使用する。

## 再配布について

AMDのプロプライエタリを混ぜないDocker buildはffmpeg（libx265）などのGPL3+と両立するようにしたい。
AMDのプロプライエタリを入れる選択の場合はユーザがビルドしてるのでセーフという立場をとる。

## konomitv buildについて

- CUDAバージョン
  - 12.4
  - 12.8
- NONFREE
  - TRUE
  - FALSE

## Git コミットメッセージ規則

upstream KonomiTV の履歴形式に寄せつつ、日本語の題名で書く。
コミットは論理的な部品ブロックごとに分ける（無関係な変更を 1 コミットに混ぜない）。

### 1 行目の形

```text
種別: [領域…] 簡潔な日本語の題名
```

- **種別**（先頭は必ず次のいずれか。末尾にコロンと半角スペース）
  - `Fix:` … 不具合修正、回帰防止、セキュリティ硬化、誤動作の是正
  - `Add:` … 新機能・新ファイル・新 API・新しい対応範囲の追加
  - `Update:` … 既存の改善、整備、追従、整理、設定やドキュメントの更新、upstream 取り込み
- **領域タグ**（角括弧。変更の所在が分かるように 1 つ以上。複数可）
  - 大枠: `[Server]` `[Client]` `[Docker]` `[Docs]` `[GitHub]` `[Upstream]` など
  - 必要ならモジュールを続ける: `[Streams/LiveEncodingTask]` `[Utils/HostPath]` `[Player]` `[Routers/SettingsRouter]` など
  - パスはリポジトリ内の実体に合わせ、`Server` / `Client` は先頭大文字（upstream と同じ）
- **題名**は日本語。何をしたかが 1 行で分かること。句点は付けないことが多い

### 2 行目以降

- 必要なら空行のあと、変更理由・影響範囲・注意点を書く
- ログメッセージ（アプリの runtime log）は英語のまま（既存のコーディング規約に従う）

### 種別の判断

題名の語感だけでなく、**そのコミットが触るコード／差分**から判断する。

| 向き | 例 |
|---|---|
| `Fix:` | クラッシュ修正、リーク回収、バリデーション強化、表示崩れ修正、権限・symlink 保護 |
| `Add:` | 新エンドポイント、新 UI、新ユーティリティ、新形式への移行で能力が増える変更 |
| `Update:` | リファクタ、設定整理、依存・Docker 追従、ドキュメント追加、テスト追従、upstream merge |

迷ったら:

1. ユーザーから見える不具合や危険が直る → `Fix:`
2. 以前できなかったことができる → `Add:`
3. それ以外の改善・追従 → `Update:`

### 良い例

```text
Fix: [Server][Utils/LogRotation] LogRotation を symlink 攻撃と権限劣化から保護する
Fix: [Client][Player] 視聴クライアントの PlayerController 世代管理を直列化する
Add: [Server][Client] BS4K向けストリーミング設定とTS解析を追加する
Update: [Docs] AGENTS.md と AGENTS-BS4K.md を追加する
Update: [Upstream] upstream/master の更新を取り込む
```

### 避けること

- 種別なしの日本語だけの 1 行（例: `録画再生を改善する`）
- 小文字だけの conventional commits のみ（例: `fix: ...` / `feat: ...`）。BS4K では `Fix:` / `Add:` / `Update:` を使う
- 領域タグなしで Server と Client をまたぐ大きな変更を 1 行に押し込む（タグで所在を示す）
- 1 コミットに無関係な複数テーマを詰め込む

### ステージング

- 原則としてファイル丸ごとの `git add <file>` は使わず、変更の意味ごとに hunk / 行単位でステージする
- 新規ファイル全体・削除全体・ドキュメントの置き換えなど、ファイル単位が自然な場合は例外としてよい
- コミット前に `git diff --cached` で意図した差分だけが staged か確認する

### ブランチの使い分けと運用ルール

基本となるブランチ構成と運用ルールは以下の通り。

1. **`main` ブランチ (`origin/main`)**
   - KonomiTV-BS4K の基本・デフォルトブランチ。
   - BS4K 独自の機能拡張、最適化、互換 API など、プロダクトとしての最終的なコードが集約される。

2. **`upstream-fix` ブランチ (`origin/upstream-fix`)**
   - 上流（`tsukumijima/KonomiTV`）由来のバグ修正・共通改善を行う専用ブランチ。
   - `main` には上流へ還元できない BS4K 固有の変更が多く含まれるため、上流バグの修正は本ブランチで実施する。
   - **運用要件**:
     - その都合上、**`upstream/master` から作成（ブランチを切る）すること**。
     - `origin/main` と `upstream/master` の双方へクリーンにマージ（または Pull Request）できる状態を維持する。BS4K 固有のコードや依存関係を一切混入させてはならない。
   - 修正完了後は `main` へマージするとともに、必要に応じて上流本家への PR に活用する。

3. **`upstream` リモート・関連ブランチ (`upstream/master` 等)**
   - 上流リポジトリ（`https://github.com/tsukumijima/KonomiTV.git`）のコードベース。
   - 上流側の新機能やバグ修正を取り込む際の参照元として使用する。
   - upstream からの取り込みコミットは Git コミット規約に従い `Update: [Upstream] upstream/master の更新を取り込む` とする。
