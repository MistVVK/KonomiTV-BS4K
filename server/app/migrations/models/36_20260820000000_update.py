from typing import Any, cast

from aiosqlite import Connection
from cryptography.fernet import InvalidToken
from tortoise import BaseDBAsyncClient

from app.constants import NICONICO_TOKEN_ENCRYPTION_PREFIX, NICONICO_TOKEN_FERNET


def _EncryptNiconicoToken(value: Any) -> Any:
    """旧形式のニコニコ OAuth トークンを暗号化する。

    Args:
        value: SQLite から渡されるトークン値。

    Returns:
        接頭辞付きの暗号文。NULL・空文字・暗号化済みの値は変更しない。
    """

    # migration の再実行や部分適用済み DB でも二重暗号化しない
    if value is None or value == '' or not isinstance(value, str):
        return value
    if value.startswith(NICONICO_TOKEN_ENCRYPTION_PREFIX):
        return value

    encrypted_text = NICONICO_TOKEN_FERNET.encrypt(value.encode('utf-8')).decode('utf-8')
    return f'{NICONICO_TOKEN_ENCRYPTION_PREFIX}{encrypted_text}'


def _DecryptNiconicoToken(value: Any) -> Any:
    """暗号化済みのニコニコ OAuth トークンを旧形式へ戻す。

    Args:
        value: SQLite から渡されるトークン値。

    Returns:
        復号済みの平文。NULL・空文字・旧形式の値は変更しない。

    Raises:
        InvalidToken: 鍵が異なるか暗号文が破損している場合。
    """

    # 旧形式の平文を受理し、upgrade 前後が混在した DB も安全に downgrade できるようにする
    if value is None or value == '' or not isinstance(value, str):
        return value
    if value.startswith(NICONICO_TOKEN_ENCRYPTION_PREFIX) is False:
        return value

    token = value[len(NICONICO_TOKEN_ENCRYPTION_PREFIX):].encode('utf-8')
    try:
        return NICONICO_TOKEN_FERNET.decrypt(token).decode('utf-8')
    except InvalidToken:
        # SQLite 関数の失敗として migration 全体を rollback し、復号不能な値を部分適用しない
        raise


async def upgrade(db: BaseDBAsyncClient) -> str:
    """既存のニコニコ OAuth トークンを暗号化する。

    Args:
        db: Aerich が渡す DB クライアント。

    Returns:
        登録した SQLite 関数で既存トークンを暗号化する SQL。
    """

    # Python 側の Fernet を SQL 内から呼び、暗号文だけを transaction 内で DB へ書き込む
    ## upgrade() 内で先に DML を実行すると Aerich の version 更新と atomic にできないため、関数登録だけを先に行う
    async with db.acquire_connection() as raw_connection:
        connection = cast(Connection, raw_connection)
        await connection.create_function(
            'konomitv_bs4k_encrypt_niconico_token',
            1,
            _EncryptNiconicoToken,
        )

    return """
        UPDATE "users"
        SET
            "niconico_access_token" = konomitv_bs4k_encrypt_niconico_token("niconico_access_token"),
            "niconico_refresh_token" = konomitv_bs4k_encrypt_niconico_token("niconico_refresh_token")
        WHERE
            "niconico_access_token" IS NOT NULL
            OR "niconico_refresh_token" IS NOT NULL;
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    """ニコニコ OAuth トークンを旧形式の平文へ戻す。

    Args:
        db: Aerich が渡す DB クライアント。

    Returns:
        登録した SQLite 関数で既存トークンを復号する SQL。
    """

    # 平文を SQL 文字列や migration ログへ展開せず、SQLite 関数内だけで復号する
    async with db.acquire_connection() as raw_connection:
        connection = cast(Connection, raw_connection)
        await connection.create_function(
            'konomitv_bs4k_decrypt_niconico_token',
            1,
            _DecryptNiconicoToken,
        )

    return """
        UPDATE "users"
        SET
            "niconico_access_token" = konomitv_bs4k_decrypt_niconico_token("niconico_access_token"),
            "niconico_refresh_token" = konomitv_bs4k_decrypt_niconico_token("niconico_refresh_token")
        WHERE
            "niconico_access_token" IS NOT NULL
            OR "niconico_refresh_token" IS NOT NULL;
    """
