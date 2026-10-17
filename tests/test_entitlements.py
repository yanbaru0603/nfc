"""取得権の発行・確定・無効化と、再試行・取得権限の判定を一時DBで確認。"""

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backend.db import connect, init_db
from backend.entitlements import (
    ActiveEntitlementExists,
    AlreadyClaimed,
    BenefitNotFound,
    DownloadSession,
    EntitlementError,
    EntitlementNotFound,
    FileNotFound,
    RetryExpired,
    RetryForbidden,
    account_benefit_ids,
    account_can_access_benefit,
    activate_issued,
    allow_repeat_for_uid,
    authorize_retry,
    claim_for_account,
    claim_for_download,
    create_issuing,
    discard_issuing,
    find_active_by_uid,
    get_benefit_detail,
    get_benefit_for_account,
    get_file_for_account,
    get_file_for_retry,
    get_public_entitlement,
    new_token,
    reset_account_benefit,
    view_state,
)


NOW = datetime(2026, 10, 17, 1, 0, 0, tzinfo=UTC)
TTL = 600


class EntitlementTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db_path = Path(self.temporary.name).resolve() / "app.db"
        self.conn = connect(self.db_path)
        self.addCleanup(self.conn.close)
        init_db(self.conn)
        conn = self.conn
        conn.execute("INSERT INTO works VALUES ('work-1', '作品1', '/covers/1.svg')")
        conn.execute("INSERT INTO works VALUES ('work-2', '作品2', '/covers/2.svg')")
        conn.execute("INSERT INTO benefits VALUES ('benefit-1', 'work-1', '特典1', '説明1')")
        conn.execute("INSERT INTO benefits VALUES ('benefit-2', 'work-2', '特典2', '')")
        # position順で返すことを確かめるため、登録順をpositionと逆にする。
        for file_id, benefit_id, position in (("f1-2", "benefit-1", 2), ("f1-1", "benefit-1", 1),
                                              ("f2-1", "benefit-2", 1)):
            conn.execute(
                "INSERT INTO benefit_files VALUES (?, ?, ?, ?, ?, 'audio/wav')",
                (file_id, benefit_id, position, f"曲{file_id}", f"{benefit_id}/{position:02d}.wav"),
            )
        conn.executemany("INSERT INTO accounts VALUES (?, ?)",
                         (("alice", "アリス"), ("bob", "ボブ")))

    def insert(self, token: str, *, uid: str | None = None, status: str = "unused",
               benefit_id: str = "benefit-1", account_id: str | None = None,
               claimed_at: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO entitlements (token, benefit_id, uid, status, account_id, issued_at, claimed_at) "
            "VALUES (?, ?, ?, ?, ?, '2026-10-17T00:00:00Z', ?)",
            (token, benefit_id, uid or f"UID-{token}", status, account_id, claimed_at),
        )

    def status(self, token: str) -> str | None:
        row = self.conn.execute("SELECT status FROM entitlements WHERE token = ?", (token,)).fetchone()
        return None if row is None else row["status"]

    def session_count(self, token: str | None = None) -> int:
        if token is None:
            return self.conn.execute("SELECT COUNT(*) FROM download_sessions").fetchone()[0]
        return self.conn.execute(
            "SELECT COUNT(*) FROM download_sessions WHERE token = ?", (token,)).fetchone()[0]

    def view(self, token: str, *, account_id: str | None = None,
             session_id: str | None = None, now: datetime = NOW) -> str:
        return view_state(self.conn, token, account_id=account_id, retry_session_id=session_id, now=now)


