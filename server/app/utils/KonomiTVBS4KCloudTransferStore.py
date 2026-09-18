from uuid import UUID, uuid4

from tortoise import timezone
from tortoise.expressions import F
from tortoise.transactions import in_transaction

from app.models.KonomiTVBS4KCloudCatalogSync import (
    KonomiTVBS4KCloudDeletedRecording,
    KonomiTVBS4KCloudPublication,
)
from app.models.KonomiTVBS4KCloudDestination import KonomiTVBS4KCloudDestination
from app.models.KonomiTVBS4KCloudTransfer import (
    KonomiTVBS4KCloudRecording,
    KonomiTVBS4KCloudTransfer,
    KonomiTVBS4KCloudTransferDirection,
    KonomiTVBS4KCloudTransferPhase,
)
from app.models.RecordedVideo import RecordedVideo
from app.utils.KonomiTVBS4KCloudManifest import KonomiTVBS4KCloudTransferPlan


class KonomiTVBS4KCloudTransferConflict(ValueError):
    """要求の再利用、所在の不一致、または古いworkerによる状態更新を拒否する。"""


class KonomiTVBS4KCloudTransferStore:
    """外部I/Oから独立したSQLiteトランザクションで移動の状態を更新する。"""

    @staticmethod
    async def enqueue(
        request_id: UUID, recorded_video_id: int, requested_by: int,
        direction: KonomiTVBS4KCloudTransferDirection, *, local_path: str, key_identity: str, key_revision: str,
    ) -> KonomiTVBS4KCloudTransfer:
        """同じ要求を一度だけ受付し、最初に選択した保存先を保持する。

        Args:
            request_id: クライアントが再送でも維持する要求UUID。
            recorded_video_id: 対象録画のDB ID。
            requested_by: 認可済み管理者のDB ID。
            direction: 所在から推測せず要求に明記する移動方向。
            local_path: 呼出し元で検証済みの転送元または復元先canonical path。
            key_identity: 確認時の鍵内容を表す非公開identity。
            key_revision: UIが確認した一覧revision。再送時も変更しない。
        Returns:
            受付済みジョブ。外部転送はまだ開始しない。
        """
        if direction not in ('ToCloud', 'ToLocal') or not local_path:
            raise ValueError('Invalid cloud transfer request.')
        async with in_transaction() as connection:
            existing = await KonomiTVBS4KCloudTransfer.filter(id=request_id).using_db(connection).get_or_none()
            if existing is not None:
                # 設定や現在の所在が変わっても、同じ要求は最初の応答へ収束させる。
                if (existing.recorded_video_id != recorded_video_id or existing.requested_by != requested_by or
                    existing.direction != direction or existing.local_path != local_path or
                    existing.key_identity != key_identity or existing.key_revision != key_revision):
                    raise KonomiTVBS4KCloudTransferConflict('Transfer request ID was reused with different inputs.')
                return existing
            video = await RecordedVideo.filter(id=recorded_video_id).using_db(connection).get_or_none()
            if video is None or video.status != 'Recorded':
                raise KonomiTVBS4KCloudTransferConflict('Only completed recordings can be moved.')
            if await KonomiTVBS4KCloudTransfer.filter(active_video_id=recorded_video_id).using_db(connection).exists():
                raise KonomiTVBS4KCloudTransferConflict('A transfer already owns this recording.')
            recording = await KonomiTVBS4KCloudRecording.filter(recorded_video_id=recorded_video_id).using_db(connection).get_or_none()
            location = recording.location if recording is not None else 'Local'
            if location != ('Local' if direction == 'ToCloud' else 'Cloud'):
                raise KonomiTVBS4KCloudTransferConflict('Recording location does not match the requested direction.')
            if direction == 'ToLocal' and recording is not None and recording.key_identity != key_identity:
                raise KonomiTVBS4KCloudTransferConflict('Cloud recording requires its original crypt key.')
            if direction == 'ToLocal' and await KonomiTVBS4KCloudPublication.filter(recorded_video_id=recorded_video_id).exclude(
                status__in=['Completed', 'Superseded'],
            ).using_db(connection).exists():
                raise KonomiTVBS4KCloudTransferConflict('Cloud analysis publication must complete before restoration.')
            if direction == 'ToCloud':
                destination = await KonomiTVBS4KCloudDestination.filter(upload_slot=1).using_db(connection).get_or_none()
                if destination is None:
                    raise KonomiTVBS4KCloudTransferConflict('No cloud upload destination is selected.')
                if video.file_path != local_path:
                    raise KonomiTVBS4KCloudTransferConflict('Recording source path changed.')
                owner_id, connection_id, folder = destination.owner_id, destination.connection_id, destination.folder
                if await KonomiTVBS4KCloudRecording.filter(location='Cloud', owner_id=owner_id, connection_id=connection_id,
                    folder=folder).exclude(key_identity=key_identity).using_db(connection).exists():
                    raise KonomiTVBS4KCloudTransferConflict('Cloud destination requires its original crypt key.')
            else:
                # Cloud状態は必ず所在レコードを持つ。アップロード先の設定は参照しない。
                if recording is None:
                    raise KonomiTVBS4KCloudTransferConflict('Cloud recording location is unavailable.')
                owner_id, connection_id, folder = recording.owner_id, recording.connection_id, recording.folder
            # 復元時にクラウドの旧UUIDへ削除マーカーを残すため、再アップロードには新しいUUIDを割り当てる。
            recording_uuid = recording.id if direction == 'ToLocal' and recording is not None else uuid4()
            if direction == 'ToCloud':
                # 復元済み録画の再移動は新しいUUIDで予約する。ORMの主キーupdateは使わない。
                # 旧ジョブはUUID値を履歴として保持しており、所在予約の置換で履歴を変更しない。
                if recording is not None:
                    await recording.delete(using_db=connection)
                await KonomiTVBS4KCloudRecording.create(
                    id=recording_uuid, recorded_video_id=recorded_video_id, location='Local',
                    owner_id=owner_id, connection_id=connection_id, folder=folder, original_path=local_path,
                    key_identity=key_identity,
                    using_db=connection,
                )
            return await KonomiTVBS4KCloudTransfer.create(
                id=request_id, recorded_video_id=recorded_video_id, recording_uuid=recording_uuid,
                requested_by=requested_by, direction=direction, active_video_id=recorded_video_id,
                owner_id=owner_id, connection_id=connection_id, folder=folder, local_path=local_path,
                key_identity=key_identity, key_revision=key_revision,
                using_db=connection,
            )

    @staticmethod
    async def cancel(job_id: UUID) -> None:
        """公開開始前の取消要求を保存し、開始済みなら清掃完了まで占有を保持する。

        Args:
            job_id: 管理者が取消を指定した要求UUID。
        Returns:
            None。同じ取消の再送は成功とし、公開が始まったジョブは拒否する。
        """
        async with in_transaction() as connection:
            job = await KonomiTVBS4KCloudTransfer.filter(id=job_id).using_db(connection).get_or_none()
            if job is None:
                raise KonomiTVBS4KCloudTransferConflict('Transfer is unavailable.')
            # 応答を失った取消の再送でも、新しいジョブや逆方向の移動を開始しない。
            if job.cancel_requested:
                return
            # Publishingへの遷移は公開I/Oより前にcommitする。応答喪失で公開の成否が不明でも取消しない。
            if job.phase not in ('Preparing', 'Copying', 'Verifying'):
                raise KonomiTVBS4KCloudTransferConflict('Publication has started; transfer cannot be cancelled.')
            job.cancel_requested = True
            # 一度もclaimされていない要求だけは外部の副作用がないため、その場で取消を完了できる。
            if job.status == 'Pending' and job.phase == 'Preparing' and job.attempt == 0 and job.manifest is None:
                job.status = 'Cancelled'
                job.phase = 'Cancelled'
                job.active_video_id = None
                if job.direction == 'ToCloud':
                    await KonomiTVBS4KCloudRecording.filter(
                        id=job.recording_uuid, recorded_video_id=job.recorded_video_id, location='Local',
                    ).using_db(connection).delete()
            elif job.status == 'Failed':
                # 失敗中の転送でも取消を実行できるよう再びworkerへ渡す。方向や対象集合は変えない。
                job.status = 'Pending'
                job.error_code = None
            # Runningのworker_tokenは取り上げない。所有workerが転送停止を確認してから清掃へ進める。
            await job.save(using_db=connection)

    @staticmethod
    async def beginCancellation(job_id: UUID, worker_token: UUID) -> None:
        """所有workerが書込み停止を確認した後に、取消清掃の開始を保存する。

        Args:
            job_id: 取消を要求されたジョブUUID。
            worker_token: claim時の実行UUID。古いrcloneとローカル書込みの停止確認後にだけ渡す。
        Returns:
            None。占有と対象集合を保持し、ファイルはまだ削除しない。
        """
        # 開始済みI/Oを止めずに別workerへ清掃を移譲してはならない。停止確認は呼出し元の責務。
        changed = await KonomiTVBS4KCloudTransfer.filter(
            id=job_id, worker_token=worker_token, status='Running', cancel_requested=True,
            phase__in=['Preparing', 'Copying', 'Verifying', 'Cancelling'],
        ).update(phase='Cancelling', updated_at=timezone.now())
        if changed != 1:
            raise KonomiTVBS4KCloudTransferConflict('Cancellation worker or publication state changed.')

    @staticmethod
    async def claim(job_id: UUID, worker_token: UUID) -> KonomiTVBS4KCloudTransfer | None:
        """未処理のジョブを単一workerだけが占有する。

        Args:
            job_id: 永続要求UUID。
            worker_token: workerがこの実行だけに生成したUUID。
        Returns:
            占有したジョブ。他workerが占有済みならNone。
        """
        async with in_transaction() as connection:
            updated = await KonomiTVBS4KCloudTransfer.filter(id=job_id, status='Pending').using_db(connection).update(
                status='Running', worker_token=worker_token, attempt=F('attempt') + 1,
                error_code=None, updated_at=timezone.now(),
            )
            if updated == 0:
                return None
            return await KonomiTVBS4KCloudTransfer.filter(id=job_id).using_db(connection).get()

    @staticmethod
    async def advance(
        job_id: UUID, worker_token: UUID, expected_phase: KonomiTVBS4KCloudTransferPhase,
        *, manifest: KonomiTVBS4KCloudTransferPlan | None = None, published_path: str | None = None,
    ) -> None:
        """完了した外部処理の次段階を保存し、所在切替も同じtransactionで確定する。

        Args:
            job_id: 永続要求UUID。
            worker_token: claim時の実行UUID。
            expected_phase: workerが実際に完了を確認した段階。
            manifest: Preparing完了時に確定した対象集合と目録。他の段階では渡さない。
            published_path: Publishing完了時に読取り確認したパス。他の段階では渡さない。
        Returns:
            None。古い実行・段階の飛越しは例外で拒否する。
        """
        phases: dict[KonomiTVBS4KCloudTransferPhase, KonomiTVBS4KCloudTransferPhase] = {
            'Preparing': 'Copying', 'Copying': 'Verifying', 'Verifying': 'Publishing',
            'Publishing': 'Cleaning', 'Cleaning': 'Completed',
            'Cancelling': 'Cancelled',
        }
        if expected_phase not in phases:
            raise KonomiTVBS4KCloudTransferConflict('Transfer cannot advance from this phase.')
        if (expected_phase == 'Preparing') != (manifest is not None):
            raise ValueError('A transfer manifest is required only when preparation completes.')
        if (expected_phase == 'Publishing') != (published_path is not None):
            raise ValueError('A verified source path is required only when publication completes.')
        async with in_transaction() as connection:
            job = await KonomiTVBS4KCloudTransfer.filter(
                id=job_id, worker_token=worker_token, status='Running', phase=expected_phase,
            ).using_db(connection).get_or_none()
            if job is None:
                raise KonomiTVBS4KCloudTransferConflict('Transfer worker or phase changed.')
            # 取消受付と公開開始を同じtransactionで直列化する。清掃以外の前進を許さない。
            if job.cancel_requested != (expected_phase == 'Cancelling'):
                raise KonomiTVBS4KCloudTransferConflict('Cancellation request prevents transfer advancement.')
            if manifest is not None:
                # 清掃対象と公開用目録を型付きで確定し、別ジョブのUUIDを誤って公開・削除しない。
                if manifest.manifest.recording_uuid != job.recording_uuid:
                    raise KonomiTVBS4KCloudTransferConflict('Transfer manifest belongs to another recording.')
                job.manifest = manifest.model_dump(mode='json')
            if expected_phase == 'Publishing':
                plan = KonomiTVBS4KCloudTransferPlan.model_validate(job.manifest)
                # 公開と読取り確認の後にだけ呼ぶ。移動元削除はこのcommitより後のCleaningで行う。
                old_location = 'Local' if job.direction == 'ToCloud' else 'Cloud'
                new_location = 'Cloud' if job.direction == 'ToCloud' else 'Local'
                changed = await KonomiTVBS4KCloudRecording.filter(
                    id=job.recording_uuid, recorded_video_id=job.recorded_video_id, location=old_location,
                ).using_db(connection).update(
                    location=new_location, owner_id=job.owner_id, connection_id=job.connection_id,
                    folder=job.folder, manifest=plan.manifest.model_dump(mode='json'), updated_at=timezone.now(),
                )
                if changed != 1:
                    raise KonomiTVBS4KCloudTransferConflict('Recording location changed during transfer.')
                if job.direction == 'ToLocal' and published_path != job.local_path:
                    raise KonomiTVBS4KCloudTransferConflict('Published restore path differs from the request.')
                await RecordedVideo.filter(id=job.recorded_video_id).using_db(connection).update(file_path=published_path)
            job.phase = phases[expected_phase]
            if job.phase == 'Completed':
                if job.direction == 'ToLocal':
                    # クラウド原本清掃後のUUIDは再利用しない。Local予約のRESTRICTで通常削除を妨げない。
                    await KonomiTVBS4KCloudDeletedRecording.get_or_create(id=job.recording_uuid, using_db=connection)
                    await KonomiTVBS4KCloudRecording.filter(
                        id=job.recording_uuid, recorded_video_id=job.recorded_video_id, location='Local',
                    ).using_db(connection).delete()
                job.status = 'Completed'
                job.worker_token = None
                job.active_video_id = None
            elif job.phase == 'Cancelled':
                # 呼出し元が記録済みの未公開コピーを清掃した後だけ解放する。原本の所在は切り替えない。
                job.status = 'Cancelled'
                job.worker_token = None
                job.active_video_id = None
                if job.direction == 'ToCloud':
                    await KonomiTVBS4KCloudRecording.filter(
                        id=job.recording_uuid, recorded_video_id=job.recorded_video_id, location='Local',
                    ).using_db(connection).delete()
            await job.save(using_db=connection)

    @staticmethod
    async def fail(job_id: UUID, worker_token: UUID, error_code: str) -> None:
        """処理段階と録画の占有を保持し、失敗を記録する。

        Args:
            job_id: 永続要求UUID。
            worker_token: claim時の実行UUID。
            error_code: 呼出し元が選ぶ固定識別子。例外本文やパスを渡さない。
        Returns:
            None
        """
        if not error_code or len(error_code) > 64 or not error_code.isascii() or not error_code.isalnum():
            raise ValueError('Invalid cloud transfer error code.')
        updated = await KonomiTVBS4KCloudTransfer.filter(id=job_id, status='Running', worker_token=worker_token).update(
            status='Failed', worker_token=None, error_code=error_code, updated_at=timezone.now(),
        )
        if updated != 1:
            raise KonomiTVBS4KCloudTransferConflict('Transfer worker changed before failure was recorded.')

    @staticmethod
    async def retry(job_id: UUID) -> None:
        """失敗した同じジョブだけを再開し、方向・段階・保存先を変更しない。

        Args:
            job_id: 管理者が再開を指定した要求UUID。
        Returns:
            None
        """
        updated = await KonomiTVBS4KCloudTransfer.filter(id=job_id, status='Failed').update(
            status='Pending', error_code=None, updated_at=timezone.now(),
        )
        if updated != 1:
            raise KonomiTVBS4KCloudTransferConflict('Only failed transfers can be retried.')
