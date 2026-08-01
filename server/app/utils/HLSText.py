
from __future__ import annotations

import re


# HLS quoted-string に入れて安全な可視文字以外、および quote / backslash を除去する。
# C0/C1 control・Unicode 行区切りが残ると playlist の行構造や parser 解釈が壊れる。
_UNSAFE_HLS_QUOTED_CHARS = re.compile(r'[\x00-\x1f\x7f-\x9f\u2028\u2029"\\]')


def sanitizeHLSQuotedString(value: str | None, *, default: str = '') -> str:
    """
    HLS の quoted-string 属性値として 1 行に安全な文字列へ正規化する。

    Args:
        value (str | None): FFprobe などから得た生の language / title など。
        default (str): value が空または除去後に空になったときの代替値。

    Returns:
        str: CR/LF/control・引用符・バックスラッシュを除いた安全な文字列。
    """

    if value is None:
        return default
    sanitized = _UNSAFE_HLS_QUOTED_CHARS.sub('', value).strip()
    return sanitized if sanitized != '' else default
