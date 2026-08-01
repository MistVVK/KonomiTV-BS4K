from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path
from typing import ClassVar, Literal

from app import logging


class TSCodecBridgeRuntimeVerifier:
    """固定runtime manifestとBridge実体を初回利用時に照合する。"""

    _lock: ClassVar[threading.Lock] = threading.Lock()
    _cached_signature: ClassVar[tuple[object, ...] | None] = None
    _cached_result: ClassVar[bool] = False
    _process_counter_lock: ClassVar[threading.Lock] = threading.Lock()
    _process_generation: ClassVar[str] = uuid.uuid4().hex
    _process_start_count: ClassVar[int] = 0
    _live_process_start_count: ClassVar[int] = 0
    _probe_process_start_count: ClassVar[int] = 0
    _verification_process_start_count: ClassVar[int] = 0
    _command_timeout_seconds: ClassVar[float] = 2.0
    _required_manifest_keys: ClassVar[tuple[str, ...]] = (
        'CLI_VERSION',
        'TS_MAPPING_VERSION',
        'EXECUTABLE_SHA256',
    )

    @staticmethod
    def __pathSignature(path: Path) -> tuple[object, ...]:
        """存在・置換・属性変更をcache世代へ反映するlstat署名を返す。"""

        try:
            stat = path.lstat()
        except OSError as ex:
            return ('missing', type(ex).__name__)
        return (
            stat.st_dev,
            stat.st_ino,
            stat.st_mode,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )

    @classmethod
    def __runtimeSignature(
        cls,
        executable_path: Path,
        manifest_path: Path,
    ) -> tuple[object, ...]:
        """実行形式とmanifestを一体の検証世代として扱う。"""

        return (
            *cls.__pathSignature(executable_path),
            *cls.__pathSignature(manifest_path),
        )

    @classmethod
    def __readManifest(cls, manifest_path: Path) -> dict[str, str]:
        """runtime manifestから一意な契約値だけを厳格に取得する。"""

        if manifest_path.is_symlink() or manifest_path.is_file() is False:
            raise ValueError('Runtime manifest is not a regular file.')
        manifest_bytes = manifest_path.read_bytes()
        if len(manifest_bytes) == 0 or len(manifest_bytes) > 1024 * 1024:
            raise ValueError('Runtime manifest size is invalid.')
        manifest = manifest_bytes.decode('utf-8')
        values: dict[str, str] = {}
        for line in manifest.splitlines():
            if not line:
                continue
            fields = line.split('\t')
            if len(fields) < 2 or not fields[0]:
                raise ValueError('Runtime manifest contains a malformed row.')
            key = fields[0]
            if key not in cls._required_manifest_keys:
                continue
            if len(fields) != 2 or key in values:
                raise ValueError(f'Runtime manifest contains an invalid {key} row.')
            values[key] = fields[1]
        if set(values) != set(cls._required_manifest_keys):
            raise ValueError('Runtime manifest is missing a required contract row.')
        if re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', values['CLI_VERSION']) is None:
            raise ValueError('Runtime manifest CLI version is invalid.')
        if re.fullmatch(r'[0-9]+', values['TS_MAPPING_VERSION']) is None:
            raise ValueError('Runtime manifest mapping version is invalid.')
        if re.fullmatch(r'[0-9a-f]{64}', values['EXECUTABLE_SHA256']) is None:
            raise ValueError('Runtime manifest executable digest is invalid.')
        return values

    @staticmethod
    def __sha256(path: Path) -> str:
        """実行形式を固定量ずつ読み、manifest照合用SHA-256を返す。"""

        digest = hashlib.sha256()
        with path.open('rb') as executable:
            while chunk := executable.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def __runVersionCommand(cls, executable_path: Path, option: str) -> str:
        """shellを介さずBridgeの版表示を取得し、余分なstderrや複数行を拒否する。"""

        try:
            completed = subprocess.run(
                [str(executable_path), option],
                check = True,
                capture_output = True,
                text = True,
                timeout = cls._command_timeout_seconds,
            )
        except (
            UnicodeError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ):
            cls.recordProcessStart('verification')
            raise
        cls.recordProcessStart('verification')
        lines = completed.stdout.splitlines()
        if len(lines) != 1 or completed.stderr:
            raise ValueError(f'Bridge {option} output is invalid.')
        return lines[0]

    @classmethod
    def __verify(cls, executable_path: Path, manifest_path: Path) -> bool:
        """一つの不一致も利用可能扱いにせず、固定runtime契約を全て照合する。"""

        try:
            if (
                executable_path.is_symlink()
                or executable_path.is_file() is False
                or os.access(executable_path, os.X_OK) is False
            ):
                return False
            manifest = cls.__readManifest(manifest_path)
            if cls.__sha256(executable_path) != manifest['EXECUTABLE_SHA256']:
                return False
            if (
                cls.__runVersionCommand(executable_path, '--version')
                != manifest['CLI_VERSION']
            ):
                return False
            return (
                cls.__runVersionCommand(executable_path, '--mapping-version')
                == manifest['TS_MAPPING_VERSION']
            )
        except (
            OSError,
            UnicodeError,
            ValueError,
            subprocess.SubprocessError,
        ) as ex:
            logging.error(
                '[TSCodecBridge] Runtime verification failed: '
                f'{type(ex).__name__}: {ex}'
            )
            return False

    @classmethod
    def isAvailable(cls, executable: str | Path) -> bool:
        """同じruntime世代は一度だけ検証し、差替え時には必ず再照合する。"""

        executable_path = Path(executable)
        manifest_path = executable_path.parent / 'Runtime-Manifest.tsv'
        with cls._lock:
            signature_before = cls.__runtimeSignature(
                executable_path,
                manifest_path,
            )
            if cls._cached_signature == signature_before:
                return cls._cached_result
            result = cls.__verify(executable_path, manifest_path)
            signature_after = cls.__runtimeSignature(
                executable_path,
                manifest_path,
            )
            # 検証中にどちらかが差し替わった世代は成功としてcacheしない。
            if signature_after != signature_before:
                result = False
            cls._cached_signature = signature_after
            cls._cached_result = result
            return result

    @classmethod
    def recordProcessStart(
        cls,
        kind: Literal['live', 'probe', 'verification'],
    ) -> None:
        """Bridge processの起動成功回数を用途別に単調増加させる。"""

        with cls._process_counter_lock:
            cls._process_start_count += 1
            if kind == 'live':
                cls._live_process_start_count += 1
            elif kind == 'probe':
                cls._probe_process_start_count += 1
            else:
                cls._verification_process_start_count += 1

    @classmethod
    def getProcessStartSnapshot(cls) -> tuple[str, int, int, int, int]:
        """互換API隔離の前後比較に使うprocess世代と用途別回数を返す。"""

        with cls._process_counter_lock:
            return (
                cls._process_generation,
                cls._process_start_count,
                cls._live_process_start_count,
                cls._probe_process_start_count,
                cls._verification_process_start_count,
            )
