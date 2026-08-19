import re

from tortoise.backends.base.client import TransactionContext
from tortoise.backends.sqlite.client import (
    SqliteClient,
    SqliteTransactionContext,
    SqliteTransactionWrapper,
    translate_exceptions,
)
from tortoise.exceptions import TransactionManagementError


class KonomiTVBS4KTransactionalSQLiteTransactionWrapper(SqliteTransactionWrapper):
    """SQLite の複数 SQL 実行を Tortoise の transaction 内へ確実に保持する。"""

    def __init__(self, connection: SqliteClient) -> None:
        """transaction 用の SQLite connection wrapper を初期化する。

        Args:
            connection: 親となる SQLite クライアント。

        Returns:
            None
        """

        super().__init__(connection)

        # migration script が transaction 外で foreign_keys を無効化する必要がある場合に、
        # commit / rollback 後に元の接続設定へ戻すための状態を保持する
        self.foreign_keys_was_enabled: bool | None = None

        # 同じ transaction で2回目の executescript() が1回目を暗黙 commit しないよう、
        # execute_script() から参照する呼び出し済み状態を保持する
        self.execute_script_called = False

        # 通常の application transaction で先行 DML を暗黙 commit しないよう、
        # begin() と execute_script() が比較する SQLite connection の変更数を保持する
        self.total_changes_at_begin: int | None = None

    async def begin(self) -> None:
        """transaction を開始し、先行 DML 検出用の変更数を記録する。

        Returns:
            None
        """

        await super().begin()
        self.total_changes_at_begin = self._connection.total_changes

    @translate_exceptions
    async def execute_script(self, query: str) -> None:
        """複数 SQL を現在の Tortoise transaction の一部として実行する。

        Args:
            query: 実行する SQL script。

        Returns:
            None
        """

        async with self.acquire_connection() as connection:
            self.log.debug(query)

            # 2回目の executescript() は1回目が開始した transaction を暗黙 commit するため、
            # transaction の atomicity を維持できない呼び出しは実行前に拒否する
            if self.execute_script_called:
                raise TransactionManagementError(
                    'execute_script() can only be called once in a SQLite transaction.'
                )

            # executescript() より前の DML は暗黙 commit されるため、通常の application transaction でも
            # 部分 commit を起こさず fail-closed になるよう変更数が増えていた場合は拒否する
            if (
                self.total_changes_at_begin is None
                or connection.total_changes != self.total_changes_at_begin
            ):
                raise TransactionManagementError(
                    'execute_script() must run before other changes in a SQLite transaction.'
                )
            self.execute_script_called = True

            # sqlite3.executescript() は実行前に未完了の transaction を暗黙 commit するため、
            # script 自身の先頭で transaction を再開しなければ Aerich の version 更新と分断される
            script_prefix = 'BEGIN IMMEDIATE;\n'

            # SQLite の PRAGMA foreign_keys は transaction 中に変更できない
            ## テーブル再構築 migration が明示的に無効化を要求した場合だけ BEGIN より前へ移し、
            ## script 内の SQL は分割せずそのまま executescript() に渡す
            foreign_keys_pragmas = re.findall(
                r'(?im)^\s*PRAGMA\s+foreign_keys\b[^;]*;',
                query,
            )
            foreign_keys_values = [
                match.group(1).lower()
                for statement in foreign_keys_pragmas
                if (match := re.fullmatch(
                    r'(?is)\s*PRAGMA\s+foreign_keys\s*=\s*(off|on|0|1)\s*;',
                    statement,
                )) is not None
            ]
            normalized_foreign_keys_values = [
                'off' if value in ('off', '0') else 'on'
                for value in foreign_keys_values
            ]
            if foreign_keys_pragmas and normalized_foreign_keys_values not in (
                ['off'],
                ['off', 'on'],
            ):
                raise TransactionManagementError(
                    'A transactional SQLite script only supports PRAGMA foreign_keys=off '
                    'optionally followed by PRAGMA foreign_keys=on.'
                )
            if normalized_foreign_keys_values:
                foreign_keys_rows = await connection.execute_fetchall('PRAGMA foreign_keys;')
                self.foreign_keys_was_enabled = bool(foreign_keys_rows[0][0])
                script_prefix = 'PRAGMA foreign_keys=off;\n' + script_prefix

            await connection.executescript(script_prefix + query)

    async def commit(self) -> None:
        """transaction を commit し、foreign key 設定を復元する。

        Returns:
            None
        """

        try:
            await super().commit()
        finally:
            await self.restoreForeignKeys()

    async def rollback(self) -> None:
        """transaction を rollback し、foreign key 設定を復元する。

        Returns:
            None
        """

        try:
            await super().rollback()
        finally:
            await self.restoreForeignKeys()

    async def restoreForeignKeys(self) -> None:
        """script 実行前に有効だった foreign key 設定を復元する。

        Returns:
            None
        """

        # 元から無効だった接続では有効化せず、接続設定を変更しない
        if self.foreign_keys_was_enabled is True:
            # commit / rollback 自体が失敗して transaction が残っている場合、PRAGMA は変更できない
            ## 先に rollback を再試行し、部分適用を確定させずに接続設定を復元する
            if self._connection.in_transaction:
                await self._connection.rollback()
                self._finalized = True
            await self._connection.execute('PRAGMA foreign_keys=on;')
            foreign_keys_rows = list(
                await self._connection.execute_fetchall('PRAGMA foreign_keys;')
            )
            if not bool(foreign_keys_rows[0][0]):
                raise TransactionManagementError('Failed to restore PRAGMA foreign_keys=on.')
        self.foreign_keys_was_enabled = None


class KonomiTVBS4KTransactionalSQLiteClient(SqliteClient):
    """transaction-safe な複数 SQL 実行を提供する SQLite クライアント。"""

    def _in_transaction(
        self,
    ) -> TransactionContext[KonomiTVBS4KTransactionalSQLiteTransactionWrapper]:
        """transaction-safe wrapper を使う transaction context を生成する。

        Returns:
            transaction context。
        """

        return SqliteTransactionContext(
            KonomiTVBS4KTransactionalSQLiteTransactionWrapper(self),
            self._lock,
        )


client_class = KonomiTVBS4KTransactionalSQLiteClient
