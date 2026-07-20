from fastapi import HTTPException, status

from app.config import Config


async def EnsureJikkyoEnabled() -> None:
    """
    ニコニコ実況 / NX-Jikkyo を利用する API を、サーバー全体の設定で保護する。

    Raises:
        HTTPException: 実況機能が無効な場合。

    Returns:
        None
    """

    # パスパラメーターの DB 解決やユーザー認証より先に、無効な機能へのアクセスを一貫して拒否する
    if Config().general.jikkyo_enabled is False:
        raise HTTPException(
            status_code = status.HTTP_403_FORBIDDEN,
            detail = 'Jikkyo is disabled by server settings',
        )
