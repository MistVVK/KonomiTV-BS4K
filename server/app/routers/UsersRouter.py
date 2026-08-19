
import asyncio
import pathlib
import uuid
from datetime import datetime, timedelta
from typing import Annotated, BinaryIO, cast

import anyio
from fastapi import (
    APIRouter,
    Body,
    Cookie,
    Depends,
    File,
    HTTPException,
    Path,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from PIL import Image, UnidentifiedImageError
from tortoise import timezone
from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from app import logging, schemas
from app.constants import (
    ACCOUNT_ICON_DEFAULT_DIR,
    ACCOUNT_ICON_DIR,
    JST,
    JWT_SECRET_KEY,
    PASSWORD_CONTEXT,
)
from app.models.AccountLink import AccountLink
from app.models.BlueskyAccount import BlueskyAccount
from app.models.RefreshToken import RefreshToken
from app.models.TwitterAccount import TwitterAccount
from app.models.User import User
from app.utils.AuthSecurity import (
    DUMMY_PASSWORD_HASH,
    LOGIN_ATTEMPT_LIMITER,
    GenerateRefreshToken,
    GenerateRefreshTokenFamilyID,
    GetClientIP,
    GetUsernameFingerprint,
    GetUsernameRateLimitKey,
    HashRefreshToken,
)


# ルーター
router = APIRouter(
    tags = ['Users'],
    prefix = '/api/users',
)

# JWT アクセストークンの有効期間
ACCESS_TOKEN_LIFETIME = timedelta(minutes=15)
# 更新トークンの有効期間（ローテーション時に更新される）
REFRESH_TOKEN_LIFETIME = timedelta(days=30)
# 更新トークンを保存する HttpOnly Cookie
REFRESH_TOKEN_COOKIE_NAME = 'KonomiTV-Refresh-Token'
REFRESH_TOKEN_COOKIE_PATH = '/api/users'
REFRESH_TOKEN_COOKIE_MAX_AGE = int(REFRESH_TOKEN_LIFETIME.total_seconds())


def SetRefreshTokenCookie(request: Request, response: Response, refresh_token: str) -> None:
    """更新トークンをHttpOnly Cookieへ設定する。"""

    response.set_cookie(
        key = REFRESH_TOKEN_COOKIE_NAME,
        value = refresh_token,
        max_age = REFRESH_TOKEN_COOKIE_MAX_AGE,
        httponly = True,
        secure = request.url.scheme == 'https',
        samesite = 'lax',
        path = REFRESH_TOKEN_COOKIE_PATH,
    )


def DeleteRefreshTokenCookie(request: Request, response: Response) -> None:
    """更新トークンCookieを削除する。"""

    response.delete_cookie(
        key = REFRESH_TOKEN_COOKIE_NAME,
        httponly = True,
        secure = request.url.scheme == 'https',
        samesite = 'lax',
        path = REFRESH_TOKEN_COOKIE_PATH,
    )


def InvalidRefreshTokenException(request: Request) -> HTTPException:
    """更新トークンの詳細を漏らさない共通エラーを生成する。"""

    # HTTPException を投げると FastAPI は依存注入された Response のCookieをコピーしないため、
    # 削除Cookieを例外応答のヘッダーへ明示的に引き継ぐ。
    delete_response = Response()
    DeleteRefreshTokenCookie(request, delete_response)
    return HTTPException(
        status_code = status.HTTP_401_UNAUTHORIZED,
        detail = 'Refresh token is invalid',
        headers = {
            'Cache-Control': 'no-store',
            'Set-Cookie': delete_response.headers['set-cookie'],
        },
    )


def GenerateAccessToken(user_id: int, token_version: int) -> str:
    """
    ユーザー ID を受け取り、そのユーザー ID を含む JWT アクセストークンを生成する

    Args:
        user_id (int): ユーザー ID
        token_version (int): ユーザーのJWT失効バージョン

    Returns:
        str: JWT アクセストークン (有効期限は 15 分間)
    """

    issued_at = datetime.now(JST)

    # JWT エンコードするペイロード
    jwt_payload = {
        # トークンの発行者
        'iss': 'KonomiTV Server',
        # トークンの種類
        'typ': 'AccessToken',
        # ユーザーの識別子 (ユーザー ID を文字列化したもの)
        'sub': f'{user_id}',
        # パスワード変更時に既存トークンを失効させるためのバージョン
        'token_version': token_version,
        # JWT の発行時間
        'iat': issued_at,
        # JWT の有効期限 (JWT の発行から 15 分間)
        'exp': issued_at + ACCESS_TOKEN_LIFETIME,
        # JWT ごとの一意な ID (UUID v4)
        'jti': str(uuid.uuid4()),
    }

    # JWT エンコードを行い、JWT アクセストークンを生成
    return jwt.encode(
        claims = jwt_payload,
        key = JWT_SECRET_KEY,
        algorithm = 'HS256',
    )


async def GetCurrentUser(token: Annotated[str, Depends(OAuth2PasswordBearer(tokenUrl='users/token'))]) -> User:
    """ 現在ログイン中のユーザーを取得する """

    try:
        # JWT トークンをデコード
        jwt_payload = jwt.decode(
            token = token,
            key = JWT_SECRET_KEY,
            algorithms = ['HS256'],
            issuer = 'KonomiTV Server',
        )

        # typ が AccessToken でない (JWT トークンが不正)
        if jwt_payload.get('typ') != 'AccessToken':
            logging.warning('[GetCurrentUser] Access token type is invalid.')
            raise HTTPException(
                status_code = status.HTTP_401_UNAUTHORIZED,
                detail = 'Access token type is invalid',
                headers = {'WWW-Authenticate': 'Bearer'},
            )

        # Subject が JWT ペイロードに含まれていない (JWT トークンが不正)
        if jwt_payload.get('sub') is None:
            logging.warning('[GetCurrentUser] Access token data is invalid.')
            raise HTTPException(
                status_code = status.HTTP_401_UNAUTHORIZED,
                detail = 'Access token data is invalid',
                headers = {'WWW-Authenticate': 'Bearer'},
            )

        # token_version がない旧JWTや不正な型のJWTは受け入れない
        ## パスワード変更時に既存JWTを確実に失効させるため、再ログインを要求する
        token_version = jwt_payload.get('token_version')
        if type(token_version) is not int or token_version < 0:
            logging.warning('[GetCurrentUser] Access token version is invalid.')
            raise HTTPException(
                status_code = status.HTTP_401_UNAUTHORIZED,
                detail = 'Access token is invalid',
                headers = {'WWW-Authenticate': 'Bearer'},
            )

        user_id = int(jwt_payload['sub'])

    # JWT トークンが不正
    except (JWTError, TypeError, ValueError) as ex:
        logging.warning('[GetCurrentUser] Access token is invalid:', exc_info=ex)
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail = 'Access token is invalid',
            headers = {'WWW-Authenticate': 'Bearer'},
        )

    # JWT トークンに刻まれたユーザー ID に紐づくユーザー情報を取得
    ## 認証時の Depends として認証が必要な全 API から呼ばれるメソッドなので、ここでは関連アカウントの取得を行わない
    ## 関連アカウントの取得は、その情報を返す必要があるエンドポイントの実装 (UsersAPI, UserAPI など) 側で明示的に行うべき
    current_user = await User.filter(id=user_id).get_or_none()

    # そのユーザー ID のユーザーが存在しない
    if not current_user:
        logging.warning(f'[GetCurrentUser] User associated with access token does not exist. [user_id: {user_id}]')
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail = 'User associated with access token does not exist',
            headers = {'WWW-Authenticate': 'Bearer'},
        )

    # パスワード変更などでユーザーのトークンバージョンが進んでいる
    if current_user.token_version != token_version:
        logging.warning(f'[GetCurrentUser] Access token version is obsolete. [user_id: {user_id}]')
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail = 'Access token is invalid',
            headers = {'WWW-Authenticate': 'Bearer'},
        )

    return current_user


