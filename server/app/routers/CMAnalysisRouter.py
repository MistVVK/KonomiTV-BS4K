from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from tortoise import transactions

from app import schemas
from app.constants import DATA_DIR, JST
from app.metadata.CMAnalysisPaths import ResolveCMHostPath, ValidateCMLogoDirectory
from app.metadata.CMLogoGenerator import UnavailableCMLogoGenerator
from app.metadata.CMLogoScanner import CMLogoScanner
from app.models.CMAnalysis import (
    CMAnalysisExcludedDirectory,
    CMAnalysisSettings,
    CMLogo,
    CMLogoServiceAssignment,
)
from app.models.User import User
from app.routers.UsersRouter import GetCurrentAdminUser


router = APIRouter(
    tags=['CM Analysis'],
    prefix='/api/cm-analysis',
)

_MAX_LOGO_UPLOAD_BYTES = 64 * 1024 * 1024


@router.get(
    '/settings',
    summary='CM解析設定取得 API',
    response_model=schemas.CMAnalysisSettings,
)
async def CMAnalysisSettingsAPI() -> schemas.CMAnalysisSettings:
    """サーバー全体で共有するCM解析設定を返す。"""

    settings, _ = await CMAnalysisSettings.get_or_create(id=1, defaults={'enabled': False})
    excluded_directories = await CMAnalysisExcludedDirectory.filter(enabled=True).order_by('id')
    return schemas.CMAnalysisSettings(
        enabled=settings.enabled,
        logo_directory=settings.logo_directory,
        excluded_directories=[excluded.path for excluded in excluded_directories],
    )


@router.get(
    '/capabilities',
    summary='CM解析機能対応状況取得 API',
    response_model=schemas.CMAnalysisCapabilities,
)
async def CMAnalysisCapabilitiesAPI() -> schemas.CMAnalysisCapabilities:
    """実行系を持たない将来機能も含め、現在の対応状況だけを返す。"""

    capability = UnavailableCMLogoGenerator().capability
    return schemas.CMAnalysisCapabilities(automatic_logo_generation=capability.availability)


@router.put(
    '/settings',
    summary='CM解析設定更新 API',
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def CMAnalysisSettingsUpdateAPI(
    new_settings: Annotated[schemas.CMAnalysisSettingsUpdate, Body(description='更新するCM解析設定。')],
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> None:
    """ロゴフォルダと除外パスを検証してCM解析設定を原子的に更新する。"""

    del current_user
    logo_directory: str | None = None
    if new_settings.logo_directory is not None and new_settings.logo_directory.strip() != '':
        host_logo_path = Path(new_settings.logo_directory.strip())
        await asyncio.to_thread(ValidateCMLogoDirectory, host_logo_path)
        logo_directory = str(host_logo_path)

    validated_exclusions: list[tuple[str, str]] = []
    seen_exclusions: set[str] = set()
    for raw_path in new_settings.excluded_directories:
        normalized_path = raw_path.strip()
        if normalized_path == '' or normalized_path in seen_exclusions:
            continue
        host_path = Path(normalized_path)
        if host_path.is_absolute() is False:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f'CM analysis exclusion must be an absolute path: {normalized_path}',
            )
        runtime_path = ResolveCMHostPath(host_path)
        resolved_path = await asyncio.to_thread(runtime_path.resolve)
        validated_exclusions.append((str(host_path), str(resolved_path)))
        seen_exclusions.add(normalized_path)

    async with transactions.in_transaction() as connection:
        settings, _ = await CMAnalysisSettings.get_or_create(
            id=1,
            defaults={'enabled': False},
            using_db=connection,
        )
        settings.enabled = new_settings.enabled
        settings.logo_directory = logo_directory
        await settings.save(using_db=connection)
        await CMAnalysisExcludedDirectory.all().using_db(connection).delete()
        await CMAnalysisExcludedDirectory.bulk_create([
            CMAnalysisExcludedDirectory(path=path, resolved_path=resolved_path, enabled=True)
            for path, resolved_path in validated_exclusions
        ], using_db=connection)


@router.get(
    '/logos',
    summary='CMロゴ一覧取得 API',
    response_model=list[schemas.CMLogo],
)
async def CMLogosAPI() -> list[schemas.CMLogo]:
    """共有ロゴフォルダを再スキャンして履歴を含む一覧を返す。"""

    await CMLogoScanner.scan()
    logos = await CMLogo.all().order_by('service_id', 'filename')
    return [schemas.CMLogo.model_validate(logo, from_attributes=True) for logo in logos]


@router.post(
    '/logos/rescan',
    summary='CMロゴ再スキャン API',
    response_model=list[schemas.CMLogo],
)
async def CMLogosRescanAPI(
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> list[schemas.CMLogo]:
    """管理者操作で共有ロゴフォルダを再スキャンする。"""

    del current_user
    logos = await CMLogoScanner.scan()
    return [schemas.CMLogo.model_validate(logo, from_attributes=True) for logo in logos]


@router.get(
    '/logos/{logo_id}/preview',
    summary='CMロゴプレビュー取得 API',
    response_class=FileResponse,
)
async def CMLogoPreviewAPI(logo_id: int) -> FileResponse:
    """AviUtl互換ピクセルをPillowでPNGへ変換し、ハッシュ単位でキャッシュする。"""

    logo = await CMLogo.get_or_none(id=logo_id)
    if logo is None or logo.missing or logo.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='CM logo was not found')
    logo_path = Path(logo.path)
    if await asyncio.to_thread(logo_path.exists) is False:
        logo_path = ResolveCMHostPath(logo_path)
    if await asyncio.to_thread(logo_path.exists) is False:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='CM logo file was not found')

    preview_directory = DATA_DIR / 'cm-analysis/logo-previews'
    await asyncio.to_thread(preview_directory.mkdir, parents=True, exist_ok=True)
    preview_path = preview_directory / f'{logo.file_hash}.png'
    if await asyncio.to_thread(preview_path.exists) is False:
        temporary_fd, temporary_name = tempfile.mkstemp(prefix=f'.{logo.file_hash}.', suffix='.png', dir=preview_directory)
        os.close(temporary_fd)
        temporary_path = Path(temporary_name)
        try:
            await asyncio.to_thread(CMLogoScanner.RenderPreview, logo_path, temporary_path)
            await asyncio.to_thread(os.replace, temporary_path, preview_path)
        except (OSError, ValueError) as ex:
            await asyncio.to_thread(temporary_path.unlink, missing_ok=True)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f'CM logo preview conversion failed: {ex!s}',
            ) from ex
        finally:
            await asyncio.to_thread(temporary_path.unlink, missing_ok=True)
    return FileResponse(preview_path, media_type='image/png')


