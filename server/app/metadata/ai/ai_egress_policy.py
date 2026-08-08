"""AI Web tool の host network 到達を制御する egress 方針。

F-03 / R-08:
    Docker 版 KonomiTV-BS4K は `network_mode: host` で動作し、現行の Docker
    seccomp では unprivileged user+net namespace (`unshare`) が EPERM になる。
    Landlock launcher も filesystem access のみを扱い、接続先 IP を kernel 層で
    制限できない。

    そのため episode 経路は当面「検索専用モード」とする。

    - websearch: 許可
      OpenCode は Exa のホスト型検索 API のみを使い、検索結果 URL を KonomiTV
      ホストから直接取得しない。ACP は provider-hosted 検索だけを permission
      で one-shot 許可する。
    - webfetch / standalone URL fetch: 拒否
      任意 URL への接続は host loopback・LAN・metadata への SSRF になり得る。
    - bash / terminal / filesystem / credential: 拒否（既存どおり）

    専用 netns と DNS pinning 付き egress proxy が完成したあとで webfetch を
    再許可する。事後の citation 検査は side effect を取り消せないため、
    任意 URL 取得の代替にはしない。

    公開 Web の任意取得を再許可する条件:
    1. AI subprocess / serve を host network から分離した netns で起動できる
    2. egress proxy が DNS 解決結果を固定し、接続前と redirect ごとに
       loopback / private / link-local / metadata を拒否する
    3. Unix socket 経由の host 到達も遮断される
"""

from __future__ import annotations

import ipaddress
from typing import Final


# cloud / link-local metadata など、hostname だけで拒否すべき予約名。
_BLOCKED_LITERAL_HOSTNAMES: Final[frozenset[str]] = frozenset({
    'localhost',
    'metadata',
    'metadata.google.internal',
})


def AreAIEpisodeWebSearchToolsEnabled() -> bool:
    """episode の provider-hosted websearch を許可するかを返す。

    Returns:
        検索専用モードでは常に True。
    """

    return True


def AreAIEpisodeWebFetchToolsEnabled() -> bool:
    """episode の任意 URL 取得 (webfetch / standalone fetch) を許可するかを返す。

    Returns:
        専用 egress 分離が完成するまでは常に False。
        単体テストだけが monkeypatch で True に差し替えてよい。
    """

    # 既定は拒否。netns + egress proxy が揃うまで True へ戻さない。
    return False


def BuildOpenCodeEpisodeToolPermissions() -> dict[str, bool]:
    """OpenCode episode session に渡す tool 有効/無効マップを返す。

    Returns:
        websearch のみ True、webfetch は False の辞書。
    """

    return {
        'websearch': AreAIEpisodeWebSearchToolsEnabled(),
        'webfetch': AreAIEpisodeWebFetchToolsEnabled(),
    }


def IsAllowedAIEgressAddress(address: str) -> bool:
    """AI egress の接続先として許可できる公開 IP かを判定する。

    DNS pinning 付き proxy が完成したあとも、接続前と redirect ごとに
    この判定を通す。現状の検索専用モードとは独立した共通ポリシー。

    Args:
        address: リテラル IP 文字列。

    Returns:
        loopback / private / link-local / reserved / multicast 等を含まない
        グローバルユニキャストなら True。
    """

    try:
        parsed_address = ipaddress.ip_address(address)
    except ValueError:
        return False

    # IPv4-mapped IPv6 は内側の IPv4 へ戻し、::ffff:127.0.0.1 などを弾く。
    if isinstance(parsed_address, ipaddress.IPv6Address) and parsed_address.ipv4_mapped is not None:
        parsed_address = parsed_address.ipv4_mapped

    # クラウド metadata の link-local 代表値を明示拒否する。
    if str(parsed_address) in {'169.254.169.254', 'fd00:ec2::254'}:
        return False

    return (
        parsed_address.is_global
        and not parsed_address.is_private
        and not parsed_address.is_loopback
        and not parsed_address.is_link_local
        and not parsed_address.is_reserved
        and not parsed_address.is_multicast
        and not parsed_address.is_unspecified
    )


def IsBlockedAIEgressHostname(hostname: str) -> bool:
    """DNS 解決前に hostname だけで拒否すべき内部用途名かを判定する。

    Args:
        hostname: URL または destination から取り出したホスト名。

    Returns:
        localhost・metadata 等の内部用途なら True。
    """

    normalized = hostname.strip().rstrip('.').lower()
    if normalized == '':
        return True
    if normalized in _BLOCKED_LITERAL_HOSTNAMES:
        return True
    if normalized.endswith((
        '.localhost',
        '.local',
        '.internal',
        '.lan',
        '.home',
        '.arpa',
        '.invalid',
        '.onion',
        '.test',
    )):
        return True
    return False