async def GetCurrentAdminUser(current_user: Annotated[User, Depends(GetCurrentUser)]) -> User:
    """ 現在ログイン中の管理者ユーザーを取得する """

    # 取得したユーザーが管理者ではない
    if current_user.is_admin is False:
        logging.warning(f'[GetCurrentAdminUser] Don\'t have permission to access this resource. [user_id: {current_user.id}]')
        raise HTTPException(
            status_code = status.HTTP_403_FORBIDDEN,
            detail = 'Don\'t have permission to access this resource',
            headers = {'WWW-Authenticate': 'Bearer'},
        )

    return current_user


async def GetSpecifiedUser(
    username: Annotated[str, Path(description='アカウントのユーザー名。')],
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],  # 管理者ユーザーのみアクセス可能
) -> User:
    """ 指定されたユーザー名のユーザーを取得する """

    # 指定されたユーザー名のユーザーを取得
    user = await User.filter(name=username).prefetch_related(
        'twitter_accounts',
        'bluesky_accounts',
        'account_links__twitter_account',
        'account_links__bluesky_account',
    ).get_or_none()

    # 指定されたユーザー名のユーザーが存在しない
    if not user:
        logging.error(f'[GetSpecifiedUser] Specified user was not found. [username: {username}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified user was not found',
        )

    return user


