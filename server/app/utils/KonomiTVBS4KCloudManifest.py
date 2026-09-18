import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from app import schemas


# 通常API側の子型を先に解決してから、別moduleの共有目録へ組み込む。
schemas.RecordedVideo.model_rebuild()
schemas.RecordedProgram.model_rebuild()


class KonomiTVBS4KCloudManifest(BaseModel):
    """クラウド上の完成録画を、ローカルDBの採番に依存せず再登録できる目録。"""

    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1] = 1
    recording_uuid: UUID
    generation: Annotated[int, Field(ge=1)] = 1
    change_uuid: UUID
    files: Annotated[list[KonomiTVBS4KCloudManifestFile], Field(min_length=1, max_length=32)]
    program: schemas.RecordedProgram

    @field_serializer('program')
    def serializeProgram(self, program: schemas.RecordedProgram) -> dict[str, Any]:
        """目録内ではホストパス変換を使わず、検証済みの論理名をそのままJSONへ渡す。

        Args:
            program: ファイル名だけを保持する共有番組情報。
        Returns:
            通常APIと同じ型構造の値。日時等のJSON変換は外側のPydanticに任せる。
        """
        return program.model_dump(mode='python')

    @model_validator(mode='after')
    def validateFiles(self) -> KonomiTVBS4KCloudManifest:
        """録画本体が一つで、全参照がこの録画の不変な世代内に閉じていることを確認する。

        Args:
            None
        Returns:
            検証済みの目録。
        """
        if sum(item.kind == 'Recording' for item in self.files) != 1:
            raise ValueError('A cloud manifest must contain exactly one recording.')
        if len({item.name for item in self.files}) != len(self.files):
            raise ValueError('Duplicate cloud manifest file name.')
        source = next(item for item in self.files if item.kind == 'Recording')
        if source.size <= 0:
            raise ValueError('A cloud recording must not be empty.')
        if self.program.recorded_video.file_path != source.name or self.program.recorded_video.file_size != source.size:
            raise ValueError('Cloud metadata must contain only the portable source name.')
        # 読む側の上限を超える目録を公開して原本を消すことがないよう、準備時点で同じ上限を適用する。
        if len(self.model_dump_json().encode('utf-8')) > 16 * 1024 * 1024:
            raise ValueError('Cloud manifest exceeds its size limit.')
        return self


class KonomiTVBS4KCloudManifestFile(BaseModel):
    """全量検証に使うサイズ・ハッシュと復元先導出に必要な論理名。"""

    model_config = ConfigDict(extra='forbid')
    name: Annotated[str, Field(min_length=1, max_length=255)]
    kind: Literal['Recording', 'Sidecar', 'Thumbnail']
    size: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
    modified_ns: Annotated[int, Field(ge=0)]
    generation_uuid: UUID | None = None

    def relativePath(self, recording_uuid: UUID) -> str:
        """録画本体・初期付随物と、新世代の解析結果を不変な別キーへ配置する。

        Args:
            recording_uuid: 所属する録画UUID。
        Returns:
            同じ録画領域から外れない論理パス。
        """
        directory = 'files' if self.generation_uuid is None else f'generations/{self.generation_uuid}'
        return f'recordings/{recording_uuid}/{directory}/{self.name}'

    @field_validator('name')
    @classmethod
    def validateName(cls, value: str) -> str:
        """クラウド目録が任意のローカルパスや別remoteを参照することを防ぐ。

        Args:
            value: 目録内のファイル名。
        Returns:
            パス成分を含まない論理名。
        """
        if (value in ('.', '..') or value != value.strip() or
            any(ord(char) < 32 or char in '/\\:' for char in value)):
            raise ValueError('Invalid cloud manifest file name.')
        return value


