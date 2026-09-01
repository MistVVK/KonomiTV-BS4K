/** 月曜始まりの曜日ラベル。API の weekday と一致させる。 */
export const WEEKDAY_LABELS = ['月', '火', '水', '木', '金', '土', '日'] as const;

/**
 * 28 時間表記の時を返す。0:00–3:59 は 24–27 時。
 * @param hour 自然時刻の時
 */
export function toDisplayHour(hour: number): number {
    return hour < 4 ? hour + 24 : hour;
}

/**
 * 28 時間表記で属する曜日を返す。0:00–3:59 は前日。
 * @param weekday 自然時刻の曜日（月曜=0）
 * @param hour 自然時刻の時
 */
export function toDisplayWeekday(weekday: number, hour: number): number {
    return hour < 4 ? (weekday + 6) % 7 : weekday;
}

/**
 * スロットの表示時刻。
 * @param hour 自然時刻の時
 * @param minute 5 分丸めした分
 */
export function formatSlotLabel(hour: number, minute: number): string {
    const display_hour = toDisplayHour(hour);
    return `${display_hour}:${String(minute).padStart(2, '0')}`;
}