def ResizeAndSaveIcon(file: BinaryIO, save_path: pathlib.Path) -> None:
    """
    アイコンを 512×512 の正方形 PNG にリサイズして保存する
    この関数は同期的なので、非同期関数から呼ぶ場合は asyncio.to_thread() を使うこと
    ref: https://note.nkmk.me/python-pillow-basic/
    ref: https://note.nkmk.me/python-pillow-image-resize/
    ref: https://note.nkmk.me/python-pillow-image-crop-trimming/

    Args:
        file (io.BytesIO): 入力元のファイルオブジェクト
        save_path (pathlib.Path): トリミング&リサイズしたファイルの保存先のパス
    """

    # リサイズする画像の幅と高さ
    RESIZE_WIDTH_AND_HEIGHT = 512

    # Content-Type はクライアントが偽装できるため、Pillow が実データから JPEG / PNG と判定した画像だけを開く
    # 入力の解析・デコードに起因するエラーだけを入力不正として正規化し、保存先の I/O エラーとは区別する
    try:
        with Image.open(file, formats=['JPEG', 'PNG']) as pillow_image:

            # 縦横どちらか長さが短い方に合わせて正方形にクロップ
            # crop() で遅延デコードも完了させ、不正な画像を保存処理へ進めない
            pillow_image_crop = pillow_image.crop((
                (pillow_image.size[0] - min(pillow_image.size)) // 2,
                (pillow_image.size[1] - min(pillow_image.size)) // 2,
                (pillow_image.size[0] + min(pillow_image.size)) // 2,
                (pillow_image.size[1] + min(pillow_image.size)) // 2,
            ))

            # デコード済み画像を保存用サイズへ変換する
            pillow_image_resize = pillow_image_crop.resize((RESIZE_WIDTH_AND_HEIGHT, RESIZE_WIDTH_AND_HEIGHT))
    except (OSError, Image.DecompressionBombError) as ex:
        raise UnidentifiedImageError('Uploaded file is not a valid JPEG or PNG image.') from ex

    # 保存先の権限・容量・I/O エラーは入力不正ではないため、422 に変換せず呼び出し元へ伝播させる
    pillow_image_resize.save(save_path, 'PNG')


@router.post(
    '',
    summary = 'アカウント作成 API',
    response_description = '作成したユーザーアカウントの情報。',
    response_model = schemas.User,
    status_code = status.HTTP_201_CREATED,
)
async def UserCreateAPI(
    user_create_request: Annotated[schemas.UserCreateRequest, Body(description='作成するユーザーの名前とパスワード。')],
):
    """
    新しいユーザーアカウントを作成する。

    指定されたユーザー名のアカウントがすでに存在する場合は 422 エラーが返される。<br>
    また、最初に作成されたアカウントのみ、特別に管理者権限 (is_admin: true) が付与される。
    """

    # 同じユーザー名のアカウントがあったら 422 を返す
    ## ユーザー名がそのままログイン ID になるので、同じユーザー名のアカウントがあると重複する
    if await User.filter(name=user_create_request.username).get_or_none() is not None:
        logging.warning(f'[UsersRouter][UserCreateAPI] Specified username is duplicated. [username: {user_create_request.username}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified username is duplicated',
        )

    # 利用不可なユーザー名だったら 422 を返す
    ## /api/users/me と /api/users/token があるので、もしその名前で登録できてしまうと重複して面倒なことになる
    ## そんな名前で登録する人はいないとは思うけど、念のため…
    PERMITTED_USERNAMES = ['me', 'token']
    if user_create_request.username.lower() in PERMITTED_USERNAMES:
        logging.warning(f'[UsersRouter][UserCreateAPI] Specified username is not permitted. [username: {user_create_request.username}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified username is not permitted',
        )

    # 新しいユーザーアカウントのモデルを作成・保存
    ## 最初の管理者判定 (count) と作成を同一トランザクション内で直列化する
    ## 同時に最初のユーザーが作成された場合でも、最初の1人だけが管理者権限を付与される
    ## UNIQUE 制約があるため、同時登録で同じユーザー名が作られた場合は IntegrityError が発生し、422 を返す
    ## bcrypt のハッシュ化は重いため、トランザクションの外で先に実行する
    password_hash = PASSWORD_CONTEXT.hash(user_create_request.password)
    try:
        async with in_transaction() as connection:
            is_first_user = await User.all().using_db(connection).count() == 0  # 他のユーザーアカウントがまだ作成されていないなら、特別に管理者権限を付与
            current_user = await User.create(
                name = user_create_request.username,  # ユーザー名
                password = password_hash,  # ハッシュ化されたパスワード
                is_admin = is_first_user,
                client_settings = {},  # クライアント側の設定（ひとまず空の辞書を設定）
                using_db = connection,
            )
    except IntegrityError as ex:
        # 同時登録などで UNIQUE 制約に違反した場合は重複エラーとして返す
        logging.warning(f'[UsersRouter][UserCreateAPI] Specified username is duplicated. [username: {user_create_request.username}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified username is duplicated',
        ) from ex

    # 外部テーブルのデータを取得してから返す
    await current_user.fetch_related(
        'twitter_accounts',
        'bluesky_accounts',
        'account_links__twitter_account',
        'account_links__bluesky_account',
    )
    return current_user


