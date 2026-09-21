import json
from pathlib import Path
from uuid import UUID

import httpx
from typing_extensions import TypedDict

from app import logging


class KonomiTVBS4KCloudControlRequest(TypedDict):
    owner_id: int
    connection_id: str


class KonomiTVBS4KCloudListRequest(TypedDict):
    fs: str
    remote: str
    opt: KonomiTVBS4KCloudListOptions


class KonomiTVBS4KCloudListOptions(TypedDict):
    dirsOnly: bool
    recurse: bool


class KonomiTVBS4KCloudRC:
    """本体からのRC通信と認証変更前の停止確認を秘密非反射の境界に集約する。"""

    CONTROL_DIRECTORY = Path('/run/konomitv-bs4k-cloud')

    @staticmethod
    def request(endpoint: Path, operation: str, body: object) -> object:
        """Unix socketだけで要求し、応答サイズと待機時間を制限する。

        Args:
            endpoint: 内部で構成したUnix socket。
            operation: 内部呼出し元が選ぶRC操作。
            body: RC本文。ログ・例外へ含めない。
        Returns:
            RCのJSON応答。通常APIへそのまま渡してはならない。
        """
        try:
            with httpx.Client(transport=httpx.HTTPTransport(uds=str(endpoint)), trust_env=False, timeout=45) as client:
                with client.stream('POST', f'http://localhost/{operation}', json=body) as response:
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        if len(data) + len(chunk) > 4 * 1024 * 1024:
                            raise ConnectionError('Cloud RC response exceeded its limit.')
                        data.extend(chunk)
                    if response.status_code != 200:
                        # crypt内の個別オブジェクトの不在を、権限失効や通信失敗と区別して再試行する。
                        if response.status_code == 404:
                            raise FileNotFoundError('Cloud RC object was not found.')
                        # 原文・入力・remote名を残さず、再試行の判断に必要な分類だけを記録する。
                        try:
                            error = json.loads(data)
                        except ValueError:
                            error = None
                        message = error.get('error') if isinstance(error, dict) else None
                        text = message.lower() if isinstance(message, str) else ''
                        category = 'Unknown'
                        if any(term in text for term in ('quotaexceeded', 'quota exceeded', 'ratelimitexceeded')):
                            category = 'Quota'
                        elif any(term in text for term in ('invalid_grant', 'invalid_client', 'oauth')):
                            category = 'OAuth'
                        elif any(term in text for term in ('permission denied', 'insufficient permission')):
                            category = 'Permission'
                        logging.warning(f'[KonomiTVBS4KCloudRC] Request failed: HTTP {response.status_code}, category={category}.')
                        raise ConnectionError('Cloud RC request failed.')
            return json.loads(data)
        except (httpx.HTTPError, ValueError):
            # httpx例外やRC本文には設定値が含まれ得るので、呼出し元へ固定の失敗だけを渡す。
            raise ConnectionError('Cloud RC communication failed.') from None

    @classmethod
    def stop(cls, owner_id: int, connection_id: UUID | None = None) -> None:
        """ownerLock保持中に、その所有者の旧rclone終了を確認する。

        Args:
            owner_id: 認証変更または削除の対象所有者。
            connection_id: 一接続だけ止める場合のUUID。Noneは所有者全体。
        Returns:
            None。停止を確認できない場合は認証変更を許可せず例外。
        """
        markers = ([cls.CONTROL_DIRECTORY / f'{owner_id}_{connection_id}.active'] if connection_id is not None
                   else list(cls.CONTROL_DIRECTORY.glob(f'{owner_id}_*.active')))
        for marker in markers:
            # まだRCを起動していない接続には、停止対象となるプロセスが存在しない。
            if not marker.exists():
                continue
            connection = UUID(marker.stem.split('_', 1)[1])
            result = cls.request(cls.CONTROL_DIRECTORY / 'supervisor.sock', 'stop',
                                 KonomiTVBS4KCloudControlRequest(owner_id=owner_id, connection_id=str(connection)))
            if not isinstance(result, dict) or result.get('ready') is not False or marker.exists():
                raise ConnectionError('Cloud process shutdown was not confirmed.')

    @classmethod
    def start(cls, owner_id: int, connection_id: UUID) -> Path:
        """ownerLock保持中に接続を起動し、確認済みのRC接続先を返す。

        Args:
            owner_id: 現存を確認済みの所有者。
            connection_id: 存在確認済みの接続UUID。
        Returns:
            接続別のUnix socket。
        """
        result = cls.request(cls.CONTROL_DIRECTORY / 'supervisor.sock', 'start',
                             KonomiTVBS4KCloudControlRequest(owner_id=owner_id, connection_id=str(connection_id)))
        if not isinstance(result, dict) or result.get('ready') is not True:
            raise ConnectionError('Cloud process startup was not confirmed.')
        return cls.CONTROL_DIRECTORY / f'{owner_id}_{connection_id}.sock'

    @classmethod
    def listFolders(cls, owner_id: int, connection_id: UUID) -> list[str]:
        """ownerLock保持中に接続を起動し、同じrcloneでフォルダを読む。

        Args:
            owner_id: 現存を確認済みの所有者。
            connection_id: 存在確認済みの接続UUID。
        Returns:
            公開してよいフォルダ名だけ（最大100件）。
        """
        result = cls.request(cls.start(owner_id, connection_id), 'operations/list',
                             KonomiTVBS4KCloudListRequest(fs='connection:', remote='', opt=KonomiTVBS4KCloudListOptions(
                                 dirsOnly=True, recurse=False)))
        if not isinstance(result, dict) or not isinstance(result.get('list'), list):
            raise ConnectionError('Invalid cloud folder response.')
        return [item['Name'] for item in result['list'][:100]
                if isinstance(item, dict) and isinstance(item.get('Name'), str)]
