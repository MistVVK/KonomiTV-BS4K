import base64
import binascii
import html
import math
import re
from pathlib import Path

from ariblib.aribgaiji import GAIJI_MAP

from app import logging


def ReadRecordedWebVTTSidecar(recorded_file_path: Path) -> bytes | None:
    """
    録画先頭基準の同名 WebVTT を検査して読み、欠損・破損時は字幕なしとして返す。

    Args:
        recorded_file_path (Path): 録画本体のパス

    Returns:
        bytes | None: 元の WebVTT バイト列。利用できない場合は None
    """

    # パスは録画本体からだけ導出し、字幕 JSON に任意の読み取り先を保持しない。
    sidecar_path = recorded_file_path.with_suffix('.vtt')
    try:
        data = sidecar_path.read_bytes()
        text = data.decode('utf-8-sig')
        # BOM は許容するが、不正 UTF-8 や WebVTT でないファイルをトラック登録しない。
        # 署名行の任意テキストに cue 区切りの --> を含めることは WebVTT 仕様で禁止される。
        if re.match(r'\A(?![^\r\n]*-->)WEBVTT(?:[ \t][^\r\n]*)?(?:\r\n|\r|\n|\Z)', text) is None:
            raise ValueError('WebVTT signature is missing or invalid.')
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError) as ex:
        logging.warning(f'[RecordedWebVTT] Failed to read WebVTT sidecar: {sidecar_path}', exc_info=ex)
        return None
    return data


def ReadRecordedARIBSidecar(recorded_file_path: Path) -> list[tuple[float, float, bytes]]:
    """
    b24tovtt の字幕を録画先頭基準の ARIB8 PES payload に復元する。

    Args:
        recorded_file_path (Path): 同名 WebVTT を持つ録画本体

    Returns:
        list[tuple[float, float, bytes]]: 開始秒・継続秒・字幕 PES payload
    """

    data = ReadRecordedWebVTTSidecar(recorded_file_path)
    if data is None:
        return []
    try:
        text = data.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
        # 通常の WebVTT を ARIB 制御符号と誤認しない。WebVTT 配信用の原文は別途保持する。
        if 'b24caption-2aaf6fcf-6388-4e59-88ff-46e1555d0edd' not in text:
            raise ValueError('The WebVTT sidecar does not contain b24tovtt captions.')
        packets: list[tuple[float, float, bytes]] = []
        previous_end: float | None = None
        clear_payload = DecodeRecordedB24Caption('<c>%04%00%00%3F%={%1F%20%={%0C%=}%=}</c>')
        timestamp = r'(?:\d{2,}:)?[0-5]\d:[0-5]\d\.\d{3}'
        for block in re.split(r'\n[ \t]*\n', text):
            if not block.strip():
                continue
            lines = block.split('\n')
            # NOTE / STYLE / REGION と署名は字幕本文ではない。
            if not lines or lines[0].split(' ', 1)[0] in ('WEBVTT', 'NOTE', 'STYLE', 'REGION'):
                continue
            time_index = 0 if '-->' in lines[0] else 1
            if time_index >= len(lines):
                raise ValueError('A WebVTT cue has no timing line.')
            timing = re.fullmatch(rf'({timestamp})[ \t]+-->[ \t]+({timestamp})(?:[ \t].*)?', lines[time_index])
            if timing is None:
                raise ValueError('A WebVTT cue has invalid timing.')
            times: list[float] = []
            for value in timing.groups():
                seconds = 0.0
                for part in value.split(':'):
                    seconds = seconds * 60 + float(part)
                times.append(seconds)
            start, end = times
            duration = end - start
            # float変換は巨大な時刻をinfにし得る。JSON応答へ非有限値を持ち込まない。
            if not all(math.isfinite(value) for value in (start, end, duration)):
                raise ValueError('WebVTT cue times must be finite.')
            if end <= start or (packets and start < packets[-1][0]):
                raise ValueError('WebVTT cue times are not ordered.')
            # 次cueまでの空白と終端では字幕を消す。seek復元でも同じ消去packetを通す。
            if previous_end is not None and previous_end < start:
                packets.append((previous_end, 0.0, clear_payload))
            for line in lines[time_index + 1:]:
                if not line:
                    continue
                voice = re.fullmatch(r'<v b24caption([0-8])>(.*)</v>', line)
                if voice is None:
                    raise ValueError('A b24tovtt cue has invalid voice markup.')
                payload = DecodeRecordedB24Caption(voice[2])
                packets.append((start, duration, payload))
            previous_end = end
        if previous_end is not None:
            packets.append((previous_end, 0.0, clear_payload))
        return packets
    except (ValueError, UnicodeError, OverflowError) as ex:
        logging.warning('[RecordedWebVTT] Failed to decode ARIB sidecar captions.', exc_info=ex)
        return []


