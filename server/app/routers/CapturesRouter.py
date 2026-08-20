
import errno
import re
import shutil
from pathlib import Path
from typing import Annotated, BinaryIO, cast

import puremagic
from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app import logging
from app.config import Config
from app.utils.KonomiTVBS4KRequestBodyLimit import (
    MULTIPART_FORM_DATA_OVERHEAD_BYTES,
    KonomiTVBS4KRequestBodyLimit,
)


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
MAX_CAPTURE_REQUEST_BODY_BYTES = MAX_CAPTURE_UPLOAD_BYTES + MULTIPART_FORM_DATA_OVERHEAD_BYTES

# 書き込み後も残すべき最低空き容量。最終フォルダでもこの余裕が無ければ保存しない。
_MIN_FREE_BYTES_AFTER_WRITE = 10 * 1024 * 1024

# copy 時の読み取り単位。上限判定と ENOSPC 時の途中ファイル削除をチャンク単位で行う。
_COPY_CHUNK_BYTES = 1024 * 1024

# Capture upload の既存上限を、ほかの form endpoint と同じ共通 middleware 設定で表す。
CAPTURE_UPLOAD_BODY_LIMIT = KonomiTVBS4KRequestBodyLimit(
    method='POST',
    path_pattern=re.compile(r'/api/captures/?'),
    max_body_bytes=MAX_CAPTURE_REQUEST_BODY_BYTES,
    detail='Capture upload exceeds the 20 MiB limit',
)


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
    HTTP 本文サイズは共通 body limit middleware が multipart 解析前に制限する。
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

        # 排他的にファイルを作成し、同名ファイルが存在した場合は連番を付けて再試行する。
        # exists() で事前確認すると、並行リクエストが同じ未使用名を選ぶ TOCTOU 競合になるため行わない。
        ## ref: https://note.nkmk.me/python-pathlib-name-suffix-parent/
        count = 1
        filepath_created = False
        try:
            while True:
                try:
                    buffer = open(filepath, mode='xb')
                    filepath_created = True
                    break
                except FileExistsError:
                    filepath = upload_folder / f'{filename.stem}-{count}{filename.suffix}'
                    count += 1

            # キャプチャを保存する。途中失敗時はこのリクエストが作成した部分ファイルだけを削除する。
            with buffer:
                _CopyUploadWithLimit(image.file, buffer, MAX_CAPTURE_UPLOAD_BYTES)
        except ValueError:
            if filepath_created is True:
                filepath.unlink(missing_ok=True)
            logging.error('[CapturesRouter][CaptureUploadAPI] Capture upload exceeded the size limit.')
            raise HTTPException(
                status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail = 'Capture upload exceeds the 20 MiB limit',
            )
        except PermissionError:
            if filepath_created is True:
                filepath.unlink(missing_ok=True)
            logging.error('[CapturesRouter][CaptureUploadAPI] Permission denied to save the file.')
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = 'Permission denied to save the file',
            )
        except OSError as ex:
            if filepath_created is True:
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
