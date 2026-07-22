from __future__ import annotations

import asyncio


# Series 判定と Episode 判定は別のキューで実行するが、同じ RecordedProgram と
# AI 日次上限を更新する。外部 API 応答の待機中に手動訂正が交差しても、
# 管理者の確定判断が必ず最後に残るよう共通 lock で commit まで直列化する。
RECORDED_SERIES_RESOLUTION_LOCK = asyncio.Lock()