@router.post(
    '/logos',
    summary='CMロゴアップロード API',
    status_code=status.HTTP_201_CREATED,
    response_model=schemas.CMLogo,
)
async def CMLogoUploadAPI(
    logo_file: Annotated[UploadFile, File(description='追加する単一ロゴ.lgdファイル。')],
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> schemas.CMLogo:
    """検証済みAviUtl v0.1単一ロゴまたは拡張v1だけを共有フォルダへ原子的に追加する。"""

    del current_user
    filename = Path(logo_file.filename or '').name
    if filename.lower().endswith('.lgd2'):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='LogoFormatUnsupported: .lgd2 and multi-logo files are not supported',
        )
    if filename == '' or filename.lower().endswith('.lgd') is False:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='A .lgd file is required')

    settings, _ = await CMAnalysisSettings.get_or_create(id=1, defaults={'enabled': False})
    configured_path = Path(settings.logo_directory) if settings.logo_directory else None
    runtime_directory = ResolveCMHostPath(configured_path) if configured_path else DATA_DIR / 'cm-analysis/logos'
    await asyncio.to_thread(runtime_directory.mkdir, parents=True, exist_ok=True)
    destination_path = runtime_directory / filename
    if await asyncio.to_thread(destination_path.exists):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='A logo with the same filename already exists')

    content = await logo_file.read(_MAX_LOGO_UPLOAD_BYTES + 1)
    if len(content) > _MAX_LOGO_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail='The CM logo file exceeds the 64 MiB limit',
        )
    temporary_fd, temporary_name = tempfile.mkstemp(prefix=f'.{filename}.', suffix='.tmp', dir=runtime_directory)
    temporary_path = Path(temporary_name)
    try:
        def WriteAndCommit() -> None:
            with os.fdopen(temporary_fd, 'wb') as temporary_file:
                os.fchmod(temporary_file.fileno(), 0o644)
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            CMLogoScanner.ReadMetadata(temporary_path)
            os.link(temporary_path, destination_path)

        await asyncio.to_thread(WriteAndCommit)
    except FileExistsError as ex:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='A logo with the same filename already exists') from ex
    except (OSError, ValueError) as ex:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(ex)) from ex
    finally:
        await asyncio.to_thread(temporary_path.unlink, missing_ok=True)

    await CMLogoScanner.scan()
    stored_path = str(configured_path / filename) if configured_path else str(destination_path)
    logo = await CMLogo.get(path=stored_path)
    return schemas.CMLogo.model_validate(logo, from_attributes=True)


