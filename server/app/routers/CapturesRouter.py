
import errno
import shutil
from pathlib import Path
from typing import Annotated, BinaryIO, cast

import puremagic
from fastapi import APIRouter, File, HTTPException, UploadFile, status
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import logging
from app.config import Config


# ルーター
router = APIRouter(
    tags = ['Captures'],
    prefix = '/api/captures',
)

# 1 リクエストあたりのキャプチャ画像の累積バイト上限。
# スクリーンショット用途を超え、保存先や一時領域を枯渇させる巨大 upload を拒否する。
MAX_CAPTURE_UPLOAD_BYTES = 20 * 1024 * 1024

# multipart 境界・フィールドヘッダ分の余裕を加えた HTTP 本文上限。
# FastAPI が UploadFile を解析する前に ASGI 層で強制し、一時 spool による枯渇を防ぐ。
MAX_CAPTURE_REQUEST_BODY_BYTES = MAX_CAPTURE_UPLOAD_BYTES + (256 * 1024)

# 書き込み後も残すべき最低空き容量。最終フォルダでもこの余裕が無ければ保存しない。
_MIN_FREE_BYTES_AFTER_WRITE = 10 * 1024 * 1024

# copy 時の読み取り単位。上限判定と ENOSPC 時の途中ファイル削除をチャンク単位で行う。
_COPY_CHUNK_BYTES = 1024 * 1024

# Capture upload の path。trailing slash の有無どちらも対象にする。
_CAPTURE_UPLOAD_PATHS = frozenset({
    '/api/captures',
    '/api/captures/',
})


def _CopyUploadWithLimit(
    source: BinaryIO,
    destination: BinaryIO,
    max_bytes: int,
) -> int:
    """
    upload ストリームを destination へコピーし、累積バイト数を返す。

    Args:
        source (BinaryIO): 読み取り元の upload ストリーム。
        destination (BinaryIO): 書き込み先ファイル。
        max_bytes (int): 許可する最大バイト数。超過時は ValueError。

    Returns:
        int: 実際に書き込んだバイト数。

    Raises:
        ValueError: max_bytes を超えた場合。
        OSError: 書き込み中の OS エラー。
    """

    total_written = 0
    while True:
        chunk = source.read(_COPY_CHUNK_BYTES)
        if not chunk:
            break
        total_written += len(chunk)
        if total_written > max_bytes:
            raise ValueError('Capture upload exceeds the configured size limit.')
        destination.write(chunk)
    return total_written


