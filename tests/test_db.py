"""DB制約、接続設定、トランザクション、UTC日時を確認。"""

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from threading import Event

from backend.db import connect, format_utc, init_db, parse_utc, transaction, utc_now


class DatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db_path = Path(self.temporary.name).resolve() / "nested/app.db"
        self.conn = connect(self.db_path)
        self.addCleanup(self.conn.close)
        init_db(self.conn)
        self.conn.execute("INSERT INTO works VALUES ('work', '作品', '/covers/work.svg')")
        self.conn.execute("INSERT INTO benefits (id, work_id, title) VALUES ('benefit', 'work', '特典')")
        self.conn.execute("INSERT INTO accounts VALUES ('alice', 'アリス')")

    def insert_entitlement(
        self,
        token: str = "token",
        uid: str = "UID",
        status: str = "unused",
        account_id: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO entitlements (token, benefit_id, uid, status, account_id, issued_at) "
            "VALUES (?, 'benefit', ?, ?, ?, '2026-10-17T01:23:45Z')",
            (token, uid, status, account_id),
        )

    def test_connection_settings_and_cross_thread_access(self) -> None:
        self.assertTrue(self.db_path.exists())
        self.assertIsNone(self.conn.isolation_level)
        self.assertIs(self.conn.row_factory, sqlite3.Row)
        with closing(connect(self.db_path)) as second:
            for conn in (self.conn, second):
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        with ThreadPoolExecutor(max_workers=1) as executor:
            row = executor.submit(lambda: self.conn.execute("SELECT * FROM works").fetchone()).result()
        self.assertEqual(row["id"], "work")

    def test_init_db_is_idempotent(self) -> None:
        init_db(self.conn)
        init_db(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM works").fetchone()[0], 1)

    def test_init_db_rejects_open_transaction_without_committing(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "取り消し"):
            with transaction(self.conn):
                self.insert_entitlement()
                with self.assertRaisesRegex(RuntimeError, "トランザクション中"):
                    init_db(self.conn)
                self.assertTrue(self.conn.in_transaction)
                with closing(connect(self.db_path)) as second:
                    self.assertEqual(second.execute("SELECT COUNT(*) FROM entitlements").fetchone()[0], 0)
                raise RuntimeError("取り消し")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entitlements").fetchone()[0], 0)

    def test_init_db_rejects_database_that_cannot_enable_wal(self) -> None:
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as memory:
            with self.assertRaisesRegex(RuntimeError, "WAL.*memory"):
                init_db(memory)

    def test_account_requires_account_id(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_entitlement(status="account")
        self.insert_entitlement(status="account", account_id="alice")

    def test_unclaimed_and_download_statuses_reject_account_id(self) -> None:
        for status in ("issuing", "unused", "download"):
            with self.subTest(status=status), self.assertRaises(sqlite3.IntegrityError):
                self.insert_entitlement(status=status, account_id="alice")

    def test_revoked_accepts_account_id_or_null(self) -> None:
        self.insert_entitlement(token="revoked-owned", status="revoked", account_id="alice")
        self.insert_entitlement(token="revoked-unowned", status="revoked")

    def test_unknown_status_is_rejected(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_entitlement(status="unknown")

    def test_only_one_active_entitlement_per_uid(self) -> None:
        self.insert_entitlement()
        for status in ("unused", "account", "download"):
            with self.subTest(status=status), self.assertRaises(sqlite3.IntegrityError):
                self.insert_entitlement(token=status, status=status,
                                        account_id="alice" if status == "account" else None)
        self.conn.execute("UPDATE entitlements SET status = 'revoked' WHERE token = 'token'")
        self.insert_entitlement(token="replacement")

    def test_issuing_and_revoked_can_duplicate_active_uid(self) -> None:
        self.insert_entitlement()
        for status in ("issuing", "revoked"):
            for number in range(2):
                self.insert_entitlement(token=f"{status}-{number}", status=status)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE entitlements SET status = 'unused' WHERE token = 'issuing-0'")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entitlements").fetchone()[0], 5)

    def test_token_is_unique(self) -> None:
        self.insert_entitlement()
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_entitlement(uid="OTHER")

    def test_benefit_file_position_is_unique_within_benefit(self) -> None:
        self.conn.execute(
            "INSERT INTO benefit_files VALUES ('file', 'benefit', 1, '曲', '01.wav', 'audio/wav')"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO benefit_files VALUES ('other', 'benefit', 1, '曲', '02.wav', 'audio/wav')"
            )

    def test_foreign_key_violations_fail(self) -> None:
        statements = (
            "INSERT INTO benefits (id, work_id, title) VALUES ('bad', 'missing', '特典')",
            "INSERT INTO benefit_files VALUES ('bad', 'missing', 1, '曲', '01.wav', 'audio/wav')",
            "INSERT INTO entitlements (token, benefit_id, uid, status, issued_at) "
            "VALUES ('bad', 'missing', 'UID', 'unused', '2026-10-17T01:23:45Z')",
            "INSERT INTO entitlements (token, benefit_id, uid, status, account_id, issued_at) "
            "VALUES ('bad', 'benefit', 'UID', 'account', 'missing', '2026-10-17T01:23:45Z')",
            "INSERT INTO download_sessions VALUES ('session', 'missing', '2026-10-17T01:33:45Z')",
            "DELETE FROM works WHERE id = 'work'",
        )
        for statement in statements:
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(statement)

    def test_transaction_commits_and_starts_immediate(self) -> None:
        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)
        with transaction(self.conn):
            self.insert_entitlement()
        self.assertEqual(statements[0], "BEGIN IMMEDIATE")
        self.assertEqual(statements[-1], "COMMIT")
        self.assertFalse(self.conn.in_transaction)
        with closing(connect(self.db_path)) as second:
            self.assertEqual(second.execute("SELECT COUNT(*) FROM entitlements").fetchone()[0], 1)

    def test_transaction_rejects_nested_transaction_and_preserves_outer_changes(self) -> None:
        with transaction(self.conn):
            self.insert_entitlement()
            with self.assertRaisesRegex(RuntimeError, "入れ子"):
                with transaction(self.conn):
                    self.fail("入れ子のトランザクションが開始されました")
            self.assertTrue(self.conn.in_transaction)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entitlements").fetchone()[0], 1)
            self.insert_entitlement(token="second", uid="SECOND")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entitlements").fetchone()[0], 2)

    def test_transaction_rejects_manually_opened_transaction(self) -> None:
        self.conn.execute("BEGIN")
        try:
            self.insert_entitlement()
            with self.assertRaisesRegex(RuntimeError, "入れ子"):
                with transaction(self.conn):
                    self.fail("既存のトランザクション内で開始されました")
            self.assertTrue(self.conn.in_transaction)
        finally:
            self.conn.execute("ROLLBACK")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entitlements").fetchone()[0], 0)

    def test_concurrent_writer_waits_for_commit(self) -> None:
        begin_attempted = Event()
        acquired = Event()
        with closing(connect(self.db_path)) as second, ThreadPoolExecutor(max_workers=1) as executor:
            def trace(statement: str) -> None:
                if statement == "BEGIN IMMEDIATE":
                    begin_attempted.set()

            def write_second() -> str:
                with transaction(second):
                    acquired.set()
                    token = second.execute("SELECT token FROM entitlements").fetchone()["token"]
                    second.execute(
                        "INSERT INTO entitlements (token, benefit_id, uid, status, issued_at) "
                        "VALUES ('second', 'benefit', 'SECOND', 'unused', '2026-10-17T01:23:45Z')"
                    )
                return token

            second.set_trace_callback(trace)
            with transaction(self.conn):
                self.insert_entitlement()
                future = executor.submit(write_second)
                self.assertTrue(begin_attempted.wait(timeout=5))
                # BEGINの試行を確認してから0.3秒保持し、待機中の完了を拒否する。
                self.assertFalse(acquired.wait(timeout=0.3))
                self.assertFalse(future.done())
            self.assertEqual(future.result(timeout=5), "token")
            self.assertTrue(acquired.is_set())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entitlements").fetchone()[0], 2)

    def test_transaction_rolls_back_all_changes_on_exception(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "失敗"):
            with transaction(self.conn):
                self.insert_entitlement()
                self.conn.execute(
                    "INSERT INTO download_sessions VALUES ('session', 'token', '2026-10-17T01:33:45Z')"
                )
                raise RuntimeError("失敗")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entitlements").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM download_sessions").fetchone()[0], 0)
        self.assertFalse(self.conn.in_transaction)
        with transaction(self.conn):
            self.insert_entitlement()

    def test_transaction_rolls_back_on_keyboard_interrupt(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            with transaction(self.conn):
                self.insert_entitlement()
                raise KeyboardInterrupt
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entitlements").fetchone()[0], 0)


class UtcDateTimeTest(unittest.TestCase):
    def test_utc_now_is_timezone_aware(self) -> None:
        self.assertIs(utc_now().tzinfo, UTC)

    def test_format_and_parse_round_trip(self) -> None:
        expected = "2026-10-17T01:23:45Z"
        dt = datetime(2026, 10, 17, 1, 23, 45, tzinfo=UTC)
        self.assertEqual(format_utc(dt), expected)
        self.assertEqual(parse_utc(expected), dt)
        self.assertIs(parse_utc(expected).tzinfo, UTC)

    def test_format_converts_timezone_and_drops_microseconds(self) -> None:
        dt = datetime(2026, 10, 17, 10, 23, 45, 987654, tzinfo=timezone(timedelta(hours=9)))
        self.assertEqual(format_utc(dt), "2026-10-17T01:23:45Z")

    def test_format_rejects_naive_datetime(self) -> None:
        with self.assertRaises(ValueError):
            format_utc(datetime(2026, 10, 17, 1, 23, 45))

    def test_parse_rejects_noncanonical_or_invalid_dates(self) -> None:
        for value in ("2026-10-17T01:23:45+00:00", "2026-10-17T01:23:45.123Z",
                      "2026-10-17T01:23:45", "2026-10-17 01:23:45Z",
                      "2026-1-7T1:2:3Z", "2026-02-30T01:23:45Z", "invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_utc(value)


if __name__ == "__main__":
    unittest.main()
