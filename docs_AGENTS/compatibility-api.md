# 互換 API について

互換 API（`compatibility_api`）に関わる変更、または本線の大きな変更で互換 API への影響がありうる作業の前に読むこと。

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