def DecodeRecordedB24Caption(markup: str) -> bytes:
    """
    b24tovtt の statement を ARIB8 に戻し、サイズと CRC を含む PES payload を生成する。

    Args:
        markup (str): b24caption1 voice の内部マークアップ

    Returns:
        bytes: data_identifier 0x80 で始まる字幕 PES payload
    """

    group = bytearray()
    # ariblib の放送外字表を逆引きし、旧表の代替表記だけを STD-B24 の Unicode 表記で補う。
    gaiji = {value: code for code, value in GAIJI_MAP.items() if len(value) == 1}
    gaiji.update({'➡': 0x7C21, '⬅': 0x7C22, '⬆': 0x7C23, '⬇': 0x7C24,
                  '⚞': 0x7D78, '⚟': 0x7D79, '\ue2fb': 0x7D7B})
    sizes: list[int] = []
    in_control = False
    # b24tovtt は可読部分以外を <c> に格納する。制御パラメーターを文字コード変換しない。
    for part in re.split(r'(<c>|</c>)', markup):
        if part == '<c>':
            if in_control:
                raise ValueError('Nested b24tovtt control markup.')
            in_control = True
            continue
        if part == '</c>':
            if not in_control:
                raise ValueError('Unbalanced b24tovtt control markup.')
            in_control = False
            continue
        if not part:
            continue
        part = html.unescape(part)
        if not in_control:
            # G1 英数を GL、G2 漢字を GR に割り当てる。SWF / CS 後も可読区間ごとに再指定する。
            group.extend(b'\x1b\x24\x2a\x42\x1b\x7d\x0e')
            for character in part:
                codepoint = ord(character)
                if 0xEC00 <= codepoint <= 0xEFFF:
                    # b24tovtt の DRCS 文字番号を DRCS-0 の区点へ戻す。
                    index = codepoint - 0xEC00
                    group.extend(b'\x1b\x24\x2b\x20\x40\x1d')
                    group.extend((index // 94 + 0x21, index % 94 + 0x21))
                # 旧外字表には「・」等の代替文字もあるため、標準JISで表現できる文字を優先する。
                elif character in gaiji and character.encode('euc_jp', errors='ignore') == b'':
                    group.extend((gaiji[character] | 0x8080).to_bytes(2, 'big'))
                else:
                    # JIS の波ダッシュと Unicode 全角チルダは同じ放送符号に対応する。
                    encoded = ('〜' if character == '～' else character).encode('euc_jp')
                    if encoded.startswith(b'\x8e'):
                        group.extend(b'\x1b\x2b\x31\x1d')
                        group.append(encoded[1] & 0x7F)
                    elif encoded.startswith(b'\x8f'):
                        raise ValueError('JIS X 0212 is not an ARIB caption character set.')
                    else:
                        group.extend(encoded)
            continue
        position = 0
        while position < len(part):
            if part[position] != '%':
                group.extend(part[position].encode('ascii'))
                position += 1
                continue
            escape = part[position + 1:position + 3]
            position += 3
            if escape == '={':
                if len(sizes) >= 8:
                    raise ValueError('Too many nested b24tovtt size fields.')
                sizes.append(len(group))
                group.extend(b'\x00\x00\x00')
            elif escape == '=}':
                if not sizes:
                    raise ValueError('Unbalanced b24tovtt size field.')
                offset = sizes.pop()
                group[offset:offset + 3] = (len(group) - offset - 3).to_bytes(3, 'big')
            elif escape == '+{':
                end = part.find('%+}', position)
                if end < 0:
                    raise ValueError('Unclosed b24tovtt base64 data.')
                group.extend(base64.b64decode(part[position:end], validate=True))
                position = end + 3
            elif len(escape) == 2 and escape[0] == '^' and '@' <= escape[1] <= '_':
                group.append(ord(escape[1]) + 0x40)
            elif re.fullmatch('[0-9a-fA-F]{2}', escape):
                group.append(int(escape, 16))
            else:
                raise ValueError('Invalid b24tovtt escape.')
            if len(group) > 65520:
                raise ValueError('ARIB subtitle data exceeds the PES size limit.')
    if in_control or sizes or not 7 <= len(group) <= 65520:
        raise ValueError('Incomplete b24tovtt caption data.')
    # データグループ番号は生成時に選択された一言語へ固定し、原文の group version は保持する。
    group[0] = (group[0] & 3) | (0 if group[0] & 0x7C == 0 else 4)
    # statement 内の DRCS データも UCS の文字番号から同じ DRCS-0 区点へ戻す。
    # サイズフィールドは検査し、壊れたパケットで別unitを走査しない。
    if group[0] & 0x7C:
        offset = 4 + (5 if group[3] >> 6 in (1, 2) else 0)
        if offset + 3 > len(group) or int.from_bytes(group[offset:offset + 3], 'big') != len(group) - offset - 3:
            raise ValueError('Invalid ARIB data unit loop size.')
        offset += 3
        while offset < len(group):
            if offset + 5 > len(group) or group[offset] != 0x1F:
                raise ValueError('Invalid ARIB data unit header.')
            parameter = group[offset + 1]
            end = offset + 5 + int.from_bytes(group[offset + 2:offset + 5], 'big')
            position = offset + 5
            if end > len(group):
                raise ValueError('Truncated ARIB data unit.')
            if parameter == 0x31:
                if position >= end:
                    raise ValueError('Empty ARIB DRCS unit.')
                count = group[position]
                position += 1
                for _ in range(count):
                    if position + 3 > end:
                        raise ValueError('Truncated ARIB DRCS code.')
                    index = int.from_bytes(group[position:position + 2], 'big') - 0xEC00
                    if not 0 <= index < 1024:
                        raise ValueError('Invalid b24tovtt DRCS code.')
                    group[position:position + 2] = bytes((index // 94 + 0x21, index % 94 + 0x21))
                    fonts = group[position + 2]
                    position += 3
                    for _ in range(fonts):
                        if position + 4 > end:
                            raise ValueError('Truncated ARIB DRCS font.')
                        mode = group[position] & 15
                        position += 1
                        if mode <= 1:
                            depth, width, height = group[position:position + 3]
                            position += 3 + ((depth + 1).bit_length() * width * height + 7) // 8
                        else:
                            if position + 4 > end:
                                raise ValueError('Truncated ARIB DRCS geometry.')
                            position += 4 + int.from_bytes(group[position + 2:position + 4], 'big')
                        if position > end:
                            raise ValueError('Truncated ARIB DRCS bitmap.')
            offset = end
    group[3:3] = (len(group) - 3).to_bytes(2, 'big')
    return b'\x80\xff\xf0' + group + binascii.crc_hqx(group, 0).to_bytes(2, 'big')