@router.post(
    '/token',
    summary = 'アクセストークン発行 API (OAuth2 準拠)',
    response_description = 'JWT エンコードされたアクセストークン。',
    response_model = schemas.UserAccessToken,
)
async def UserAccessTokenAPI(
    request: Request,
    response: Response,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
):
    """
    指定されたユーザー名とパスワードを検証し、そのユーザーの JWT エンコードされたアクセストークンを発行する。<br>
    この API は OAuth2 のトークンエンドポイントの仕様に準拠している（はず）。

    発行された JWT トークンを Authorization: Bearer で送ることで、認証が必要なエンドポイントにアクセスできる。<br>
    この API はアクセストークンを発行するだけで、ログインそのものは行わない。
    """

    client_ip = GetClientIP(request)
    username_key = GetUsernameRateLimitKey(form_data.username)

    # IP とユーザー名のどちらかが制限中なら認証処理を行わない
    retry_after = await LOGIN_ATTEMPT_LIMITER.Check(client_ip, username_key)
    if retry_after is not None:
        logging.warning(
            '[UsersRouter][UserAccessTokenAPI] Login attempt was rate limited. '
            f'[client_ip: {client_ip}, username_fingerprint: {GetUsernameFingerprint(form_data.username)}]'
        )
        raise HTTPException(
            status_code = status.HTTP_429_TOO_MANY_REQUESTS,
            detail = 'Too many login attempts',
            headers = {
                'Retry-After': str(retry_after),
                'Cache-Control': 'no-store',
            },
        )

    # ユーザーを取得
    current_user = await User.filter(name=form_data.username).get_or_none()

    # ユーザーが存在しない場合も固定bcryptハッシュを検証し、応答時間の差を抑える
    password_hash = current_user.password if current_user is not None else DUMMY_PASSWORD_HASH
    password_is_valid = PASSWORD_CONTEXT.verify(form_data.password, password_hash)

    # ユーザーが存在しない場合とパスワードが違う場合は同じ応答にする
    if current_user is None or password_is_valid is False:
        await LOGIN_ATTEMPT_LIMITER.RecordFailure(client_ip, username_key)
        logging.warning(
            '[UsersRouter][UserAccessTokenAPI] Login credentials were rejected. '
            f'[client_ip: {client_ip}, username_fingerprint: {GetUsernameFingerprint(form_data.username)}]'
        )
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail = 'Invalid credentials',
            headers = {
                'WWW-Authenticate': 'Bearer',
                'Cache-Control': 'no-store',
            },
        )

    await LOGIN_ATTEMPT_LIMITER.RecordSuccess(username_key)

    # ログイン成功時に更新トークンを発行し、DBにはハッシュだけを保存する
    refresh_token = GenerateRefreshToken()
    await RefreshToken.create(
        token_hash = HashRefreshToken(refresh_token),
        family_id = GenerateRefreshTokenFamilyID(),
        user_id = current_user.id,
        expires_at = timezone.now() + REFRESH_TOKEN_LIFETIME,
    )
    SetRefreshTokenCookie(request, response, refresh_token)
    response.headers['Cache-Control'] = 'no-store'

    # JWT アクセストークンを生成して返す
    return schemas.UserAccessToken(
        access_token = GenerateAccessToken(current_user.id, current_user.token_version),
        token_type = 'bearer',
    )


@router.post(
    '/refresh',
    summary = 'アクセストークン更新 API',
    response_description = '更新されたJWTアクセストークン。',
    response_model = schemas.UserAccessToken,
)
async def RefreshAccessTokenAPI(
    request: Request,
    response: Response,
    refresh_token_cookie: Annotated[str | None, Cookie(alias=REFRESH_TOKEN_COOKIE_NAME)] = None,
):
    """HttpOnly Cookie の更新トークンをローテーションしてアクセストークンを更新する。"""

    if refresh_token_cookie is None:
        raise InvalidRefreshTokenException(request)

    now = timezone.now()
    refresh_token_hash = HashRefreshToken(refresh_token_cookie)
    new_refresh_token = GenerateRefreshToken()
    new_refresh_token_hash = HashRefreshToken(new_refresh_token)
    user: User | None = None
    refresh_token_error = False

    # 同じ更新トークンの同時利用を直列化し、利用済みトークンの再利用時は
    # ファミリー全体を失効させる。
    async with in_transaction() as connection:
        refresh_token = await RefreshToken.filter(
            token_hash = refresh_token_hash,
        ).select_for_update().using_db(connection).get_or_none()

        if refresh_token is None:
            refresh_token_error = True
        elif refresh_token.revoked_at is not None or refresh_token.expires_at <= now:
            await RefreshToken.filter(
                family_id = refresh_token.family_id,
            ).using_db(connection).update(revoked_at = now)
            refresh_token_error = True
        else:
            refresh_token_user_id = cast(int, getattr(refresh_token, 'user_id'))
            user = await User.filter(id=refresh_token_user_id).select_for_update().using_db(connection).get_or_none()
            if user is None:
                refresh_token_error = True
            else:
                refresh_token.revoked_at = now
                refresh_token.replaced_by_hash = new_refresh_token_hash
                await refresh_token.save(
                    using_db = connection,
                    update_fields = ['revoked_at', 'replaced_by_hash'],
                )
                await RefreshToken.create(
                    token_hash = new_refresh_token_hash,
                    family_id = refresh_token.family_id,
                    user_id = user.id,
                    expires_at = now + REFRESH_TOKEN_LIFETIME,
                    using_db = connection,
                )

    if refresh_token_error or user is None:
        raise InvalidRefreshTokenException(request)

    SetRefreshTokenCookie(request, response, new_refresh_token)
    response.headers['Cache-Control'] = 'no-store'
    return schemas.UserAccessToken(
        access_token = GenerateAccessToken(user.id, user.token_version),
        token_type = 'bearer',
    )


