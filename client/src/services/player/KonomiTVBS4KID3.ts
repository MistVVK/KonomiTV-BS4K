export interface IKonomiTVBS4KID3PrivateFrame {
    owner: string;
    payload: Uint8Array;
}


/** ID3 の synchsafe integer を読み取る。上位ビットが立った不正値は受理しない。 */
function readSynchsafeInteger(data: Uint8Array, offset: number): number | null {
    if (offset < 0 || offset + 4 > data.length) return null;
    if ((data[offset] | data[offset + 1] | data[offset + 2] | data[offset + 3]) >= 0x80) return null;
    return (
        (data[offset] << 21) |
        (data[offset + 1] << 14) |
        (data[offset + 2] << 7) |
        data[offset + 3]
    );
}


function readBigEndian32(data: Uint8Array, offset: number): number {
    return (
        data[offset] * 0x1000000 +
        (data[offset + 1] << 16) +
        (data[offset + 2] << 8) +
        data[offset + 3]
    );
}


/**
 * ID3 タグに含まれる PRIV frame を列挙する。
 *
 * ライブの Stream Anchor と ARIB-TTML は同じ timed-ID3 stream を owner で共有するため、
 * ここで一度だけ安全な frame 境界検査を実装する。
 */
export function extractKonomiTVBS4KID3PrivateFrames(
    data: Uint8Array,
): IKonomiTVBS4KID3PrivateFrame[] {
    const frames: IKonomiTVBS4KID3PrivateFrame[] = [];
    const latin1_decoder = new TextDecoder('iso-8859-1');

    for (let tag_begin = 0; tag_begin + 10 <= data.length;) {
        if (
            data[tag_begin] !== 0x49 ||
            data[tag_begin + 1] !== 0x44 ||
            data[tag_begin + 2] !== 0x33
        ) {
            // 古い FFmpeg が先頭5バイトを落としたデータへの既存 aribb24.js と同じ救済。
            if (tag_begin === 0 && data.length >= 15) {
                tag_begin += 5;
                continue;
            }
            break;
        }

        const version = data[tag_begin + 3];
        if (version !== 3 && version !== 4) break;
        const tag_size = readSynchsafeInteger(data, tag_begin + 6);
        if (tag_size === null) break;
        const tag_end = tag_begin + 10 + tag_size;
        if (tag_end > data.length) break;

        const tag_flags = data[tag_begin + 5];
        let frame_offset = tag_begin + 10;
        // 未解除の unsynchronisation は payload 自体を書き換えるため安全側でタグを捨てる。
        if ((tag_flags & 0x80) !== 0) break;
        if ((tag_flags & 0x40) !== 0) {
            if (frame_offset + 4 > tag_end) break;
            const extended_header_size = version === 4 ?
                readSynchsafeInteger(data, frame_offset) : readBigEndian32(data, frame_offset);
            // v2.3 のサイズ値は自分自身の4バイトを含まず、v2.4 は含む。
            const extended_header_total_size = extended_header_size === null ? 0 :
                extended_header_size + (version === 3 ? 4 : 0);
            if (
                extended_header_size === null || extended_header_size < 4 ||
                frame_offset + extended_header_total_size > tag_end
            ) {
                break;
            }
            frame_offset += extended_header_total_size;
        }

        while (frame_offset + 10 <= tag_end) {
            // ID3 の末尾 padding は0埋めなので、frame IDの先頭が0なら終了する。
            if (data[frame_offset] === 0) break;
            const frame_name = latin1_decoder.decode(data.subarray(frame_offset, frame_offset + 4));
            const frame_size = version === 4 ?
                readSynchsafeInteger(data, frame_offset + 4) : readBigEndian32(data, frame_offset + 4);
            if (frame_size === null || frame_size < 0) break;
            const payload_begin = frame_offset + 10;
            const payload_end = payload_begin + frame_size;
            if (payload_end > tag_end) break;

            if (frame_name === 'PRIV') {
                let owner_end = payload_begin;
                while (owner_end < payload_end && data[owner_end] !== 0) owner_end += 1;
                if (owner_end < payload_end) {
                    frames.push({
                        owner: latin1_decoder.decode(data.subarray(payload_begin, owner_end)),
                        payload: data.slice(owner_end + 1, payload_end),
                    });
                }
            }
            frame_offset = payload_end;
        }

        tag_begin = tag_end;
        if (
            (tag_flags & 0x10) !== 0 &&
            tag_begin + 10 <= data.length &&
            data[tag_begin] === 0x33 &&
            data[tag_begin + 1] === 0x44 &&
            data[tag_begin + 2] === 0x49
        ) {
            tag_begin += 10;
        }
    }
    return frames;
}
