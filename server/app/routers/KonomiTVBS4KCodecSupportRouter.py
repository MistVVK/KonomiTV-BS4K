from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app import schemas
from app.models.User import User
from app.routers.UsersRouter import GetCurrentAdminUser
from app.utils.KonomiTVBS4KCodecSupport import KonomiTVBS4KCodecSupportJobManager


# コーデック対応のサーバー診断は FFmpeg などの外部プロセスを多数起動するため、
# 管理者が明示的に開始したときだけ共有ジョブとして実行する。
router = APIRouter(
    tags = ['KonomiTV-BS4K Codec Support'],
    prefix = '/api/maintenance/konomitv-bs4k-codec-support',
)


@router.post(
    '/probe',
    summary = 'コーデック対応サーバー診断開始 API',
    responses = {
        status.HTTP_200_OK: {
            'description': '同一環境署名の結果キャッシュが再利用された。',
            'model': schemas.KonomiTVBS4KCodecSupportJob,
        },
        status.HTTP_202_ACCEPTED: {
            'description': '新しい共有診断ジョブを開始または実行中ジョブへ合流した。',
            'model': schemas.KonomiTVBS4KCodecSupportJob,
        },
    },
)
async def KonomiTVBS4KCodecSupportProbeCreateAPI(
    response: Response,
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
    force: Annotated[
        bool,
        Query(description='同一環境署名の結果キャッシュがある場合でも再診断する。'),
    ] = False,
) -> schemas.KonomiTVBS4KCodecSupportJob:
    """
    管理者専用のコーデック対応サーバー診断 (FFmpeg / QSV / NVENC / AMF の実 probe) を開始する。<br>
    同一環境署名の結果キャッシュがあれば 200 として再利用し、新しいジョブを開始または
    実行中ジョブへ合流した場合は 202 を返す。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    del current_user
    started, job = await KonomiTVBS4KCodecSupportJobManager.startProbe(force=force)
    response.status_code = status.HTTP_202_ACCEPTED if started else status.HTTP_200_OK
    return job


@router.get(
    '',
    summary = 'コーデック対応サーバー診断状態取得 API',
    response_model = schemas.KonomiTVBS4KCodecSupportJob,
)
async def KonomiTVBS4KCodecSupportGetAPI(
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> schemas.KonomiTVBS4KCodecSupportJob:
    """
    共有診断ジョブの状態 (Idle / Running / Completed / Failed)、進捗、部分結果を取得する。<br>
    実行中でも未完了の probe だけ Unknown として返すため、進捗表示と部分結果の取得に使う。<br>
    キャンセルされた場合は同一環境署名の直近成功結果を復元し、再診断が失敗した場合は Failed と部分結果を返す。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    del current_user
    return await KonomiTVBS4KCodecSupportJobManager.getState()


@router.delete(
    '/probe',
    summary = 'コーデック対応サーバー診断キャンセル API',
    status_code = status.HTTP_204_NO_CONTENT,
    responses = {
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            'description': 'キャンセルは受け付けたが、外部プロセスの回収が時間内に終わらなかった。',
        },
    },
)
async def KonomiTVBS4KCodecSupportProbeDeleteAPI(
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> Response:
    """
    実行中の共有診断ジョブをキャンセルし、外部プロセスの回収を待ってから Idle へ戻す。<br>
    実行中ジョブがない場合も冪等に 204 を返す。完了済みの結果キャッシュは保持する。<br>
    回収が時間内に終わらない場合は 503 を返し、外部プロセスが残っている可能性を呼び出し側へ伝える。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    del current_user
    result = await KonomiTVBS4KCodecSupportJobManager.cancelProbe()
    if result == 'ReclaimTimeout':
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