@router.post(
    '/logout',
    summary = 'ログアウト API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def UserLogoutAPI(
    request: Request,
    response: Response,
    refresh_token_cookie: Annotated[str | None, Cookie(alias=REFRESH_TOKEN_COOKIE_NAME)] = None,
):
    """現在の更新トークンを失効させ、Cookieを削除する。"""

    if refresh_token_cookie is not None:
        await RefreshToken.filter(
            token_hash = HashRefreshToken(refresh_token_cookie),
            revoked_at__isnull = True,
        ).update(revoked_at = timezone.now())

    DeleteRefreshTokenCookie(request, response)


@router.get(
    '',
    summary = 'アカウント一覧 API',
    response_description = 'すべてのユーザーアカウントの情報。',
    response_model = schemas.Users,
)
async def UsersAPI(
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """
    すべてのユーザーアカウントのリストを取得する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    # 常に関連するアカウント系テーブルの対応するレコードを全取得して返す
    return await User.all().prefetch_related(
        'twitter_accounts',
        'bluesky_accounts',
        'account_links__twitter_account',
        'account_links__bluesky_account',
    )


# ***** ログイン中ユーザーアカウント情報 API *****


@router.get(
    '/me',
    summary = 'アカウント情報 API (ログイン中のユーザー)',
    response_description = 'ログイン中のユーザーアカウントの情報。',
    response_model = schemas.User,
)
async def UserAPI(
    current_user: Annotated[User, Depends(GetCurrentUser)],
):
    """
    現在ログイン中のユーザーアカウントの情報を取得する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。
    """

    # 一番よく使う API なので、リクエスト時に twitter_accounts テーブルに仮のアカウントデータが残っていたらすべて消しておく
    ## Twitter 連携では途中で連携をキャンセルした場合に仮のアカウントデータが残置されてしまうので、それを取り除く
    if await TwitterAccount.filter(icon_url='Temporary').count() > 0:
        await TwitterAccount.filter(icon_url='Temporary').delete()

    # 常に関連するアカウント系テーブルの対応するレコードを全取得して返す
    return await User.filter(id=current_user.id).prefetch_related(
        'twitter_accounts',
        'bluesky_accounts',
        'account_links__twitter_account',
        'account_links__bluesky_account',
    ).get()


@router.post(
    '/me/account-links',
    summary = 'Twitter / Bluesky アカウント紐付け作成 API',
    response_description = '作成したアカウント紐付け。',
    response_model = schemas.AccountLink,
    status_code = status.HTTP_201_CREATED,
)
async def AccountLinkCreateAPI(
    account_link_create_request: Annotated[schemas.AccountLinkCreateRequest, Body(description='紐付ける Twitter / Bluesky アカウント ID 。')],
    current_user: Annotated[User, Depends(GetCurrentUser)],
):
    """
    ログイン中ユーザーの Twitter アカウントと Bluesky アカウントを紐付ける。<br>
    紐付けは視聴画面の Twitter タブで両方のタイムラインをまとめて表示し、ツイートを同時投稿する際に利用される。
    """

    # リクエストされた Twitter アカウントがログイン中ユーザーの所有物であることを確認する
    twitter_account = await TwitterAccount.filter(
        id = account_link_create_request.twitter_account_id,
        user_id = current_user.id,
    ).get_or_none()
    if twitter_account is None:
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified Twitter account does not exist',
        )

    # Bluesky 側も同じユーザーに属するレコードだけを許可し、他ユーザーのアカウントとの紐付けを防ぐ
    bluesky_account = await BlueskyAccount.filter(
        id = account_link_create_request.bluesky_account_id,
        user_id = current_user.id,
    ).get_or_none()
    if bluesky_account is None:
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified Bluesky account does not exist',
        )

    # 返却直後にクライアントが表示名やアイコンを表示できるように、両方の子レコードを取得しておく
    try:
        account_link = await AccountLink.create(
            user = current_user,
            twitter_account = twitter_account,
            bluesky_account = bluesky_account,
        )
    except IntegrityError as ex:
        # 紐付けは DB の一意制約で一対一を最終保証する
        ## 事前確認だけでは複数タブの同時作成を防げないため、競合後に実際の重複側を調べて既存のエラー文へ戻す
        if await AccountLink.filter(twitter_account_id=twitter_account.id).exists() is True:
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = 'Specified Twitter account is already linked',
            ) from ex
        if await AccountLink.filter(bluesky_account_id=bluesky_account.id).exists() is True:
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = 'Specified Bluesky account is already linked',
            ) from ex
        logging.error(
            f'[UsersRouter][AccountLinkCreateAPI] Failed to create account link due to an unexpected integrity error. [user_id: {current_user.id}]',
            exc_info=ex,
        )
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Failed to create account link',
        ) from ex
    await account_link.fetch_related('twitter_account', 'bluesky_account')
    return account_link


@router.delete(
    '/me/account-links/{link_id}',
    summary = 'Twitter / Bluesky アカウント紐付け解除 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def AccountLinkDeleteAPI(
    link_id: Annotated[int, Path(description='解除するアカウント紐付け ID 。')],
    current_user: Annotated[User, Depends(GetCurrentUser)],
):
    """
    ログイン中ユーザーの Twitter / Bluesky アカウント紐付けを解除する。<br>
    連携済みアカウント自体は削除しない。
    """

    # 紐付け解除はログイン中ユーザーのリンクレコードだけに限定する
    ## 個別の Twitter / Bluesky 連携は残されるので、紐付け解除後は別々のアカウントとして選択候補に表示される形となる
    account_link = await AccountLink.filter(id=link_id, user_id=current_user.id).get_or_none()
    if account_link is None:
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified account link does not exist',
        )
    await account_link.delete()


@router.put(
    '/me',
    summary = 'アカウント情報更新 API (ログイン中のユーザー)',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def UserUpdateAPI(
    user_update_request: Annotated[schemas.UserUpdateRequest, Body(description='更新するユーザーアカウントの情報。')],
    current_user: Annotated[User, Depends(GetCurrentUser)],
):
    """
    現在ログイン中のユーザーアカウントの情報を更新する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。
    """

    # ユーザー名を更新（存在する場合）
    if user_update_request.username is not None:

        # 重複しないように、同じユーザー名のアカウントがあったら 422 を返す
        ## 新しいユーザー名が現在のユーザー名と同じなら問題ないので除外
        if user_update_request.username != current_user.name and await User.filter(name=user_update_request.username).get_or_none():
            logging.warning(f'[UsersRouter][UserUpdateAPI] Specified username is duplicated. [username: {user_update_request.username}]')
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = 'Specified username is duplicated',
            )

        # 利用不可なユーザー名だったら 422 を返す
        ## /api/users/me と /api/users/token があるので、もしその名前で登録できてしまうと重複して面倒なことになる
        ## そんな名前で登録する人はいないとは思うけど、念のため…
        PERMITTED_USERNAMES = ['me', 'token']
        if user_update_request.username.lower() in PERMITTED_USERNAMES:
            logging.warning(f'[UsersRouter][UserUpdateAPI] Specified username is not permitted. [username: {user_update_request.username}]')
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = 'Specified username is not permitted',
            )

        # 新しいユーザー名を設定
        current_user.name = user_update_request.username

    # パスワードを更新する場合は、行ロック下でハッシュとトークンバージョンを更新する
    ## 並行するパスワード変更で token_version の増分が失われないようにする
    if user_update_request.password is not None:
        password_hash = PASSWORD_CONTEXT.hash(user_update_request.password)
        try:
            async with in_transaction() as connection:
                locked_user = await User.filter(id=current_user.id).select_for_update().using_db(connection).get()
                if user_update_request.username is not None:
                    locked_user.name = current_user.name
                locked_user.password = password_hash
                locked_user.token_version += 1
                update_fields = ['password', 'token_version']
                if user_update_request.username is not None:
                    update_fields.append('name')
                await locked_user.save(
                    using_db = connection,
                    update_fields = update_fields,
                )

                # パスワード変更時は、全端末の更新トークンも失効させる
                await RefreshToken.filter(
                    user_id = locked_user.id,
                    revoked_at__isnull = True,
                ).using_db(connection).update(revoked_at = timezone.now())
        except IntegrityError as ex:
            # 同時更新でユーザー名が UNIQUE 制約に違反した場合は重複エラーとして返す
            logging.warning(f'[UsersRouter][UserUpdateAPI] Specified username is duplicated. [username: {user_update_request.username}]')
            raise HTTPException(
                status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail = 'Specified username is duplicated',
            ) from ex
    else:
        # パスワードを変更しない場合は、指定されたユーザー名だけを保存する
        ## UNIQUE 制約があるため、同時更新でユーザー名が重複した場合は IntegrityError が発生する
        ## dependency 解決後に並行更新されたパスワード・token_version・権限・設定を古いモデルで上書きしない
        if user_update_request.username is not None:
            try:
                await current_user.save(update_fields=['name', 'updated_at'])
            except IntegrityError as ex:
                logging.warning(f'[UsersRouter][UserUpdateAPI] Specified username is duplicated. [username: {user_update_request.username}]')
                raise HTTPException(
                    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail = 'Specified username is duplicated',
                ) from ex


@router.get(
    '/me/icon',
    summary = 'アカウントアイコン画像 API (ログイン中のユーザー)',
    response_class = Response,
    responses = {
        status.HTTP_200_OK: {
            'description': 'ユーザーアカウントのアイコン画像。',
            'content': {'image/png': {}},
        }
    }
)
async def UserIconAPI(
    current_user: Annotated[User, Depends(GetCurrentUser)],
):
    """
    現在ログイン中のユーザーアカウントのアイコン画像を取得する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。
    """

    # ブラウザにキャッシュさせないようにヘッダーを設定
    # ref: https://developer.mozilla.org/ja/docs/Web/HTTP/Headers/Cache-Control
    header = {
        'Cache-Control': 'no-store',
    }

    # アイコン画像が保存されていればそれを返す
    icon_save_path = anyio.Path(str(ACCOUNT_ICON_DIR)) / f'{current_user.id:02}.png'
    if await icon_save_path.exists():
        return FileResponse(icon_save_path, headers=header)

    # デフォルトのアイコン画像を返す
    return FileResponse(ACCOUNT_ICON_DEFAULT_DIR / 'default.png', headers=header)


@router.put(
    '/me/icon',
    summary = 'アカウントアイコン画像更新 API (ログイン中のユーザー)',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def UserUpdateIconAPI(
    image: Annotated[UploadFile, File(description='アカウントのアイコン画像 (JPEG or PNG)。')],
    current_user: Annotated[User, Depends(GetCurrentUser)],
):
    """
    現在ログイン中のユーザーアカウントのアイコン画像を更新する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。
    """

    # MIME タイプが image/jpeg or image/png 以外
    if image.content_type != 'image/jpeg' and image.content_type != 'image/png':
        logging.warning(f'[UsersRouter][UserUpdateIconAPI] Please upload JPEG or PNG image. [content_type: {image.content_type}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Please upload JPEG or PNG image',
        )

    # 実データも JPEG / PNG のときだけ、正方形の PNG にリサイズして保存
    # 保存先ファイルパス: (ユーザー ID を0埋めしたもの).png
    try:
        await asyncio.to_thread(ResizeAndSaveIcon, image.file, ACCOUNT_ICON_DIR / f'{current_user.id:02}.png')
    except UnidentifiedImageError as ex:
        logging.warning(
            f'[UsersRouter][UserUpdateIconAPI] Invalid JPEG or PNG image was uploaded. '
            f'[error_type: {type(ex).__name__}]',
        )
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Please upload valid JPEG or PNG image',
        ) from ex


@router.delete(
    '/me',
    summary = 'アカウント削除 API (ログイン中のユーザー)',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def UserDeleteAPI(
    current_user: Annotated[User, Depends(GetCurrentUser)],
):
    """
    現在ログイン中のユーザーアカウントを削除する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。
    """

    # アイコン画像が保存されていれば削除する
    icon_save_path = anyio.Path(str(ACCOUNT_ICON_DIR)) / f'{current_user.id:02}.png'
    if await icon_save_path.exists():
        await icon_save_path.unlink()

    # 現在ログイン中のユーザーアカウント（自分自身）を削除
    # アカウントを削除すると、それ以降は（当然ながら）ログインを要求する API へアクセスできなくなる
    ## 削除と管理者数の確認を同一トランザクション内で直列化し、同時削除で管理者 0 人にならないようにする
    async with in_transaction() as connection:
        await current_user.delete(using_db = connection)

        # ユーザーを削除した結果、管理者アカウントがいなくなってしまった場合
        ## ID が一番若いアカウントに管理者権限を付与する（そうしないと誰も管理者権限を行使できないし付与できない）
        if await User.filter(is_admin=True).using_db(connection).count() == 0:
            id_young_user = await User.all().order_by('id').using_db(connection).first()
            if id_young_user is not None:
                id_young_user.is_admin = True
                await id_young_user.save(
                    using_db = connection,
                    update_fields = ['is_admin', 'updated_at'],
                )


# ***** 指定ユーザーアカウント情報 API (管理者用) *****


@router.get(
    '/{username}',
    summary = 'アカウント情報 API',
    response_description = 'ユーザーアカウントの情報。',
    response_model = schemas.User,
)
async def SpecifiedUserAPI(
    user: Annotated[User, Depends(GetSpecifiedUser)],
):
    """
    指定されたユーザーアカウントの情報を取得する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    return user


@router.put(
    '/{username}',
    summary = 'アカウント情報更新 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def SpecifiedUserUpdateAPI(
    user_update_request: Annotated[schemas.UserUpdateRequestForAdmin, Body(description='更新するユーザーアカウントの情報。')],
    user: Annotated[User, Depends(GetSpecifiedUser)],
):
    """
    指定されたユーザーアカウントの情報を更新する。/api/users/me と異なり、管理者権限の付与/剥奪のみ可能。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    # 管理者権限を剥奪する場合、この処理によってシステム内に管理者が一人もいなくならないかを確認
    ## 確認と保存を同一トランザクション内で直列化し、同時に複数の管理者が剥奪されても管理者 0 人にならないようにする
    if user_update_request.is_admin is False:
        async with in_transaction() as connection:
            # 同時実行されるパスワード変更などとの競合で古い値を書き戻さないよう、トランザクション内で最新のユーザーを再取得する
            ## トランザクション開始前に取得した user は古いスナップショットであり、そのまま save() すると
            ## 旧パスワード・旧ユーザー名・旧 token_version まで書き戻されてしまうため
            locked_user = await User.filter(id=user.id).select_for_update().using_db(connection).get()
            remaining_admins = await User.filter(is_admin=True).exclude(id=user.id).using_db(connection).count()
            if remaining_admins == 0:
                logging.warning('[UsersRouter][SpecifiedUserUpdateAPI] Cannot revoke admin permission because there are no more admins.')
                raise HTTPException(
                    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail = 'Cannot revoke admin permission because there are no more admins',
                )

            # 管理者権限の変更のみを保存する
            locked_user.is_admin = False
            await locked_user.save(
                using_db = connection,
                update_fields = ['is_admin'],
            )
    else:
        # 管理者権限を付与/剥奪
        if user_update_request.is_admin is not None:
            # 同時実行されるパスワード変更などとの競合で古い値を書き戻さないよう、最新のユーザーを再取得してから権限のみを更新する
            ## トランザクション外のため行ロックは効かないが、更新フィールドを is_admin に限定することで
            ## 旧パスワード・旧ユーザー名・旧 token_version の書き戻しを防ぐ
            locked_user = await User.filter(id=user.id).get()
            locked_user.is_admin = True
            await locked_user.save(
                update_fields = ['is_admin'],
            )


@router.get(
    '/{username}/icon',
    summary = 'アカウントアイコン画像 API',
    response_class = Response,
    responses = {
        status.HTTP_200_OK: {
            'description': 'ユーザーアカウントのアイコン画像。',
            'content': {'image/png': {}},
        }
    }
)
async def SpecifiedUserIconAPI(
    user: Annotated[User, Depends(GetSpecifiedUser)],
):
    """
    指定されたユーザーアカウントのアイコン画像を取得する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    # ブラウザにキャッシュさせないようにヘッダーを設定
    # ref: https://developer.mozilla.org/ja/docs/Web/HTTP/Headers/Cache-Control
    header = {
        'Cache-Control': 'no-store',
    }

    # アイコン画像が保存されていればそれを返す
    icon_save_path = anyio.Path(str(ACCOUNT_ICON_DIR)) / f'{user.id:02}.png'
    if await icon_save_path.exists():
        return FileResponse(icon_save_path, headers=header)

    # デフォルトのアイコン画像を返す
    return FileResponse(ACCOUNT_ICON_DEFAULT_DIR / 'default.png', headers=header)


@router.delete(
    '/{username}',
    summary = 'アカウント削除 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def SpecifiedUserDeleteAPI(
    user: Annotated[User, Depends(GetSpecifiedUser)],
):
    """
    指定されたユーザーアカウントを削除する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    # アイコン画像が保存されていれば削除する
    icon_save_path = anyio.Path(str(ACCOUNT_ICON_DIR)) / f'{user.id:02}.png'
    if await icon_save_path.exists():
        await icon_save_path.unlink()

    # 指定されたユーザーを削除
    ## 削除と管理者数の確認を同一トランザクション内で直列化し、同時削除で管理者 0 人にならないようにする
    async with in_transaction() as connection:
        await user.delete(using_db = connection)

        # ユーザーを削除した結果、管理者アカウントがいなくなってしまった場合
        ## ID が一番若いアカウントに管理者権限を付与する（そうしないと誰も管理者権限を行使できないし付与できない）
        if await User.filter(is_admin=True).using_db(connection).count() == 0:
            id_young_user = await User.all().order_by('id').using_db(connection).first()
            if id_young_user is not None:
                id_young_user.is_admin = True
                await id_young_user.save(
                    using_db = connection,
                    update_fields = ['is_admin', 'updated_at'],
                )
