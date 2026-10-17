"""特典情報API（GET /api/r/<token>）の表示状態と、GETで状態を変えないことを確認。"""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.db import connect, format_utc
from backend.main import create_app
from backend.routes import API_PREFIX
from backend.routes.receive import RETRY_COOKIE_NAME, get_now, retry_cookie_path
from backend.security import login_session
from seed import DEV_TOKENS, seed


NOW = datetime(2026, 10, 17, 1, 0, 0, tzinfo=UTC)
TOKEN = DEV_TOKENS[0]
OTHER_TOKEN = DEV_TOKENS[1]

EXPECTED_WORK = {"id": "work-001", "title": "星めぐりの物語", "cover_path": "/covers/work-001.svg"}
EXPECTED_BENEFIT = {"id": "benefit-001", "title": "特装版CD 特典音源", "description": "デモ用に生成した3曲の単音音源"}
EXPECTED_FILES = [
    {"id": "file-001", "position": 1, "title": "朝の光", "mime_type": "audio/wav"},
    {"id": "file-002", "position": 2, "title": "風の便り", "mime_type": "audio/wav"},
    {"id": "file-003", "position": 3, "title": "星の帰り道", "mime_type": "audio/wav"},
]


def make_settings(root: Path) -> Settings:
    return Settings(
        db_path=root / "app.db",
        media_dir=root / "media",
        base_url="http://testserver",
        session_secret="test-secret",
        retry_ttl_seconds=600,
        cookie_secure=False,
    )


def add_login_route(app: FastAPI) -> None:
    """デモログインのAPIは後の開発単位なので、テスト用のログインのルートを足す。"""
    router = APIRouter(prefix=API_PREFIX + "/_test")

    @router.post("/login/{account_id}")
    def login(request: Request, account_id: str) -> dict[str, str]:
        return {"csrf_token": login_session(request, account_id)}

    app.include_router(router)


class ReceiveApiTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings = make_settings(self.root)
        self.app = create_app(self.settings)
        self.app.dependency_overrides[get_now] = lambda: NOW
        add_login_route(self.app)
        self.client = self.enter_client()
        with self.db() as conn:
            seed(conn, self.settings, now=NOW - timedelta(days=1))

    def enter_client(self) -> TestClient:
        client = TestClient(self.app)
        client.__enter__()  # lifespan を実行してスキーマを作る
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def db(self) -> closing[sqlite3.Connection]:
        return closing(connect(self.settings.db_path))

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        with self.db() as conn:
            conn.execute(sql, params)

    def entitlement_row(self, token: str) -> dict[str, object]:
        with self.db() as conn:
            row = conn.execute("SELECT * FROM entitlements WHERE token = ?", (token,)).fetchone()
        return dict(row)

    def snapshot(self) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
        """取得権と再試行セッションの全行。GETの前後で比べる。"""
        with self.db() as conn:
            entitlements = [tuple(row) for row in conn.execute("SELECT * FROM entitlements ORDER BY token")]
            sessions = [tuple(row) for row in conn.execute("SELECT * FROM download_sessions ORDER BY id")]
        return entitlements, sessions

    def login(self, client: TestClient, account_id: str) -> None:
        csrf_token = client.get("/api/session").json()["csrf_token"]
        response = client.post(f"/api/_test/login/{account_id}", headers={"X-CSRF-Token": csrf_token})
        self.assertEqual(response.status_code, 200)

    def claim_for_account(self, token: str, account_id: str) -> None:
        self.execute(
            "UPDATE entitlements SET status = 'account', account_id = ?, claimed_at = ? WHERE token = ?",
            (account_id, format_utc(NOW - timedelta(hours=1)), token),
        )

    def claim_for_download(self, token: str) -> None:
        self.execute(
            "UPDATE entitlements SET status = 'download', claimed_at = ? WHERE token = ?",
            (format_utc(NOW - timedelta(minutes=5)), token),
        )

    def add_download_session(self, session_id: str, token: str, expires_at: datetime) -> None:
        self.execute(
            "INSERT INTO download_sessions (id, token, expires_at) VALUES (?, ?, ?)",
            (session_id, token, format_utc(expires_at)),
        )

    def set_retry_cookie(self, client: TestClient, token: str, session_id: str) -> None:
        # 本番と同じく、トークンごとのPathを付けて保存する。
        client.cookies.set(RETRY_COOKIE_NAME, session_id, domain="testserver.local",
                           path=retry_cookie_path(token))

    def get_state(self, client: TestClient, token: str = TOKEN) -> str:
        response = client.get(f"/api/r/{token}")
        self.assertEqual(response.status_code, 200)
        return response.json()["state"]

    def assert_not_found(self, token: str) -> None:
        response = self.client.get(f"/api/r/{token}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {
            "error": {"code": "entitlement_not_found", "message": "この特典は見つかりません"},
        })
        self.assertNotIn(token, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    # ---- 未使用 ----

    def test_unused_returns_display_info_without_extra_keys(self) -> None:
        response = self.client.get(f"/api/r/{TOKEN}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "state": "unused",
            "work": EXPECTED_WORK,
            "benefit": EXPECTED_BENEFIT,
            "files": EXPECTED_FILES,
        })
        self.assertNotIn("file_path", response.text)
        self.assertNotIn(".wav", response.text)
        self.assertNotIn(TOKEN, response.text)

    def test_files_are_ordered_by_position(self) -> None:
        # 登録順をpositionと逆にした特典で確認する。
        self.execute("INSERT INTO benefits VALUES ('benefit-x', 'work-001', '特典X', '')")
        for file_id, position in (("x-3", 3), ("x-1", 1), ("x-2", 2)):
            self.execute(
                "INSERT INTO benefit_files VALUES (?, 'benefit-x', ?, ?, ?, 'audio/mpeg')",
                (file_id, position, f"曲{position}", f"benefit-x/{position:02d}.mp3"),
            )
        self.execute(
            "INSERT INTO entitlements (token, benefit_id, uid, status, issued_at) "
            "VALUES ('token-x', 'benefit-x', 'UID-X', 'unused', ?)",
            (format_utc(NOW),),
        )
        body = self.client.get("/api/r/token-x").json()
        self.assertEqual([file["id"] for file in body["files"]], ["x-1", "x-2", "x-3"])
        self.assertEqual([file["position"] for file in body["files"]], [1, 2, 3])
        self.assertEqual(body["benefit"], {"id": "benefit-x", "title": "特典X", "description": ""})

    def test_repeated_anonymous_get_keeps_entitlement_unused(self) -> None:
        before_row = self.entitlement_row(TOKEN)
        before = self.snapshot()
        for _ in range(3):
            self.assertEqual(self.get_state(self.client), "unused")
        # 別のブラウザ（URLプレビューなど）からのGETでも変わらない。
        self.assertEqual(self.get_state(self.enter_client()), "unused")
        after_row = self.entitlement_row(TOKEN)
        self.assertEqual(after_row["status"], "unused")
        self.assertIsNone(after_row["account_id"])
        self.assertIsNone(after_row["claimed_at"])
        self.assertEqual(after_row, before_row)
        self.assertEqual(self.snapshot(), before)

    def test_logged_in_get_does_not_claim_unused_entitlement(self) -> None:
        # ログイン済みでも、追加はPOST（claim/account）で行い、GETでは確定しない。
        self.login(self.client, "demo-alice")
        before = self.snapshot()
        self.assertEqual(self.get_state(self.client), "unused")
        self.assertEqual(self.snapshot(), before)

    # ---- アカウントに追加済み ----

    def test_account_claim_states_by_viewer(self) -> None:
        self.claim_for_account(TOKEN, "demo-alice")
        alice = self.enter_client()
        self.login(alice, "demo-alice")
        bob = self.enter_client()
        self.login(bob, "demo-bob")
        anonymous = self.enter_client()
        before = self.snapshot()
        for name, client, expected in (("本人", alice, "account_owned"),
                                       ("別アカウント", bob, "claimed"),
                                       ("未ログイン", anonymous, "claimed")):
            with self.subTest(viewer=name):
                response = client.get(f"/api/r/{TOKEN}")
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["state"], expected)
                self.assertEqual(set(body), {"state", "work", "benefit", "files"})
                # 使用済みでも何の特典か分かるよう、表示情報は返す。
                self.assertEqual(body["work"], EXPECTED_WORK)
                self.assertEqual(body["benefit"], EXPECTED_BENEFIT)
                self.assertEqual(body["files"], EXPECTED_FILES)
                # 受け取ったアカウントの情報は出さない。
                self.assertNotIn("demo-alice", response.text)
                self.assertNotIn("アリス", response.text)
        self.assertEqual(self.snapshot(), before)

    # ---- 登録なしで受け取り済み ----

    def test_download_with_valid_retry_cookie_is_retryable(self) -> None:
        self.claim_for_download(TOKEN)
        self.add_download_session("session-a", TOKEN, NOW + timedelta(seconds=1))
        self.set_retry_cookie(self.client, TOKEN, "session-a")
        before = self.snapshot()
        response = self.client.get(f"/api/r/{TOKEN}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["state"], "download_retryable")
        self.assertEqual(body["files"], EXPECTED_FILES)
        self.assertNotIn("session-a", response.text)
        self.assertEqual(self.snapshot(), before)

    def test_download_without_valid_retry_session_is_claimed(self) -> None:
        self.claim_for_download(TOKEN)
        self.claim_for_download(OTHER_TOKEN)
        self.add_download_session("session-expired", TOKEN, NOW - timedelta(seconds=1))
        self.add_download_session("session-boundary", TOKEN, NOW)  # 期限の時刻ちょうどは期限切れ。
        self.add_download_session("session-other", OTHER_TOKEN, NOW + timedelta(minutes=5))
        cases = (
            ("Cookieなし", None),
            ("存在しないセッション", "session-unknown"),
            ("別トークンのセッション", "session-other"),
            ("期限切れ", "session-expired"),
            ("期限ちょうど", "session-boundary"),
        )
        for name, session_id in cases:
            with self.subTest(case=name):
                client = self.enter_client()
                if session_id is not None:
                    self.set_retry_cookie(client, TOKEN, session_id)
                self.assertEqual(self.get_state(client), "claimed")

    def test_retry_cookie_for_other_token_path_is_not_sent(self) -> None:
        # 別のグッズのPathで保存したCookieは、このトークンのAPIには送られない。
        self.claim_for_download(TOKEN)
        self.add_download_session("session-a", TOKEN, NOW + timedelta(minutes=5))
        self.set_retry_cookie(self.client, OTHER_TOKEN, "session-a")
        self.assertEqual(self.get_state(self.client), "claimed")

    def test_retry_cookies_for_multiple_tokens_do_not_interfere(self) -> None:
        for token, session_id in ((TOKEN, "session-a"), (OTHER_TOKEN, "session-b")):
            self.claim_for_download(token)
            self.add_download_session(session_id, token, NOW + timedelta(minutes=5))
            self.set_retry_cookie(self.client, token, session_id)
        self.assertEqual(self.get_state(self.client, TOKEN), "download_retryable")
        self.assertEqual(self.get_state(self.client, OTHER_TOKEN), "download_retryable")

    def test_retry_expiry_uses_injected_now(self) -> None:
        self.claim_for_download(TOKEN)
        self.add_download_session("session-a", TOKEN, NOW + timedelta(minutes=10))
        self.set_retry_cookie(self.client, TOKEN, "session-a")
        self.assertEqual(self.get_state(self.client), "download_retryable")
        self.app.dependency_overrides[get_now] = lambda: NOW + timedelta(minutes=10)
        self.assertEqual(self.get_state(self.client), "claimed")

    # ---- 見つからない ----

    def test_missing_issuing_and_revoked_tokens_return_404(self) -> None:
        self.execute(
            "INSERT INTO entitlements (token, benefit_id, uid, status, issued_at) "
            "VALUES ('issuing-token-1234', 'benefit-001', 'UID-I', 'issuing', ?)",
            (format_utc(NOW),),
        )
        self.execute("UPDATE entitlements SET status = 'revoked' WHERE token = ?", (OTHER_TOKEN,))
        # account_id を残したまま無効にした取得権も、特典ページでは見つからない扱い。
        self.claim_for_account(DEV_TOKENS[2], "demo-alice")
        self.execute("UPDATE entitlements SET status = 'revoked' WHERE token = ?", (DEV_TOKENS[2],))
        self.login(self.client, "demo-alice")
        for token in ("missing-token-5678", "issuing-token-1234", OTHER_TOKEN, DEV_TOKENS[2]):
            with self.subTest(token=token):
                self.assert_not_found(token)

    # ---- 共通 ----

    def test_responses_have_no_store_and_common_headers(self) -> None:
        for token in (TOKEN, "missing-token-5678"):
            with self.subTest(token=token):
                response = self.client.get(f"/api/r/{token}")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
                self.assertEqual(response.headers["X-Robots-Tag"], "noindex")
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_retry_cookie_path_is_per_token(self) -> None:
        self.assertEqual(retry_cookie_path(TOKEN), f"/api/r/{TOKEN}")
        self.assertEqual(RETRY_COOKIE_NAME, "nfc_retry")

    def test_openapi_schema_has_no_file_path(self) -> None:
        schemas = self.client.get("/openapi.json").json()["components"]["schemas"]
        self.assertEqual(set(schemas["BenefitFileOut"]["properties"]), {"id", "position", "title", "mime_type"})
        self.assertEqual(set(schemas["ReceiveOut"]["properties"]), {"state", "work", "benefit", "files"})
        self.assertEqual(set(schemas["ReceiveOut"]["properties"]["state"]["enum"]),
                         {"unused", "account_owned", "download_retryable", "claimed"})


if __name__ == "__main__":
    unittest.main()