@router.put(
    '/logos/{logo_id}',
    summary='CMロゴ設定更新 API',
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def CMLogoUpdateAPI(
    logo_id: int,
    update: Annotated[schemas.CMLogoUpdate, Body(description='更新するロゴ設定。')],
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> None:
    """共有ファイルを変更せずKonomiTV-BS4K側の有効状態だけを更新する。"""

    del current_user
    logo = await CMLogo.get_or_none(id=logo_id)
    if logo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='CM logo was not found')
    logo.enabled = update.enabled
    await logo.save(update_fields=['enabled', 'updated_at'])


@router.get(
    '/logo-assignments',
    summary='CMロゴサービス割り当て一覧 API',
    response_model=list[schemas.CMLogoServiceAssignment],
)
async def CMLogoAssignmentsAPI() -> list[schemas.CMLogoServiceAssignment]:
    """KonomiTV-BS4K固有のNID・TSID割り当てと「ロゴなし」指定を返す。"""

    assignments = await CMLogoServiceAssignment.all().order_by('network_id', 'transport_stream_id', 'service_id', 'id')
    return [schemas.CMLogoServiceAssignment.model_validate(assignment, from_attributes=True) for assignment in assignments]


@router.post(
    '/logo-assignments',
    summary='CMロゴサービス割り当て追加 API',
    status_code=status.HTTP_201_CREATED,
    response_model=schemas.CMLogoServiceAssignment,
)
async def CMLogoAssignmentCreateAPI(
    new_assignment: Annotated[schemas.CMLogoServiceAssignmentCreate, Body(description='追加するサービス割り当て。')],
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> schemas.CMLogoServiceAssignment:
    """SIDロゴまたは「ロゴなし」をNID・TSIDサービスへ明示的に割り当てる。"""

    del current_user
    if new_assignment.is_no_logo == (new_assignment.logo_id is not None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Specify exactly one of logo_id or is_no_logo',
        )
    if (
        new_assignment.valid_from is not None
        and new_assignment.valid_until is not None
        and new_assignment.valid_from > new_assignment.valid_until
    ):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Invalid applicability period')
    if new_assignment.logo_id is not None:
        logo = await CMLogo.get_or_none(id=new_assignment.logo_id)
        if logo is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='CM logo was not found')
        if logo.service_id is not None and logo.service_id != new_assignment.service_id:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Logo SID does not match service SID')
    assignment = await CMLogoServiceAssignment.create(**new_assignment.model_dump())
    return schemas.CMLogoServiceAssignment.model_validate(assignment, from_attributes=True)


@router.delete(
    '/logo-assignments/{assignment_id}',
    summary='CMロゴサービス割り当て削除 API',
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def CMLogoAssignmentDeleteAPI(
    assignment_id: int,
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> None:
    """KonomiTV-BS4K固有の割り当てだけを削除し、共有ロゴは変更しない。"""

    del current_user
    assignment = await CMLogoServiceAssignment.get_or_none(id=assignment_id)
    if assignment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='CM logo assignment was not found')
    await assignment.delete()


@router.delete(
    '/logos/{logo_id}',
    summary='CM共有ロゴ削除 API',
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def CMLogoDeleteAPI(
    logo_id: int,
    confirm_shared_deletion: Annotated[bool, Query(description='共有フォルダからの削除確認。')],
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> None:
    """明示確認後に共有.lgdを削除し、DB履歴はmissingとして保持する。"""

    del current_user
    if confirm_shared_deletion is False:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Shared logo deletion must be confirmed')
    logo = await CMLogo.get_or_none(id=logo_id)
    if logo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='CM logo was not found')
    settings, _ = await CMAnalysisSettings.get_or_create(id=1, defaults={'enabled': False})
    runtime_path = ResolveCMHostPath(Path(logo.path)) if settings.logo_directory else Path(logo.path)
    try:
        await asyncio.to_thread(runtime_path.unlink, missing_ok=True)
    except OSError as ex:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(ex)) from ex
    logo.missing = True
    logo.deleted_at = datetime.now(tz=JST)
    await logo.save(update_fields=['missing', 'deleted_at', 'updated_at'])