class CaptureUploadBodyLimitMiddleware:
    """
    POST /api/captures の HTTP 本文を multipart 解析前に制限する ASGI ミドルウェア。

    FastAPI は UploadFile 依存を解決する前に本文全体を spool するため、
    endpoint 内の上限だけでは一時領域を枯渇させられる。Content-Length 先行拒否と
    receive ストリームの累積上限の両方で、認証前でも本文を制限する。
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int = MAX_CAPTURE_REQUEST_BODY_BYTES) -> None:
        # 後段 ASGI アプリ。通常は FastAPI 本体。
        self.app = app
        # POST /api/captures に許す HTTP 本文の最大バイト数。
        self.max_body_bytes = max_body_bytes

    @staticmethod
    def _GetHeader(scope: Scope, name: bytes) -> str | None:
        """小文字比較で最初に一致したヘッダー値を返す。"""

        for key, value in scope.get('headers', []):
            if key.lower() == name:
                return value.decode('latin-1')
        return None

    @staticmethod
    def _IsCaptureUpload(scope: Scope) -> bool:
        """Capture 画像 upload の対象リクエストかどうかを返す。"""

        if scope.get('type') != 'http':
            return False
        if scope.get('method') != 'POST':
            return False
        return scope.get('path') in _CAPTURE_UPLOAD_PATHS

    @staticmethod
    async def _EmptyReceive() -> Message:
        """本文を消費済みのときに JSONResponse へ渡す空 receive。"""

        return {
            'type': 'http.request',
            'body': b'',
            'more_body': False,
        }

    async def _SendTooLargeResponse(self, scope: Scope, send: Send) -> None:
        """413 JSON レスポンスを返す。"""

        response = JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content={'detail': 'Capture upload exceeds the 20 MiB limit'},
        )
        await response(scope, self._EmptyReceive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._IsCaptureUpload(scope) is False:
            await self.app(scope, receive, send)
            return

        content_length_header = self._GetHeader(scope, b'content-length')
        if content_length_header is not None:
            try:
                content_length = int(content_length_header.strip())
            except ValueError:
                # 不正な Content-Length は巨大本文と同じく拒否する
                content_length = self.max_body_bytes + 1
            if content_length > self.max_body_bytes:
                logging.warning(
                    '[CapturesRouter][CaptureUploadBodyLimitMiddleware] Rejected capture upload by Content-Length. '
                    f'[content_length: {content_length_header}]',
                )
                # Content-Length が分かる場合は 1 バイトも読まずに 413 を返す。
                # 本文を drain すると一時領域消費が残るため、ここでは読まない。
                await self._SendTooLargeResponse(scope, send)
                return

        # Content-Length 欠如や途中超過に備え、receive で本文累積を監視する。
        # 例外で中断すると ExceptionMiddleware が 500 に変換し得るため、フラグと send 差し替えで 413 にする。
        received_bytes = 0
        body_limit_exceeded = False
        response_started = False
        replacement_response_sent = False

        async def limited_receive() -> Message:
            nonlocal received_bytes, body_limit_exceeded
            if body_limit_exceeded is True:
                return {
                    'type': 'http.request',
                    'body': b'',
                    'more_body': False,
                }
            message = await receive()
            if message['type'] != 'http.request':
                return message
            body = message.get('body', b'')
            received_bytes += len(body)
            if received_bytes > self.max_body_bytes:
                body_limit_exceeded = True
                logging.warning(
                    '[CapturesRouter][CaptureUploadBodyLimitMiddleware] Rejected capture upload by streamed body size. '
                    f'[received_bytes: {received_bytes}]',
                )
                # 後段には本文終端だけを渡し、残りは読まずに spool と受信を打ち切る。
                # drain すると送信終了まで 413 を返せず、終端のない巨大ストリームに拘束され続ける。
                return {
                    'type': 'http.request',
                    'body': b'',
                    'more_body': False,
                }
            return message

        async def limited_send(message: Message) -> None:
            nonlocal response_started, replacement_response_sent
            if body_limit_exceeded is True:
                if message['type'] == 'http.response.start':
                    response_started = True
                    if replacement_response_sent is False:
                        # 後段の 4xx/5xx を 413 に差し替える
                        await send({
                            'type': 'http.response.start',
                            'status': status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            'headers': [
                                (b'content-type', b'application/json'),
                            ],
                        })
                    return
                if message['type'] == 'http.response.body':
                    if replacement_response_sent is False:
                        replacement_response_sent = True
                        await send({
                            'type': 'http.response.body',
                            'body': b'{"detail":"Capture upload exceeds the 20 MiB limit"}',
                            'more_body': False,
                        })
                    return
            if message['type'] == 'http.response.start':
                response_started = True
            await send(message)

        await self.app(scope, limited_receive, limited_send)

        # 後段がレスポンスを開始する前に上限超過だけが起きた場合のフォールバック
        if body_limit_exceeded is True and response_started is False:
            await self._SendTooLargeResponse(scope, send)


@router.post(
    '',
    summary = 'キャプチャ画像アップロード API',
    status_code = status.HTTP_204_NO_CONTENT,
)
def CaptureUploadAPI(
    image: Annotated[UploadFile, File(description='アップロードするキャプチャ画像 (JPEG or PNG)。')],
):
    """
    クライアント側でキャプチャした画像をサーバーにアップロードする。<br>
    アップロードされた画像は、サーバー設定で指定されたフォルダに保存される。<br>
    同期ファイル I/O を伴うため敢えて同期関数として実装している。<br>
    HTTP 本文サイズは CaptureUploadBodyLimitMiddleware が multipart 解析前に制限する。
    """

    # 画像が JPEG または PNG かをチェック
    ## 万が一悪意ある攻撃者から危険なファイルを送り込まれないように
    mimetype: str = puremagic.magic_stream(image.file)[0].mime_type
    if mimetype != 'image/jpeg' and mimetype != 'image/png':
        logging.error('[CapturesRouter][CaptureUploadAPI] Invalid image file was uploaded.')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Please upload JPEG or PNG image',
        )

    # シークを元に戻す（重要）
    ## puremagic を使った時点でファイルはシークされているため、戻さないと 0 バイトになる
    image.file.seek(0)

    # 先頭から順に保存容量が空いている保存先フォルダを探す
    upload_folders: list[Path] = [Path(folder) for folder in Config().capture.upload_folders]
    for upload_folder in upload_folders:

        # 万が一保存先フォルダが存在しない場合は次の保存先フォルダを探す
        if not upload_folder.exists():
            continue

        # 保存前に空き容量を確認する。
        # 最終フォルダでも最低空き容量を下回る場合は書き込まず、ENOSPC まで進めない。
        upload_folder_disk_usage = shutil.disk_usage(upload_folder)
        if upload_folder_disk_usage.free < (_MIN_FREE_BYTES_AFTER_WRITE + MAX_CAPTURE_UPLOAD_BYTES):
            continue

        # ディレクトリトラバーサル対策のためのチェック
        ## ref: https://stackoverflow.com/a/45190125/17124142
        filename = Path(cast(str, image.filename))
        try:
            upload_folder.joinpath(filename).resolve().relative_to(upload_folder.resolve())
        except ValueError:
            logging.error('[CapturesRouter][CaptureUploadAPI] Invalid filename was specified.')
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = 'Specified filename is invalid',
            )

        # 保存するファイルパス
        filepath = upload_folder / filename

        # 既にファイルが存在していた場合は上書きしないようにリネーム
        ## ref: https://note.nkmk.me/python-pathlib-name-suffix-parent/
        count = 1
        while filepath.exists():
            filepath = upload_folder / f'{filename.stem}-{count}{filename.suffix}'
            count += 1

        # キャプチャを保存する。途中失敗時は部分ファイルを必ず削除する。
        try:
            with open(filepath, mode='wb') as buffer:
                _CopyUploadWithLimit(image.file, buffer, MAX_CAPTURE_UPLOAD_BYTES)
        except ValueError:
            filepath.unlink(missing_ok=True)
            logging.error('[CapturesRouter][CaptureUploadAPI] Capture upload exceeded the size limit.')
            raise HTTPException(
                status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail = 'Capture upload exceeds the 20 MiB limit',
            )
        except PermissionError:
            filepath.unlink(missing_ok=True)
            logging.error('[CapturesRouter][CaptureUploadAPI] Permission denied to save the file.')
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = 'Permission denied to save the file',
            )
        except OSError as ex:
            filepath.unlink(missing_ok=True)
            is_disk_full_error = False
            if hasattr(ex, 'winerror'):
                is_disk_full_error = ex.winerror == 112  # type: ignore
            if hasattr(ex, 'errno'):
                is_disk_full_error = ex.errno == errno.ENOSPC
            if is_disk_full_error is True:
                logging.error('[CapturesRouter][CaptureUploadAPI] No space left on the device.')
                raise HTTPException(
                    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail = 'No space left on the device',
                )
            else:
                logging.error('[CapturesRouter][CaptureUploadAPI] Unexpected OSError:', exc_info=ex)
                raise HTTPException(
                    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail = 'Unexpected error occurred while saving the file',
                )

        # キャプチャのアップロードに成功したら 204 No Content を返す
        return

    # 保存先フォルダが見つからなかった場合はエラー
    logging.error('[CapturesRouter][CaptureUploadAPI] No available folder to save the file.')
    raise HTTPException(
        status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail = 'No available folder to save the file',
    )