class KonomiTVBS4KCloudLocalFile(BaseModel):
    """転送中の置換・変更を検出するローカル専用の対象集合。クラウドへ公開しない。"""

    model_config = ConfigDict(extra='forbid')
    path: str
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    remove_source: bool

    @classmethod
    def capture(
        cls, path: Path, *, remove_source: bool, cancellation_requested: Callable[[], bool] | None = None,
    ) -> tuple[KonomiTVBS4KCloudLocalFile, str]:
        """通常ファイルを全量ハッシュし、読取り中に変化していないfingerprintと共に返す。

        Args:
            path: 呼出し元が録画共有の範囲内と確認した通常ファイル。
            remove_source: 公開後の元清掃で削除できる、この録画専有のファイルか。
            cancellation_requested: 巨大ファイルのハッシュ計算を中断するための取消確認。
        Returns:
            ローカル対象情報とSHA-256。
        """
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('Cloud transfer source must be a regular file.')
            hasher = hashlib.sha256()
            while chunk := source.read(1024 * 1024):
                if cancellation_requested is not None and cancellation_requested():
                    raise InterruptedError('Cloud source hashing was interrupted.')
                hasher.update(chunk)
            digest = hasher.hexdigest()
            item = cls(path=str(path), device=before.st_dev, inode=before.st_ino, size=before.st_size,
                       modified_ns=before.st_mtime_ns, changed_ns=before.st_ctime_ns, remove_source=remove_source)
            # 開いたinodeだけでなく、名前が別ファイルに差し替えられた場合も検出する。
            item.validateStat(os.fstat(source.fileno()))
            item.validateStat(path.stat(follow_symlinks=False))
            return item, digest

    def validateStat(self, current: os.stat_result) -> None:
        """対象集合を確定した後の書換え・置換・非通常ファイル化を拒否する。

        Args:
            current: 開いたファイルまたはパスの最新stat。
        Returns:
            None。変更されている場合は原本清掃へ進まず例外を返す。
        """
        if (not stat.S_ISREG(current.st_mode) or
            (self.device, self.inode, self.size, self.modified_ns, self.changed_ns) !=
            (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns)):
            raise ValueError('Cloud transfer source changed.')


class KonomiTVBS4KCloudTransferPlan(BaseModel):
    """公開用目録とローカル専用の清掃対象を分けてSQLiteへ永続化する。"""

    model_config = ConfigDict(extra='forbid')
    manifest: KonomiTVBS4KCloudManifest
    local_files: list[KonomiTVBS4KCloudLocalFile]

    @model_validator(mode='after')
    def validateLocalFiles(self) -> KonomiTVBS4KCloudTransferPlan:
        """配列の取り違えによる、未検証ファイルの清掃を防ぐ。

        Args:
            None
        Returns:
            対象集合の順序とサイズが一致した転送計画。
        """
        if len(self.local_files) != len(self.manifest.files):
            raise ValueError('Cloud transfer file set is incomplete.')
        if len({item.path for item in self.local_files}) != len(self.local_files):
            raise ValueError('Duplicate cloud transfer source path.')
        for local, remote in zip(self.local_files, self.manifest.files, strict=True):
            if local.size != remote.size or Path(local.path).name != remote.name:
                raise ValueError('Cloud transfer file identities do not match.')
        return self


class KonomiTVBS4KCloudDeletion(BaseModel):
    """本体の清掃より先に永続化し、遅延した変更記録による復活を防ぐ削除マーカー。"""

    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1] = 1
    recording_uuid: UUID
    deletion_uuid: UUID


class KonomiTVBS4KCloudPublicationPlan(BaseModel):
    """追記する全体目録と、この世代で新たに転送する生成物だけを保持する。"""

    model_config = ConfigDict(extra='forbid')
    manifest: KonomiTVBS4KCloudManifest
    local_files: list[KonomiTVBS4KCloudLocalFile]

    @model_validator(mode='after')
    def validateUploads(self) -> KonomiTVBS4KCloudPublicationPlan:
        """解析公開で原本や他世代のファイルを上書きさせない。

        Args:
            None
        Returns:
            新しい世代内のファイルだけを参照する公開計画。
        """
        if len({Path(item.path).name for item in self.local_files}) != len(self.local_files):
            raise ValueError('Duplicate publication source.')
        for local in self.local_files:
            remote = next((item for item in self.manifest.files if item.name == Path(local.path).name), None)
            if (remote is None or remote.kind == 'Recording' or local.remove_source or local.size != remote.size or
                remote.generation_uuid != self.manifest.change_uuid):
                raise ValueError('Publication must only create new analysis artifacts.')
        return self


# 親目録を先に定義する並びを保ち、継承した番組スキーマの前方参照を公開前に確定する。
KonomiTVBS4KCloudManifest.model_rebuild()
KonomiTVBS4KCloudTransferPlan.model_rebuild()
