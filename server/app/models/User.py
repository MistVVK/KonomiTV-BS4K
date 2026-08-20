
# Type Hints を指定できるように
# ref: https://stackoverflow.com/a/33533514/17124142
from __future__ import annotations

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

import httpx
from cryptography.fernet import InvalidToken
from tortoise import fields
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.fields import Field as TortoiseField
from tortoise.models import Model as TortoiseModel

from app import logging
from app.config import Config
from app.constants import (
    API_REQUEST_HEADERS,
    HTTPX_CLIENT,
    NICONICO_OAUTH_CLIENT_ID,
    NICONICO_TOKEN_ENCRYPTION_PREFIX,
    NICONICO_TOKEN_FERNET,
)
from app.utils import Interlaced


if TYPE_CHECKING:
    from app.models.AccountLink import AccountLink
    from app.models.BlueskyAccount import BlueskyAccount
    from app.models.RefreshToken import RefreshToken
    from app.models.TwitterAccount import TwitterAccount


def encodeClientSettingsJSON(value: object) -> str:
    """ClientSettings JSON を allow_nan=False でシリアライズする。"""

    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class NiconicoTokenDecryptionError(RuntimeError):
    """ニコニコ OAuth トークンを復号できない状態を表す。"""


class User(TortoiseModel):

    # データベース上のテーブル名
    class Meta(TortoiseModel.Meta):
        table: str = 'users'

    id = fields.IntField(pk=True)
    # ユーザー名はそのままログイン ID になるため、DB レベルの UNIQUE 制約で重複を最終保証する
    ## アプリケーション側の事前チェックだけでは、同時登録時に同じユーザー名のアカウントが複数作られてしまうため
    ## Tortoise の TextField は UNIQUE 制約をサポートしないため、CharField を利用する
    name = fields.CharField(max_length=64, unique=True)
    password = fields.TextField()
    is_admin = fields.BooleanField()
    token_version = fields.IntField(default=0)
    # allow_nan=False で NaN/Inf の DB 汚染を拒否する (Starlette JSONResponse も非有限値を拒否するため)
    client_settings = cast(
        TortoiseField[dict[str, Any]],
        fields.JSONField(default={}, encoder=encodeClientSettingsJSON),
    )  # type: ignore
    niconico_user_id = cast(TortoiseField[int | None], fields.IntField(null=True))
    niconico_user_name = cast(TortoiseField[str | None], fields.TextField(null=True))
    niconico_user_premium = cast(TortoiseField[bool | None], fields.BooleanField(null=True))
    # DB カラムには暗号文を保持し、公開プロパティ経由の読み書きだけで復号・暗号化する
    ## source_field で既存カラム名を維持し、既存 DB とスキーマ互換を保つ
    _niconico_access_token = cast(
        TortoiseField[str | None],
        fields.TextField(null=True, source_field='niconico_access_token'),
    )
    _niconico_refresh_token = cast(
        TortoiseField[str | None],
        fields.TextField(null=True, source_field='niconico_refresh_token'),
    )
    twitter_accounts: fields.ReverseRelation[TwitterAccount]
    bluesky_accounts: fields.ReverseRelation[BlueskyAccount]
    refresh_tokens: fields.ReverseRelation[RefreshToken]
    account_links: fields.ReverseRelation[AccountLink]
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)


    @staticmethod
    def _encryptNiconicoToken(plain_text: str | None) -> str | None:
        """ニコニコ OAuth トークンを暗号化する。

        Args:
            plain_text: 暗号化前のトークン。未連携時は None。

        Returns:
            接頭辞付きの暗号文。None はそのまま返す。
        """

        # 未連携状態は NULL のまま保存し、既存の連携判定を維持する
        if plain_text is None:
            return None

        # JWT シークレットから用途分離して導出した鍵で暗号化する
        encrypted_text = NICONICO_TOKEN_FERNET.encrypt(plain_text.encode('utf-8')).decode('utf-8')
        return f'{NICONICO_TOKEN_ENCRYPTION_PREFIX}{encrypted_text}'


    @staticmethod
    def _decryptNiconicoToken(encrypted_text: str | None) -> str | None:
        """DB に保存されているニコニコ OAuth トークンを復号する。

        Args:
            encrypted_text: DB から読み込んだ暗号文または旧形式の平文。

        Returns:
            復号済みのトークン。未連携時は None。

        Raises:
            NiconicoTokenDecryptionError: 鍵が異なるか暗号文が破損している場合。
        """

        # NULL と migration 適用前の平文を受理し、既存の永続データをそのまま利用可能にする
        if encrypted_text is None:
            return None
        if encrypted_text.startswith(NICONICO_TOKEN_ENCRYPTION_PREFIX) is False:
            return encrypted_text

        # 接頭辞を除去した Fernet トークンを復号し、失敗時は外部 API へ暗号文を送らず再連携を促す
        token = encrypted_text[len(NICONICO_TOKEN_ENCRYPTION_PREFIX):].encode('utf-8')
        try:
            return NICONICO_TOKEN_FERNET.decrypt(token).decode('utf-8')
        except InvalidToken as ex:
            logging.error('[User][_decryptNiconicoToken] Failed to decrypt Niconico OAuth token:', exc_info=ex)
            raise NiconicoTokenDecryptionError(
                'ニコニコの認証情報を復号できませんでした。設定画面から再連携してください。'
            ) from ex


    @property
    def niconico_access_token(self) -> str | None:
        """ニコニコ OAuth アクセストークンを復号して返す。

        Returns:
            復号済みのアクセストークン。未連携時は None。
        """

        return self._decryptNiconicoToken(self._niconico_access_token)


    @niconico_access_token.setter
    def niconico_access_token(self, plain_text: str | None) -> None:
        """ニコニコ OAuth アクセストークンを暗号化して保持する。

        Args:
            plain_text: 暗号化前のアクセストークン。連携解除時は None。

        Returns:
            None
        """

        self._niconico_access_token = self._encryptNiconicoToken(plain_text)


    @property
    def niconico_refresh_token(self) -> str | None:
        """ニコニコ OAuth リフレッシュトークンを復号して返す。

        Returns:
            復号済みのリフレッシュトークン。未連携時は None。
        """

        return self._decryptNiconicoToken(self._niconico_refresh_token)


    @niconico_refresh_token.setter
    def niconico_refresh_token(self, plain_text: str | None) -> None:
        """ニコニコ OAuth リフレッシュトークンを暗号化して保持する。

        Args:
            plain_text: 暗号化前のリフレッシュトークン。連携解除時は None。

        Returns:
            None
        """

        self._niconico_refresh_token = self._encryptNiconicoToken(plain_text)


    async def save(
        self,
        using_db: BaseDBAsyncClient | None = None,
        update_fields: Iterable[str] | None = None,
        force_create: bool = False,
        force_update: bool = False,
    ) -> None:
        """公開プロパティ名を内部 ORM フィールド名へ変換して保存する。

        Args:
            using_db: 保存に使用する DB 接続。
            update_fields: 更新対象のフィールド名。
            force_create: INSERT を強制するかどうか。
            force_update: UPDATE を強制するかどうか。

        Returns:
            None
        """

        # 呼び出し側の既存 update_fields 契約を維持し、Tortoise ORM にだけ実フィールド名を渡す
        if update_fields is not None:
            token_field_map = {
                'niconico_access_token': '_niconico_access_token',
                'niconico_refresh_token': '_niconico_refresh_token',
            }
            update_fields = [token_field_map.get(field_name, field_name) for field_name in update_fields]

        await super().save(
            using_db = using_db,
            update_fields = update_fields,
            force_create = force_create,
            force_update = force_update,
        )


    async def refreshNiconicoAccessToken(self) -> None:
        """
        このユーザーに紐づくニコニコアカウントのアクセストークンを、リフレッシュトークンで更新する
        更新されたアクセストークンはこのメソッド内でデータベースに永続化される

        Raises:
            Exception: アクセストークンの更新に失敗した場合 (例外に含まれるエラーメッセージを API レスポンスで返す想定)
        """

        # 実況機能が無効な間は、保持中の認証情報を変更せずニコニコへも接続しない
        if Config().general.jikkyo_enabled is False:
            raise RuntimeError('実況機能はサーバー設定で無効になっています。')

        try:

            # リフレッシュトークンを使い、ニコニコ OAuth のアクセストークンとリフレッシュトークンを更新
            token_api_url = 'https://oauth.nicovideo.jp/oauth2/token'
            async with HTTPX_CLIENT() as client:
                token_api_response = await client.post(
                    url = token_api_url,
                    headers = {**API_REQUEST_HEADERS, 'Content-Type': 'application/x-www-form-urlencoded'},
                    data = {
                        'grant_type': 'refresh_token',
                        'client_id': NICONICO_OAUTH_CLIENT_ID,
                        'client_secret': Interlaced(3),
                        'refresh_token': self.niconico_refresh_token,
                    },
                )

            # ステータスコードが 200 以外
            if token_api_response.status_code != 200:
                error_code = ''
                try:
                    error_code = f' ({token_api_response.json()["error"]})'
                except Exception:
                    pass
                raise Exception(f'アクセストークンの更新に失敗しました。(HTTP Error {token_api_response.status_code}{error_code})')

            token_api_response_json = token_api_response.json()

        # 接続エラー（サーバーメンテナンスやタイムアウトなど）
        except (httpx.NetworkError, httpx.TimeoutException):
            raise Exception('アクセストークンの更新リクエストがタイムアウトしました。')

        # 取得したアクセストークンとリフレッシュトークンをユーザーアカウントに設定
        ## 仕様上リフレッシュトークンに有効期限はないが、一応このタイミングでリフレッシュトークンも更新することが推奨されている
        self.niconico_access_token = str(token_api_response_json['access_token'])
        self.niconico_refresh_token = str(token_api_response_json['refresh_token'])
        update_fields = ['niconico_access_token', 'niconico_refresh_token', 'updated_at']

        try:
            # ついでなので、このタイミングでユーザー情報を取得し直す
            ## 頻繁に変わるものでもないとは思うけど、一応再ログインせずとも同期されるようにしておきたい
            ## 3秒応答がなかったらタイムアウト
            user_api_url = f'https://nvapi.nicovideo.jp/v1/users/{self.niconico_user_id}'
            async with HTTPX_CLIENT() as client:
                # X-Frontend-Id がないと INVALID_PARAMETER になる
                user_api_response = await client.get(user_api_url, headers={**API_REQUEST_HEADERS, 'X-Frontend-Id': '6'})

            if user_api_response.status_code == 200:
                # ユーザー名
                self.niconico_user_name = str(user_api_response.json()['data']['user']['nickname'])
                # プレミアム会員かどうか
                self.niconico_user_premium = bool(user_api_response.json()['data']['user']['isPremium'])
                update_fields.extend(['niconico_user_name', 'niconico_user_premium'])

        # 接続エラー（サーバー再起動やタイムアウトなど）
        except (httpx.NetworkError, httpx.TimeoutException):
            pass  # 取れなくてもセッション取得に支障はないのでパス

        # 外部 API の応答待ち中に更新されたパスワードや設定を古いモデルで上書きしないよう、変更列だけを保存
        await self.save(update_fields=update_fields)
