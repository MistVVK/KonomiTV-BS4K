# AGENTS-BS4K.md

## プロジェクト固有の注意事項

AGENTS.mdよりもAGENTS-BS4K.mdに記載された事項のほうが優先される。
矛盾する場合は、AGENTS-BS4K.mdが優先。

### ブラウザ検証

UI などのデバッグで Web ブラウザを使用するときは、`Chrome DevTools MCP` と Firefox (`Playwright` または `Firefox DevTools MCP`) の双方で動作確認する。
実際の検証 URL はマシン固有の指示を参照し、Git 管理下のファイルへ記載しない。

### 公開情報の保護

- 実ドメイン、個人ホームの絶対パス、ローカル IP アドレス、リバースプロキシ設定など、マシン固有の情報を Git 管理下の文書・コード・コミットへ含めない。
- マシン固有の Main / Development URL と配置場所は、Git 管理外のユーザー指示を参照する。
- push 前には、origin の先頭と push するコミットの間にあるすべてのコミットのコード全体と未コミット差分を対象に、これらの情報が混入していないことを確認する。差分のみならず各コミット時点のファイル内容全体を検査し、途中で追加・削除された情報も検出する。

### 巨大ファイルと例外処理の方針

- upstream 由来で差分がほぼゼロの巨大ファイルは、upstream merge 容易性を優先し、行数だけを理由に分割しない。
- BS4K 独自の新しい責務を既存の巨大関数へ無制限に積み増さない。独立した責務とライフサイクルを持つ場合だけモジュールやクラスへ分ける。
- 新しいコードで沈黙する `except Exception: pass` を追加しない。例外を無視する必要がある場合は `ProcessLookupError` / `OSError` など具体的な例外へ限定する。
- 正常なフォールバック (ファイル不在など) は必要に応じて debug、利用者が調査すべき異常は warning 以上で記録する。
- broad exception が不可避な場合は、具体的な例外へ限定できない根拠とフォールバック契約をコメントする。

## ターゲット

Docker Linux オンリーとする。

## 参照ファイル

以下の条件に合致する作業を始める前に、必ず該当ファイルを読むこと。
詳細な運用指示は docs_AGENTS/ 以下に分離してあり、このファイルには常時必要な指示だけを残す。

| 条件 | ファイル |
|------|---------|
| リポジトリ内のディレクトリ構成を把握する必要がある作業 | `docs_AGENTS/directory-structure.md` |
| Development コンテナのビルド・再作成・再起動・停止・削除、volume や状態ディレクトリの操作、Dockerfile の変更 | `docs_AGENTS/docker-development.md` |
| イメージのビルド設定 (CUDA / NONFREE) や再配布に関わる作業 | `docs_AGENTS/build.md` |
| コミットの作成・ステージング・ブランチ操作・upstream からの取り込み、どのブランチで作業すべきかの判断 | `docs_AGENTS/git.md` |
| 新機能の命名・製品名表記・バージョン情報に関わる作業 | `docs_AGENTS/naming.md` |
| 互換 API に関わる変更、本線の大きな変更 | `docs_AGENTS/compatibility-api.md` |
| 設計上の却下判断が確定したとき | `docs_AGENTS/rejected-proposals.md` に記録（skill: `record-rejected-proposal`） |
