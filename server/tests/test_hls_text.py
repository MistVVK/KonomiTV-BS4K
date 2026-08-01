
from app.utils.HLSText import sanitizeHLSQuotedString


def test_sanitize_hls_quoted_string_removes_control_and_quotes() -> None:
    """quote・backslash・CR/LF/C0/C1 control を除去して 1 行の安全な文字列になること。"""

    raw = 'ja\r\n"evil"\t\\x\x00title\u0085\u2028end'
    sanitized = sanitizeHLSQuotedString(raw)
    assert '\n' not in sanitized
    assert '\r' not in sanitized
    assert '"' not in sanitized
    assert '\\' not in sanitized
    assert '\x00' not in sanitized
    assert '\u0085' not in sanitized
    assert '\u2028' not in sanitized
    assert 'ja' in sanitized
    assert 'evil' in sanitized
    assert 'title' in sanitized
    assert 'end' in sanitized


def test_sanitize_hls_quoted_string_default_when_empty() -> None:
    """除去後に空なら default を返すこと。"""

    assert sanitizeHLSQuotedString(None, default='und') == 'und'
    assert sanitizeHLSQuotedString('\r\n\t', default='und') == 'und'
    assert sanitizeHLSQuotedString('"""', default='Audio 1') == 'Audio 1'


def test_master_playlist_structure_survives_hostile_metadata() -> None:
    """HLS MEDIA 行へ埋め込んでも改行や構造破壊が起きないこと。"""

    language = sanitizeHLSQuotedString('jpn\n#EXT-X-ENDLIST\u0085', default='und')
    name = sanitizeHLSQuotedString('主音声\r\nURI="http://evil"\u2029', default='Audio')
    line = (
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",'
        f'NAME="{name}",DEFAULT=YES,AUTOSELECT=YES,LANGUAGE="{language}",'
        'URI="audio/0/playlist"'
    )
    playlist = '#EXTM3U\n#EXT-X-VERSION:6\n' + line + '\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nindex.m3u8\n'
    # 行分断が起きないこと（MEDIA 行が 1 行で保たれる）
    assert line.count('\n') == 0
    assert line.count('\r') == 0
    assert '\u0085' not in line
    assert '\u2029' not in line
    # quote 注入による属性脱出が起きないこと
    assert 'URI="http://evil"' not in line
    assert line.startswith('#EXT-X-MEDIA:')
    assert line.endswith('URI="audio/0/playlist"')
    # 単純な行分割 parser でも MEDIA 行が 1 行として受理されること
    media_lines = [row for row in playlist.splitlines() if row.startswith('#EXT-X-MEDIA:')]
    assert len(media_lines) == 1
    assert 'LANGUAGE="jpn' in media_lines[0] or 'LANGUAGE="jpn"' in media_lines[0]
