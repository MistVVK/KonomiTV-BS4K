from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol


CMLogoGenerationAvailability = Literal['Unavailable']


@dataclass(frozen=True, slots=True)
class CMLogoGenerationCapability:
    """将来の自動ロゴ作成実装を差し替えるための能力表現。"""

    availability: CMLogoGenerationAvailability = 'Unavailable'
    reason_code: str = 'NotImplemented'


@dataclass(frozen=True, slots=True)
class CMLogoGenerationResult:
    """自動ロゴ作成の実行結果。現行実装は常にUnavailableを返す。"""

    succeeded: bool = False
    output_path: Path | None = None
    exit_code: int | None = None
    error_message: str | None = 'AutomaticLogoGenerationUnavailable'


class CMLogoGenerator(Protocol):
    """特定の外部ツールに依存しない将来実装向けの最小契約。"""

    @property
    def capability(self) -> CMLogoGenerationCapability:
        ...

    async def generate(self, *_args: object, **_kwargs: object) -> CMLogoGenerationResult:
        ...


class UnavailableCMLogoGenerator:
    """自動作成を実行せず、安定したUnavailableを返す現行実装。"""

    @property
    def capability(self) -> CMLogoGenerationCapability:
        return CMLogoGenerationCapability()

    async def generate(self, *_args: object, **_kwargs: object) -> CMLogoGenerationResult:
        return CMLogoGenerationResult()
