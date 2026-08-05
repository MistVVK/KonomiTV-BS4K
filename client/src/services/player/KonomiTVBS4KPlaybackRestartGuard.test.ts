

import { describe, expect, it } from 'vitest';

import KonomiTVBS4KPlaybackRestartGuard from '@/services/player/KonomiTVBS4KPlaybackRestartGuard';


describe('KonomiTVBS4KPlaybackRestartGuard', () => {
    it('同一再生対象・codec tuple の最初の自動再起動だけを許可する', () => {
        const guard = new KonomiTVBS4KPlaybackRestartGuard();

        expect(guard.requestAutomaticRestart('Video:1:FFmpeg:av1:10:opus', 1_000)).toBe(true);
        expect(guard.requestAutomaticRestart('Video:1:FFmpeg:av1:10:opus', 30_000)).toBe(false);
    });

    it('再生対象または実効 codec tuple が変われば別の復旧枠として扱う', () => {
        const guard = new KonomiTVBS4KPlaybackRestartGuard();

        expect(guard.requestAutomaticRestart('Video:1:FFmpeg:av1:10:opus', 1_000)).toBe(true);
        expect(guard.requestAutomaticRestart('Video:2:FFmpeg:av1:10:opus', 2_000)).toBe(true);
        expect(guard.requestAutomaticRestart('Video:2:FFmpeg:av1:10:aac', 3_000)).toBe(true);
    });

    it('同一 pipeline でも60秒以上安定した後の単発エラーでは再び復旧を試す', () => {
        const guard = new KonomiTVBS4KPlaybackRestartGuard();

        expect(guard.requestAutomaticRestart('Live:gr011:QSV:hevc:10:opus', 1_000)).toBe(true);
        expect(guard.requestAutomaticRestart('Live:gr011:QSV:hevc:10:opus', 61_000)).toBe(true);
    });
});
