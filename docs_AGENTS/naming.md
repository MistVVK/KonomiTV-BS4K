# 製品名・識別子の命名規則とバージョン管理

新機能の命名、外部出力への製品名表記、バージョン情報に関わる作業の前に読むこと。

## 製品名・識別子の命名規則

- 利用者から見える製品名は `KonomiTV-BS4K` とする。
- WebUI、PWA、ページタイトル、ナビゲーション、マイページ、CLI 出力、起動ログ、キャプチャ画像の EXIF、CM 解析 YAML、メインのバージョン情報 API など、利用者が目にする表示や外部出力では `KonomiTV-BS4K` を使用する。
- 今後、upstream KonomiTV に存在しない新機能や要素を KonomiTV-BS4K で独自に追加する場合は、外部出力だけでなく、ファイル名、スキーマ名、クラス名、関数名、定数名、内部識別子を含めて `KonomiTV-BS4K` または言語・形式に適した `KonomiTVBS4K` / `konomitv-bs4k` を使用する。
- upstream KonomiTV に存在する機能や要素では、upstream 互換性とマージ容易性を維持するため、API パス、DB テーブル名、LocalStorage キー、Python の内部モジュール名などに含まれる既存の `KonomiTV` を原則として維持する。
- 新機能や要素が upstream に存在するか不明な場合は、命名前に `upstream/master` の同じパスや履歴を確認する。BS4K 独自の新機能や要素であることを確認できた場合は、upstream 互換を理由として `KonomiTV` の名前を使用しない。
- Komorebi など upstream KonomiTV 向けクライアントに公開する互換 API では、KonomiTV としての互換性を維持する。

## アプリケーションバージョン

- upstream KonomiTV のバージョンと KonomiTV-BS4K のバージョンは、単一の文字列へ結合せず別々に管理する。
- upstream 側の既存 `VERSION` は upstream KonomiTV のバージョンとして維持し、upstream 取り込み時に更新する。
- KonomiTV-BS4K 固有のバージョンは Git タグ `bs4k-v*` から自動解決する（実装: `server/app/bs4k_version.py` → `BS4K_VERSION` / version API の `version`）。
  - タグちょうど（clean）: `1.1.1`
  - タグより先のコミット、または dirty: `1.1.1-dev`
  - マッチするタグが無い: `0.0.0-dev`
  - `.git` が無いイメージなどでは環境変数 `KONOMITV_BS4K_VERSION` をフォールバックとして使う
- 定数へのハードコードや `client/package.json` の版数を WebUI 表示の正本にしない。WebUI は version API の `version`（`display_version`）を表示する。
- upstream の Git コミットはバージョン情報として保持・表示しない。
- WebUI などでは `KonomiTV-BS4K 1.1.1` と `upstream: KonomiTV 0.14.1` のように、両方のバージョンが分かる形で表示する。
- KonomiTV-BS4K の Git タグと更新確認は、BS4K 側の `bs4k-v*` タグだけを使う（upstream の `v0.x` と混同しない）。
- メインのバージョン情報 API では、BS4K 側を `version`、upstream 側を `upstream_version` として別々に返す。
- upstream 互換 API の既存 `version` には upstream 側の `VERSION` を返す。