class RepeatableCardTest(EntitlementTestCase):
    def enable(self, token: str = "t") -> None:
        uid = get_public_entitlement(self.conn, token).uid
        allow_repeat_for_uid(self.conn, uid)

    def test_exception_is_persistent_and_scoped_to_one_uid(self) -> None:
        self.insert("t")
        self.insert("normal")
        self.enable()
        self.enable()
        init_db(self.conn)
        for account in ("alice", "alice", "bob"):
            claim_for_account(self.conn, "t", account, now=NOW)
            self.assertEqual(self.view("t", account_id=account), "unused")
            self.assertTrue(account_can_access_benefit(self.conn, account, "benefit-1"))
            self.assertEqual(account_benefit_ids(self.conn, account), ["benefit-1"])
        self.assertEqual(self.status("t"), "unused")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM repeatable_account_claims").fetchone()[0], 2)
        claim_for_account(self.conn, "normal", "alice", now=NOW)
        with self.assertRaises(AlreadyClaimed):
            claim_for_account(self.conn, "normal", "bob", now=NOW)

    def test_repeat_downloads_keep_each_session_and_enforce_expiry(self) -> None:
        self.insert("t")
        self.enable()
        first = claim_for_download(self.conn, "t", now=NOW, ttl_seconds=TTL)
        second = claim_for_download(self.conn, "t", now=NOW + timedelta(seconds=TTL), ttl_seconds=TTL)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self.status("t"), "unused")
        self.assertEqual(authorize_retry(self.conn, "t", first.id, now=NOW), first)
        with self.assertRaises(RetryExpired):
            authorize_retry(self.conn, "t", first.id, now=NOW + timedelta(seconds=TTL))
        self.assertEqual(authorize_retry(self.conn, "t", second.id, now=NOW + timedelta(seconds=TTL)), second)
        with self.assertRaises(RetryForbidden):
            authorize_retry(self.conn, "t", None, now=NOW)
        with self.assertRaises(RetryForbidden):
            authorize_retry(self.conn, "t", "wrong-session", now=NOW)

    def test_enabling_used_card_preserves_owner_and_allows_other_receivers(self) -> None:
        self.insert("t")
        claim_for_account(self.conn, "t", "alice", now=NOW)
        self.enable()
        claim_for_account(self.conn, "t", "bob", now=NOW)
        session = claim_for_download(self.conn, "t", now=NOW, ttl_seconds=TTL)
        self.assertEqual(authorize_retry(self.conn, "t", session.id, now=NOW), session)
        self.assertTrue(account_can_access_benefit(self.conn, "alice", "benefit-1"))
        self.assertTrue(account_can_access_benefit(self.conn, "bob", "benefit-1"))
        self.assertEqual(self.status("t"), "account")

    def test_reissue_preserves_exception_and_previous_libraries(self) -> None:
        self.insert("t", uid="repeat-uid")
        self.enable()
        claim_for_account(self.conn, "t", "alice", now=NOW)
        session = claim_for_download(self.conn, "t", now=NOW, ttl_seconds=TTL)
        token = create_issuing(self.conn, uid="repeat-uid", benefit_id="benefit-1", now=NOW, reissue=True)
        activate_issued(self.conn, token, reissue=True)
        self.assertEqual(self.status("t"), "revoked")
        with self.assertRaises(EntitlementNotFound):
            claim_for_account(self.conn, "t", "bob", now=NOW)
        with self.assertRaises(EntitlementNotFound):
            authorize_retry(self.conn, "t", session.id, now=NOW)
        claim_for_account(self.conn, token, "bob", now=NOW)
        claim_for_account(self.conn, token, "alice", now=NOW)
        self.assertTrue(account_can_access_benefit(self.conn, "alice", "benefit-1"))
        self.assertTrue(account_can_access_benefit(self.conn, "bob", "benefit-1"))

    def test_reset_removes_only_one_accounts_claim_and_preserves_downloads(self) -> None:
        self.insert("t")
        claim_for_account(self.conn, "t", "alice", now=NOW)
        self.enable()
        for account in ("alice", "bob"):
            claim_for_account(self.conn, "t", account, now=NOW)
        session = claim_for_download(self.conn, "t", now=NOW, ttl_seconds=TTL)
        reset_account_benefit(self.conn, "alice", "benefit-1")
        self.assertFalse(account_can_access_benefit(self.conn, "alice", "benefit-1"))
        self.assertTrue(account_can_access_benefit(self.conn, "bob", "benefit-1"))
        self.assertEqual(authorize_retry(self.conn, "t", session.id, now=NOW), session)
        claim_for_account(self.conn, "t", "alice", now=NOW)
        self.assertTrue(account_can_access_benefit(self.conn, "alice", "benefit-1"))

    def test_download_session_failure_does_not_change_existing_grants(self) -> None:
        self.insert("t")
        self.enable()
        claim_for_account(self.conn, "t", "alice", now=NOW)
        self.conn.execute("CREATE TEMP TRIGGER fail_repeat_session BEFORE INSERT ON download_sessions "
                          "BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            claim_for_download(self.conn, "t", now=NOW, ttl_seconds=TTL)
        self.assertEqual(self.status("t"), "unused")
        self.assertEqual(self.session_count(), 0)
        self.assertTrue(account_can_access_benefit(self.conn, "alice", "benefit-1"))


class ClaimTest(EntitlementTestCase):
    def test_exceptions_share_base_class(self) -> None:
        for error in (EntitlementNotFound, AlreadyClaimed, RetryForbidden, RetryExpired,
                      ActiveEntitlementExists, FileNotFound, BenefitNotFound):
            self.assertTrue(issubclass(error, EntitlementError))

    def test_viewing_does_not_consume_entitlement(self) -> None:
        self.insert("t")
        for _ in range(3):
            self.assertEqual(self.view("t"), "unused")
            self.assertEqual(self.view("t", account_id="alice", session_id="x"), "unused")
            self.assertEqual(get_public_entitlement(self.conn, "t").status, "unused")
        row = self.conn.execute("SELECT * FROM entitlements WHERE token = 't'").fetchone()
        self.assertEqual((row["status"], row["account_id"], row["claimed_at"]), ("unused", None, None))
        self.assertEqual(self.session_count(), 0)

    def test_claim_for_account_once(self) -> None:
        self.insert("t")
        entitlement = claim_for_account(self.conn, "t", "alice", now=NOW)
        self.assertEqual(entitlement.status, "account")
        self.assertEqual(entitlement.account_id, "alice")
        self.assertEqual(entitlement.claimed_at, NOW)
        self.assertFalse(self.conn.in_transaction)
        with self.assertRaises(AlreadyClaimed):
            claim_for_account(self.conn, "t", "alice", now=NOW + timedelta(seconds=1))
        with self.assertRaises(AlreadyClaimed):
            claim_for_account(self.conn, "t", "bob", now=NOW)
        with self.assertRaises(AlreadyClaimed):
            claim_for_download(self.conn, "t", now=NOW, ttl_seconds=TTL)
        row = self.conn.execute("SELECT * FROM entitlements WHERE token = 't'").fetchone()
        self.assertEqual((row["status"], row["account_id"], row["claimed_at"]),
                         ("account", "alice", "2026-10-17T01:00:00Z"))
        self.assertEqual(self.session_count(), 0)
        self.assertFalse(self.conn.in_transaction)

    def test_account_view_states(self) -> None:
        self.insert("t")
        claim_for_account(self.conn, "t", "alice", now=NOW)
        self.assertEqual(self.view("t", account_id="alice"), "account_owned")
        self.assertEqual(self.view("t", account_id="bob"), "claimed")
        self.assertEqual(self.view("t"), "claimed")

    def test_claim_for_download_registers_retry_session(self) -> None:
        self.insert("t")
        session = claim_for_download(self.conn, "t", now=NOW, ttl_seconds=TTL)
        self.assertIsInstance(session, DownloadSession)
        self.assertEqual(session.expires_at, NOW + timedelta(seconds=TTL))
        self.assertGreaterEqual(len(session.id), 40)
        row = self.conn.execute("SELECT * FROM download_sessions").fetchone()
        self.assertEqual((row["id"], row["token"], row["expires_at"]),
                         (session.id, "t", "2026-10-17T01:10:00Z"))
        entitlement = get_public_entitlement(self.conn, "t")
        self.assertEqual((entitlement.status, entitlement.account_id, entitlement.claimed_at),
                         ("download", None, NOW))
        with self.assertRaises(AlreadyClaimed):
            claim_for_download(self.conn, "t", now=NOW, ttl_seconds=TTL)
        with self.assertRaises(AlreadyClaimed):
            claim_for_account(self.conn, "t", "alice", now=NOW)
        self.assertEqual(self.session_count(), 1)
        self.assertEqual(self.status("t"), "download")

    def test_retry_within_and_after_ttl(self) -> None:
        self.insert("t")
        session = claim_for_download(self.conn, "t", now=NOW, ttl_seconds=TTL)
        before_expiry = NOW + timedelta(seconds=TTL - 1)
        self.assertEqual(authorize_retry(self.conn, "t", session.id, now=before_expiry), session)
        self.assertEqual(self.view("t", session_id=session.id, now=before_expiry), "download_retryable")
        # 期限の時刻ちょうどから期限切れとして扱う。
        for after in (NOW + timedelta(seconds=TTL), NOW + timedelta(days=1)):
            with self.subTest(after=after):
                with self.assertRaises(RetryExpired):
                    authorize_retry(self.conn, "t", session.id, now=after)
                self.assertEqual(self.view("t", session_id=session.id, now=after), "claimed")

    def test_retry_with_missing_or_mismatched_session_is_forbidden(self) -> None:
        self.insert("t")
        self.insert("other")
        session = claim_for_download(self.conn, "t", now=NOW, ttl_seconds=TTL)
        other = claim_for_download(self.conn, "other", now=NOW, ttl_seconds=TTL)
        for session_id in (None, "", "unknown-session", other.id):
            with self.subTest(session_id=session_id):
                with self.assertRaises(RetryForbidden):
                    authorize_retry(self.conn, "t", session_id, now=NOW)
                self.assertEqual(self.view("t", session_id=session_id), "claimed")
        # 期限切れでも、別のトークンのセッションなら403が先。
        with self.assertRaises(RetryForbidden):
            authorize_retry(self.conn, "t", other.id, now=NOW + timedelta(days=1))
        self.assertEqual(authorize_retry(self.conn, "t", session.id, now=NOW), session)

    def test_retry_session_on_non_download_entitlement_is_forbidden(self) -> None:
        for status, account_id in (("unused", None), ("account", "alice")):
            with self.subTest(status=status):
                token = f"t-{status}"
                self.insert(token, status=status, account_id=account_id)
                self.conn.execute("INSERT INTO download_sessions VALUES (?, ?, '2026-10-17T02:00:00Z')",
                                  (f"s-{status}", token))
                with self.assertRaises(RetryForbidden):
                    authorize_retry(self.conn, token, f"s-{status}", now=NOW)

    def test_invalid_tokens_are_not_found_before_session_checks(self) -> None:
        self.insert("issuing", status="issuing")
        self.insert("revoked", status="revoked")
        for token in ("issuing", "revoked"):
            # 期限切れのセッションを持っていても、無効なトークンの判定が先。
            self.conn.execute("INSERT INTO download_sessions VALUES (?, ?, '2026-10-16T00:00:00Z')",
                              (f"s-{token}", token))
        for token in ("missing", "issuing", "revoked"):
            with self.subTest(token=token):
                with self.assertRaises(EntitlementNotFound):
                    get_public_entitlement(self.conn, token)
                with self.assertRaises(EntitlementNotFound):
                    self.view(token, account_id="alice", session_id=f"s-{token}")
                with self.assertRaises(EntitlementNotFound):
                    authorize_retry(self.conn, token, f"s-{token}", now=NOW)
                with self.assertRaises(EntitlementNotFound):
                    authorize_retry(self.conn, token, None, now=NOW)
                with self.assertRaises(EntitlementNotFound):
                    claim_for_account(self.conn, token, "alice", now=NOW)
                with self.assertRaises(EntitlementNotFound):
                    claim_for_download(self.conn, token, now=NOW, ttl_seconds=TTL)
                self.assertFalse(self.conn.in_transaction)
        self.assertEqual((self.status("issuing"), self.status("revoked")), ("issuing", "revoked"))
        self.assertEqual(self.session_count(), 2)

    def test_download_session_insert_failure_leaves_entitlement_unused(self) -> None:
        self.insert("t")
        self.conn.execute(
            "CREATE TEMP TRIGGER fail_session BEFORE INSERT ON download_sessions "
            "BEGIN SELECT RAISE(ABORT, 'セッション登録の失敗'); END"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "セッション登録の失敗"):
            claim_for_download(self.conn, "t", now=NOW, ttl_seconds=TTL)
        self.assertFalse(self.conn.in_transaction)
        row = self.conn.execute("SELECT * FROM entitlements WHERE token = 't'").fetchone()
        self.assertEqual((row["status"], row["account_id"], row["claimed_at"]), ("unused", None, None))
        self.assertEqual(self.session_count(), 0)
        self.conn.execute("DROP TRIGGER fail_session")
        claim_for_account(self.conn, "t", "alice", now=NOW)


class ConcurrentClaimTest(EntitlementTestCase):
    THREADS = 16
    ROUNDS = 5

    def run_round(self, token: str) -> list[tuple[str, object]]:
        barrier = threading.Barrier(self.THREADS)

        def worker(index: int) -> tuple[str, object]:
            with closing(connect(self.db_path)) as conn:
                barrier.wait(timeout=10)
                try:
                    if index % 2 == 0:
                        account = ("alice", "bob")[index // 2 % 2]
                        return "account", claim_for_account(conn, token, account, now=NOW)
                    return "download", claim_for_download(conn, token, now=NOW, ttl_seconds=TTL)
                except AlreadyClaimed as error:
                    return "already", error

        with ThreadPoolExecutor(max_workers=self.THREADS) as executor:
            return list(executor.map(worker, range(self.THREADS)))

    def test_concurrent_account_and_download_claims_succeed_once(self) -> None:
        kinds_seen: set[str] = set()
        for round_number in range(self.ROUNDS):
            token = f"race-{round_number}"
            self.insert(token)
            with self.subTest(token=token):
                results = self.run_round(token)
                successes = [(kind, value) for kind, value in results if kind != "already"]
                self.assertEqual(len(successes), 1)
                self.assertEqual(sum(kind == "already" for kind, _ in results), self.THREADS - 1)
                kind, value = successes[0]
                kinds_seen.add(kind)
                row = self.conn.execute("SELECT * FROM entitlements WHERE token = ?", (token,)).fetchone()
                self.assertEqual(row["status"], kind)
                if kind == "account":
                    self.assertEqual(row["account_id"], value.account_id)
                    self.assertEqual(self.session_count(token), 0)
                else:
                    self.assertIsNone(row["account_id"])
                    self.assertEqual(self.session_count(token), 1)
                    session_id = self.conn.execute(
                        "SELECT id FROM download_sessions WHERE token = ?", (token,)).fetchone()["id"]
                    self.assertEqual(session_id, value.id)
        self.assertTrue(kinds_seen)


class IssueTest(EntitlementTestCase):
    def test_new_token_is_random_urlsafe(self) -> None:
        tokens = {new_token() for _ in range(100)}
        self.assertEqual(len(tokens), 100)
        for token in tokens:
            self.assertEqual(len(token), 22)
            self.assertRegex(token, r"^[A-Za-z0-9_-]+$")

    def test_issue_and_activate(self) -> None:
        token = create_issuing(self.conn, benefit_id="benefit-1", uid="04A1", now=NOW)
        self.assertEqual(self.status(token), "issuing")
        self.assertIsNone(find_active_by_uid(self.conn, "04A1"))
        with self.assertRaises(EntitlementNotFound):
            get_public_entitlement(self.conn, token)
        activate_issued(self.conn, token)
        active = find_active_by_uid(self.conn, "04A1")
        assert active is not None
        self.assertEqual((active.token, active.status, active.issued_at), (token, "unused", NOW))
        self.assertEqual(self.view(token), "unused")
        # 発行中でなくなった取得権は、もう一度有効にも削除もできない。
        with self.assertRaises(EntitlementNotFound):
            activate_issued(self.conn, token)
        discard_issuing(self.conn, token)
        self.assertEqual(self.status(token), "unused")
        with self.assertRaises(EntitlementNotFound):
            activate_issued(self.conn, "missing")

    def test_create_issuing_accepts_token_and_rejects_unknown_benefit(self) -> None:
        self.assertEqual(create_issuing(self.conn, benefit_id="benefit-1", uid="U", now=NOW,
                                        token="fixed"), "fixed")
        with self.assertRaises(BenefitNotFound):
            create_issuing(self.conn, benefit_id="missing", uid="V", now=NOW)
        self.assertFalse(self.conn.in_transaction)

    def test_duplicate_uid_requires_reissue(self) -> None:
        self.insert("old", uid="04A1")
        with self.assertRaises(ActiveEntitlementExists) as raised:
            create_issuing(self.conn, benefit_id="benefit-1", uid="04A1", now=NOW)
        self.assertEqual(raised.exception.existing.token, "old")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entitlements").fetchone()[0], 1)

    def test_issuing_can_duplicate_and_activate_checks_again(self) -> None:
        first = create_issuing(self.conn, benefit_id="benefit-1", uid="04A1", now=NOW)
        second = create_issuing(self.conn, benefit_id="benefit-1", uid="04A1", now=NOW)
        activate_issued(self.conn, first)
        # 発行中の間に同じUIDが有効になった場合は、reissueなしでは有効にしない。
        with self.assertRaises(ActiveEntitlementExists):
            activate_issued(self.conn, second)
        self.assertEqual((self.status(first), self.status(second)), ("unused", "issuing"))
        self.assertFalse(self.conn.in_transaction)

    def test_discard_issuing_removes_only_issuing(self) -> None:
        self.insert("old", uid="04A1")
        token = create_issuing(self.conn, benefit_id="benefit-1", uid="04A1", now=NOW, reissue=True)
        discard_issuing(self.conn, token)
        discard_issuing(self.conn, "old")
        discard_issuing(self.conn, "missing")
        self.assertIsNone(self.status(token))
        self.assertEqual(self.status("old"), "unused")

    def test_failed_reissue_keeps_existing_entitlement(self) -> None:
        self.insert("old", uid="04A1")
        claim_for_account(self.conn, "old", "alice", now=NOW)
        token = create_issuing(self.conn, benefit_id="benefit-1", uid="04A1", now=NOW, reissue=True)
        # 発行中の間も既存の取得権は有効なまま。
        self.assertEqual(self.status("old"), "account")
        self.assertEqual(self.view("old", account_id="alice"), "account_owned")
        discard_issuing(self.conn, token)
        self.assertIsNone(self.status(token))
        active = find_active_by_uid(self.conn, "04A1")
        assert active is not None
        self.assertEqual((active.token, active.status), ("old", "account"))

    def test_reissue_revokes_old_and_activates_new_in_one_transaction(self) -> None:
        self.insert("old", uid="04A1")
        token = create_issuing(self.conn, benefit_id="benefit-1", uid="04A1", now=NOW, reissue=True)
        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)
        activate_issued(self.conn, token, reissue=True)
        self.conn.set_trace_callback(None)
        self.assertEqual(statements[0], "BEGIN IMMEDIATE")
        self.assertEqual(statements[-1], "COMMIT")
        self.assertEqual(sum(s.startswith(("BEGIN", "COMMIT")) for s in statements), 2)
        self.assertEqual((self.status("old"), self.status(token)), ("revoked", "unused"))
        with self.assertRaises(EntitlementNotFound):
            claim_for_account(self.conn, "old", "alice", now=NOW)

    def test_failed_activation_rolls_back_revocation(self) -> None:
        self.insert("old", uid="04A1")
        token = create_issuing(self.conn, benefit_id="benefit-1", uid="04A1", now=NOW, reissue=True)
        self.conn.execute(
            "CREATE TEMP TRIGGER fail_activate BEFORE UPDATE OF status ON entitlements "
            "WHEN NEW.status = 'unused' BEGIN SELECT RAISE(ABORT, '有効化の失敗'); END"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "有効化の失敗"):
            activate_issued(self.conn, token, reissue=True)
        self.assertEqual((self.status("old"), self.status(token)), ("unused", "issuing"))

    def test_reissue_keeps_account_benefit_in_library(self) -> None:
        self.insert("old", uid="04A1")
        claim_for_account(self.conn, "old", "alice", now=NOW)
        token = create_issuing(self.conn, benefit_id="benefit-1", uid="04A1", now=NOW, reissue=True)
        activate_issued(self.conn, token, reissue=True)
        row = self.conn.execute("SELECT * FROM entitlements WHERE token = 'old'").fetchone()
        self.assertEqual((row["status"], row["account_id"]), ("revoked", "alice"))
        self.assertEqual(account_benefit_ids(self.conn, "alice"), ["benefit-1"])
        self.assertTrue(account_can_access_benefit(self.conn, "alice", "benefit-1"))
        self.assertEqual(get_benefit_for_account(self.conn, "alice", "benefit-1").benefit.id, "benefit-1")
        self.assertEqual(get_file_for_account(self.conn, "alice", "f1-1").id, "f1-1")
        with self.assertRaises(EntitlementNotFound):
            self.view("old", account_id="alice")
        # 新しい取得権は別のアカウントでも受け取れる。
        claim_for_account(self.conn, token, "bob", now=NOW)
        self.assertEqual(account_benefit_ids(self.conn, "bob"), ["benefit-1"])

    def test_reissue_disables_download_retry(self) -> None:
        self.insert("old", uid="04A1")
        session = claim_for_download(self.conn, "old", now=NOW, ttl_seconds=TTL)
        token = create_issuing(self.conn, benefit_id="benefit-1", uid="04A1", now=NOW, reissue=True)
        activate_issued(self.conn, token, reissue=True)
        row = self.conn.execute("SELECT * FROM entitlements WHERE token = 'old'").fetchone()
        self.assertEqual((row["status"], row["account_id"]), ("revoked", None))
        with self.assertRaises(EntitlementNotFound):
            authorize_retry(self.conn, "old", session.id, now=NOW)
        with self.assertRaises(EntitlementNotFound):
            self.view("old", session_id=session.id)


class AccessTest(EntitlementTestCase):
    def test_account_benefit_ids_are_unique_and_newest_first(self) -> None:
        self.insert("a1", benefit_id="benefit-1", status="account", account_id="alice",
                    claimed_at="2026-10-17T00:10:00Z")
        self.insert("a2", benefit_id="benefit-1", status="revoked", account_id="alice",
                    claimed_at="2026-10-17T00:30:00Z")
        self.insert("a3", benefit_id="benefit-2", status="account", account_id="alice",
                    claimed_at="2026-10-17T00:20:00Z")
        self.insert("b1", benefit_id="benefit-2", status="account", account_id="bob",
                    claimed_at="2026-10-17T00:00:00Z")
        self.insert("d1", benefit_id="benefit-1", status="download", claimed_at="2026-10-17T00:40:00Z")
        self.assertEqual(account_benefit_ids(self.conn, "alice"), ["benefit-2", "benefit-1"])
        self.assertEqual(account_benefit_ids(self.conn, "bob"), ["benefit-2"])
        self.assertEqual(account_benefit_ids(self.conn, "nobody"), [])
        self.assertFalse(account_can_access_benefit(self.conn, "bob", "benefit-1"))

    def test_benefit_detail(self) -> None:
        detail = get_benefit_detail(self.conn, "benefit-1")
        self.assertEqual((detail.work.id, detail.work.title, detail.work.cover_path),
                         ("work-1", "作品1", "/covers/1.svg"))
        self.assertEqual((detail.benefit.id, detail.benefit.title, detail.benefit.description),
                         ("benefit-1", "特典1", "説明1"))
        self.assertEqual([(f.id, f.position) for f in detail.files], [("f1-1", 1), ("f1-2", 2)])
        self.assertEqual(detail.files[0].file_path, "benefit-1/01.wav")
        self.assertEqual(detail.files[0].mime_type, "audio/wav")
        with self.assertRaises(BenefitNotFound):
            get_benefit_detail(self.conn, "missing")

    def test_benefit_and_files_for_account_require_ownership(self) -> None:
        self.insert("t", benefit_id="benefit-1")
        claim_for_account(self.conn, "t", "alice", now=NOW)
        self.assertEqual(len(get_benefit_for_account(self.conn, "alice", "benefit-1").files), 2)
        for account_id, benefit_id in (("bob", "benefit-1"), ("alice", "benefit-2"), ("alice", "missing")):
            with self.subTest(account_id=account_id, benefit_id=benefit_id):
                with self.assertRaises(BenefitNotFound):
                    get_benefit_for_account(self.conn, account_id, benefit_id)
        self.assertEqual(get_file_for_account(self.conn, "alice", "f1-2").position, 2)
        for account_id, file_id in (("bob", "f1-1"), ("alice", "f2-1"), ("alice", "missing")):
            with self.subTest(account_id=account_id, file_id=file_id):
                with self.assertRaises(FileNotFound):
                    get_file_for_account(self.conn, account_id, file_id)

    def test_download_does_not_grant_account_access(self) -> None:
        self.insert("t")
        claim_for_download(self.conn, "t", now=NOW, ttl_seconds=TTL)
        self.assertEqual(account_benefit_ids(self.conn, "alice"), [])
        with self.assertRaises(FileNotFound):
            get_file_for_account(self.conn, "alice", "f1-1")

    def test_file_for_retry_must_belong_to_token_benefit(self) -> None:
        self.insert("t", benefit_id="benefit-1")
        self.assertEqual(get_file_for_retry(self.conn, "t", "f1-1").benefit_id, "benefit-1")
        for token, file_id in (("t", "f2-1"), ("t", "missing"), ("missing", "f1-1")):
            with self.subTest(token=token, file_id=file_id):
                with self.assertRaises(FileNotFound):
                    get_file_for_retry(self.conn, token, file_id)


if __name__ == "__main__":
    unittest.main()
