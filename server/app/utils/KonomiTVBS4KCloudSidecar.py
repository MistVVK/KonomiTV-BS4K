"""専用コンテナ内で接続ごとのrcloneとUnix socketの寿命を所有する。"""

import fcntl
import json
import os
import signal
import socket
import socketserver
import stat
import subprocess
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from uuid import UUID


class KonomiTVBS4KCloudSidecar:
    """rcloneの起動・停止と、停止確認後のIPCおよびmount回収を直列化する。"""

    def __init__(self, control: Path, credentials: Path, mounts: Path) -> None:
        """sidecar専用の保存先を受け取る。認証の内容は読まない。

        Args:
            control: 本体と共有する非公開Unix socketディレクトリ。
            credentials: rcloneへ通常入力として渡す認証ディレクトリ。
            mounts: 本体へ伝播するmount専用ディレクトリ。
        Returns:
            None
        """
        # start()/stop()が扱うIPC・認証・mountの起点。いずれもコンテナの固定bind先。
        self.control = control
        self.credentials = credentials
        self.mounts = mounts
        # stop()で停止を確認する子プロセス。キーは所有者IDと接続UUIDからだけ生成する。
        self.children: dict[str, subprocess.Popen[bytes]] = {}
        control.mkdir(parents=True, exist_ok=True, mode=0o700)
        mounts.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 子にも継承し、監督プロセスだけが落ちても旧rcloneと二重起動させない。
        self.lock_fd = os.open(control / '.supervisor.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def connectionName(owner: object, connection: UUID) -> str:
        """IPCパスに利用できる接続名を作る。

        Args:
            owner: 正のDB所有者ID。
            connection: 接続UUID。
        Returns:
            パス区切りを含まない接続名。
        """
        if isinstance(owner, bool) or not isinstance(owner, int) or not 0 < owner < 2**63:
            raise ValueError('Invalid owner.')
        return f'{owner}_{connection}'

    def stop(self, name: str) -> None:
        """子の終了を確認してから残ったmountとsocketを回収する。

        Args:
            name: 内部で検証済みの接続名。
        Returns:
            None
        """
        child = self.children.get(name)
        if child is not None:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            del self.children[name]
        mount = self.mounts / name
        # stale FUSEのstatは失敗し得るため、カーネルのmount表で存在を確認する。
        mounted = [line.split()[4] for line in Path('/proc/self/mountinfo').read_text().splitlines()
                   if line.split()[4] == str(mount) or line.split()[4].startswith(f'{mount}/')]
        for target in sorted(mounted, key=len, reverse=True):
            subprocess.run(['fusermount3', '-uz', target], check=True, timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (self.control / f'{name}.sock').unlink(missing_ok=True)
        (self.control / f'{name}.active').unlink(missing_ok=True)

    def recover(self) -> None:
        """排他取得後に限り、前回停止後に残った接続のIPCとmountを回収する。

        Args:
            None
        Returns:
            None
        """
        for marker in self.control.glob('*.active'):
            owner, connection = marker.stem.split('_', 1)
            name = self.connectionName(int(owner), UUID(connection))
            if name != marker.stem:
                raise ValueError('Invalid recovery entry.')
            self.stop(name)

    def start(self, owner: int, connection: UUID) -> None:
        """通常の認証ファイルを使うrcloneを接続ごとに一つだけ起動する。

        Args:
            owner: 所有者ID。
            connection: 接続UUID。
        Returns:
            None。接続準備が確認できなければ例外。
        """
        name = self.connectionName(owner, connection)
        child = self.children.get(name)
        if child is not None and child.poll() is None:
            return
        self.stop(name)
        config = self.credentials / str(owner) / f'{connection}.conf'
        if not stat.S_ISREG(config.lstat().st_mode) or config.parent.is_symlink():
            raise ValueError('Invalid credential entry.')
        # 本体側のbindはread-onlyなので、mount先の作成もsidecarが所有する。
        mount = self.mounts / name
        mount.mkdir(mode=0o755, exist_ok=True)
        if mount.is_symlink() or not mount.is_dir():
            raise ValueError('Invalid mount directory.')
        endpoint = self.control / f'{name}.sock'
        # Unix socketの108-byte上限を超えてから起動失敗しないよう事前に拒否する。
        if len(os.fsencode(endpoint)) >= 108:
            raise ValueError('IPC path is too long.')
        marker = self.control / f'{name}.active'
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            self.children[name] = subprocess.Popen(
                ['rclone', '--config', str(config), '--ask-password=false',
                 '--contimeout', '10s', '--timeout', '30s', '--retries', '1', '--low-level-retries', '1',
                 'rcd', '--rc-addr', f'unix://{endpoint}', '--rc-no-auth', '--log-level', 'EMERGENCY'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                pass_fds=(self.lock_fd,),
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self.children[name].poll() is not None:
                    raise OSError('Rclone exited during startup.')
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.2)
                    try:
                        probe.connect(str(endpoint))
                    except OSError:
                        time.sleep(0.05)
                        continue
                return
            raise TimeoutError('Rclone startup timed out.')
        except (OSError, subprocess.SubprocessError):
            self.stop(name)
            raise


def RunKonomiTVBS4KCloudSidecar() -> None:
    """TCPを使わず本体専用Unix socketで監督APIを提供する。

    Args:
        None
    Returns:
        None
    """
    os.umask(0o077)
    service = KonomiTVBS4KCloudSidecar(
        Path('/run/konomitv-bs4k-cloud'), Path('/cloud-credentials'), Path('/cloud-mounts'),
    )
    service.recover()
    endpoint = service.control / 'supervisor.sock'
    endpoint.unlink(missing_ok=True)

    class Handler(BaseHTTPRequestHandler):
        """外部入力を固定の接続操作へ限定し、例外本文とアクセスログを返さない。"""

        def log_message(self, format: str, *args: object) -> None:
            """ローカル制御要求をアクセスログへ複製しない。

            Args:
                format: 標準ハンドラーの書式（未使用）。
                args: 標準ハンドラーの引数（未使用）。
            Returns:
                None
            """

        def setup(self) -> None:
            """ヘッダー送信前に止まったクライアントも有限時間で切断する。

            Args:
                None
            Returns:
                None
            """
            self.request.settimeout(5)
            super().setup()

        def do_GET(self) -> None:
            """監督APIだけの生存確認を返す。クラウド到達性を示すものではない。

            Args:
                None
            Returns:
                None
            """
            if self.path != '/health':
                self.send_error(404)
                return
            data = b'{"ready":true}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            """検証した接続の起動・停止だけを受け付ける。

            Args:
                None
            Returns:
                None
            """
            self.connection.settimeout(5)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096 or self.headers.get('Transfer-Encoding') is not None:
                    raise ValueError('Invalid request length.')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or not isinstance(body.get('connection_id'), str):
                    raise ValueError('Invalid request shape.')
                owner = body['owner_id']
                connection = UUID(body['connection_id'])
                name = service.connectionName(owner, connection)
                if self.path == '/start':
                    service.start(owner, connection)
                elif self.path == '/stop':
                    service.stop(name)
                elif self.path == '/mount-directory':
                    # フォルダ・鍵世代ごとのmount先を本体のread-only bind外側で作成する。
                    mount_id = body.get('mount_id')
                    if (not isinstance(mount_id, str) or len(mount_id) != 64 or
                        any(char not in '0123456789abcdef' for char in mount_id) or
                        name not in service.children or service.children[name].poll() is not None):
                        raise ValueError('Invalid mount request.')
                    directory = service.mounts / name / mount_id
                    directory.mkdir(mode=0o755, exist_ok=True)
                    if directory.is_symlink() or not directory.is_dir():
                        raise ValueError('Invalid mount directory.')
                else:
                    self.send_error(404)
                    return
                data = b'{"ready":false}' if self.path == '/stop' else b'{"ready":true}'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (ValueError, KeyError, TypeError):
                self.send_error(400)
            except (OSError, subprocess.SubprocessError):
                self.send_error(503)

    def Stop(signum: int, frame: object) -> None:
        """終了時はfinallyで子プロセスを回収する。

        Args:
            signum: 終了シグナル。
            frame: シグナルを受けたフレーム（未使用）。
        Returns:
            None。SystemExitで終了処理へ移る。
        """
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, Stop)
    signal.signal(signal.SIGINT, Stop)
    try:
        with socketserver.UnixStreamServer(str(endpoint), Handler) as server:
            server.serve_forever(poll_interval=0.2)
    finally:
        for name in list(service.children):
            service.stop(name)
        endpoint.unlink(missing_ok=True)
        os.close(service.lock_fd)


if __name__ == '__main__':
    RunKonomiTVBS4KCloudSidecar()
