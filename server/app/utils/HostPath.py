from __future__ import annotations

import re
from pathlib import Path


DOCKER_HOST_ROOT = Path('/host-rootfs')
_DOCKER_HOST_ROOT_IN_TEXT_PATTERN = re.compile(
    r'(?<![A-Za-z0-9_./-])/host-rootfs(?=/|[^A-Za-z0-9_./-]|$)'
)


class HostPathError(ValueError):
    """ホストパスとDocker内部パスの境界に違反した入力を表す。"""


def _ReplaceDockerHostRootInText(match: re.Match[str]) -> str:
    """エラー本文中のDocker内部接頭辞を、対応するホスト側のルート表現へ置き換える。"""

    # 後続の / はホスト絶対パス側に残る。単独の接頭辞ではホストルートを表す / を補う。
    return '' if match.string[match.end():].startswith('/') else '/'


def _IsDockerEnvironment() -> bool:
    """
    現在の実行環境がDockerかを返す。

    Returns:
        bool: Docker環境ならTrue。
    """

    # 循環importを避け、テストから環境判定を差し替えられるよう遅延importする。
    from app.utils import GetPlatformEnvironment

    return GetPlatformEnvironment() == 'Linux-Docker'


def NormalizeHostPath(path_value: str | Path) -> Path:
    """
    外部から受け取ったパスをホスト絶対パスへ正規化する。

    Docker環境では旧クライアントが付けた先頭のDocker内部接頭辞を1回だけ受理する。
    パス中間の同名ディレクトリは変更せず、二重接頭辞と親ディレクトリ参照は拒否する。

    Args:
        path_value (str | Path): API・設定ファイル・DBで扱うパス。

    Returns:
        Path: 正規化済みのホスト絶対パス。

    Raises:
        HostPathError: 絶対パスでない、親ディレクトリ参照を含む、または接頭辞が二重の場合。
    """

    path = Path(path_value)
    if path.is_absolute() is False:
        raise HostPathError('ホスト側の絶対パスを指定してください。')
    if '..' in path.parts:
        raise HostPathError('親ディレクトリ参照を含むパスは指定できません。')

    # 非Docker環境では既存どおり入力パスをそのまま利用する。
    if _IsDockerEnvironment() is False:
        return path

    # POSIX では先頭がちょうど // のパスが通常の / と異なる anchor を持つ。
    # Docker 内部パスへ安全に相対化できない曖昧な絶対パスは、未捕捉例外にせず入力エラーとして拒否する。
    if path.anchor != '/':
        raise HostPathError('Docker環境ではPOSIX形式の絶対パスを指定してください。')

    # 旧クライアントの先頭接頭辞だけを1回外し、内部に同名部分を含むパスは保持する。
    if path == DOCKER_HOST_ROOT or path.is_relative_to(DOCKER_HOST_ROOT):
        host_path = Path('/') / path.relative_to(DOCKER_HOST_ROOT)
        if host_path == DOCKER_HOST_ROOT or host_path.is_relative_to(DOCKER_HOST_ROOT):
            raise HostPathError('Docker内部パスの接頭辞を重ねて指定することはできません。')
        return host_path
    return path


def ToRuntimePath(path_value: str | Path) -> Path:
    """
    ホスト絶対パスを現在の環境で実アクセスするパスへ変換する。

    Args:
        path_value (str | Path): API・設定ファイル・DBで保持するホスト絶対パス。

    Returns:
        Path: Dockerでは接頭辞付き、非Dockerでは入力と同じ実アクセス用パス。

    Raises:
        HostPathError: ホストパスとして安全に正規化できない場合。
    """

    host_path = NormalizeHostPath(path_value)
    if _IsDockerEnvironment():
        return DOCKER_HOST_ROOT / host_path.relative_to('/')
    return host_path


def ToHostPath(path_value: str | Path) -> Path:
    """
    現在の環境の実アクセス用パスを外部向けホスト絶対パスへ変換する。

    Args:
        path_value (str | Path): KonomiTV-BS4K内部で利用している実アクセス用パス。

    Returns:
        Path: API・設定ファイル・DBへ出せるホスト絶対パス。

    Raises:
        HostPathError: パスとして安全に正規化できない場合。
    """

    return NormalizeHostPath(path_value)


def ToUserHostPathText(text: str) -> str:
    """
    エラー本文中でパストークンの先頭に現れるDocker内部接頭辞を除去する。

    パス中間の同名ディレクトリは保持する。過去に二重化したエラー本文も外部へ
    再露出しないよう、同じパストークンの先頭から接頭辞がなくなるまで正規化する。

    Args:
        text (str): ログ・API・DBへ保存される可能性があるユーザー向け本文。

    Returns:
        str: Docker内部接頭辞を外部へ露出しない本文。
    """

    normalized_text = text
    while True:
        next_text = _DOCKER_HOST_ROOT_IN_TEXT_PATTERN.sub(_ReplaceDockerHostRootInText, normalized_text)
        if next_text == normalized_text:
            return normalized_text
        normalized_text = next_text
