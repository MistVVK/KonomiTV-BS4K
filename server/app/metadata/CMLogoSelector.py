from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from app.metadata.CMLogoScanner import CMLogoFileMetadata, CMLogoScanner
from app.models.CMAnalysis import CMLogo, CMLogoServiceAssignment
from app.models.RecordedVideo import RecordedVideo
from app.utils.HostPath import ToRuntimePath


CMLogoSelectionStatus = Literal['Selected', 'NoLogo', 'Missing', 'Ambiguous']


@dataclass(frozen=True, slots=True)
class CMLogoSelection:
    """録画サービスへ適用する共有ロゴの選択結果。"""

    status: CMLogoSelectionStatus
    logos: tuple[CMLogo, ...] = ()
    runtime_paths: tuple[Path, ...] = ()


class CMLogoSelector:
    """SID共有ロゴとKonomiTV-BS4K固有のNID・TSID割り当てを分離して選択する。"""

    @staticmethod
    async def select(
        recorded_video: RecordedVideo,
        logo_directory: Path | None,
        *,
        resolved_video_size: tuple[int, int] | None = None,
    ) -> CMLogoSelection:
        """明示割り当てを優先し、SID共有ロゴは全候補を内容照合に渡す。

        Args:
            recorded_video: 解析対象の録画。
            logo_directory: 共有ロゴフォルダの実行時パス。未設定時はロゴ path をそのまま使う。
            resolved_video_size: 実測解像度。未指定時は録画メタの解像度を使う。

        Returns:
            選択結果。
        """

        recorded_program = recorded_video.recorded_program
        channel = recorded_program.channel
        service_id = recorded_program.service_id
        if service_id is None:
            return CMLogoSelection(status='Missing')

        network_id = recorded_program.network_id
        transport_stream_id = channel.transport_stream_id if channel is not None else None
        reference_time = recorded_program.start_time
        video_width, video_height = (
            resolved_video_size
            if resolved_video_size is not None
            else (recorded_video.video_resolution_width, recorded_video.video_resolution_height)
        )
        if network_id is not None and transport_stream_id is not None:
            assignments = await CMLogoServiceAssignment.filter(
                network_id=network_id,
                transport_stream_id=transport_stream_id,
                service_id=service_id,
                enabled=True,
            ).prefetch_related('logo')
            applicable_assignments = [
                assignment
                for assignment in assignments
                if CMLogoSelector._isApplicable(assignment.valid_from, assignment.valid_until, reference_time)
            ]
            if len(applicable_assignments) > 1:
                return CMLogoSelection(status='Ambiguous')
            if len(applicable_assignments) == 1:
                assignment = applicable_assignments[0]
                if assignment.is_no_logo:
                    return CMLogoSelection(status='NoLogo')
                if assignment.logo is None or assignment.logo.enabled is False or assignment.logo.missing:
                    return CMLogoSelection(status='Missing')
                return await CMLogoSelector._selected(
                    (assignment.logo,),
                    logo_directory,
                    video_width,
                    video_height,
                )

        logos = await CMLogo.filter(service_id=service_id, enabled=True, missing=False, deleted_at=None)
        if len(logos) == 0:
            return CMLogoSelection(status='Missing')
        return await CMLogoSelector._selected(
            tuple(logos),
            logo_directory,
            video_width,
            video_height,
        )

    @staticmethod
    async def _selected(
        logos: tuple[CMLogo, ...],
        logo_directory: Path | None,
        video_width: int | None,
        video_height: int | None,
    ) -> CMLogoSelection:
        """DB保存パスを実行時パスへ変換し、入力キャンバスと互換なロゴだけを返す。"""

        runtime_paths = tuple(
            ToRuntimePath(Path(logo.path)) if logo_directory is not None else Path(logo.path)
            for logo in logos
        )
        compatible: list[tuple[CMLogo, Path]] = []
        for logo, runtime_path in zip(logos, runtime_paths, strict=True):
            try:
                metadata = await asyncio.to_thread(CMLogoScanner.ReadMetadata, runtime_path)
            except (OSError, ValueError):
                continue
            if CMLogoSelector._isResolutionCompatible(metadata, video_width, video_height):
                compatible.append((logo, runtime_path))
        if len(compatible) == 0:
            return CMLogoSelection(status='Missing')
        return CMLogoSelection(
            status='Selected',
            logos=tuple(logo for logo, _ in compatible),
            runtime_paths=tuple(path for _, path in compatible),
        )

    @staticmethod
    def _isResolutionCompatible(
        metadata: CMLogoFileMetadata,
        video_width: int | None,
        video_height: int | None,
    ) -> bool:
        """拡張形式だけ、生成元キャンバスと入力解像度が一致するか判定する。"""

        if video_width is None or video_height is None:
            return True
        # AviUtl標準v0.1には生成元キャンバスが記録されていない。
        # ロゴ矩形だけから放送解像度を推測せず、明示割り当てされたものをLogoFrameへ渡す。
        if metadata.image_width is None or metadata.image_height is None:
            return True
        return metadata.image_width == video_width and metadata.image_height == video_height

    @staticmethod
    def _isApplicable(valid_from: datetime | None, valid_until: datetime | None, target: datetime) -> bool:
        """適用期間を両端包含として判定する。"""

        return (valid_from is None or valid_from <= target) and (valid_until is None or target <= valid_until)
