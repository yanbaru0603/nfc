"""デモログイン（accounts・prepare・login・logout）と簡易ライブラリのAPIを確認。"""

import io
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.db import connect, format_utc
from backend.main import create_app
from backend.routes.account import SESSION_RETURN_TO_KEY
from seed import DEV_TOKENS, seed


NOW = datetime(2026, 10, 17, 1, 0, 0, tzinfo=UTC)
TOKEN = DEV_TOKENS[0]

EXPECTED_WORK = {"id": "work-001", "title": "星めぐりの物語", "cover_path": "/covers/work-001.svg"}
EXPECTED_BENEFIT = {"id": "benefit-001", "title": "特装版CD 特典音源", "description": "デモ用に生成した3曲の単音音源"}


def make_settings(root: Path) -> Settings:
    return Settings(
        db_path=root / "app.db",
        media_dir=root / "media",
        base_url="http://testserver",
        session_secret="test-secret",
        retry_ttl_seconds=600,
        cookie_secure=False,
    )


class AccountApiTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings = make_settings(self.root)
        self.app = create_app(self.settings)
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

    def post(self, client: TestClient, path: str, json: object | None = None) -> httpx.Response:
        """その時点のCSRFトークンを付けてPOSTする。"""
        csrf_token = client.get("/api/session").json()["csrf_token"]
        return client.post(path, json=json, headers={"X-CSRF-Token": csrf_token})

    def login(self, client: TestClient, account_id: str) -> httpx.Response:
        response = self.post(client, "/api/auth/login", {"account_id": account_id})
        self.assertEqual(response.status_code, 200)
        return response

    def claim_for_account(self, token: str, account_id: str, claimed_at: datetime = NOW) -> None:
        self.execute(
            "UPDATE entitlements SET status = 'account', account_id = ?, claimed_at = ? WHERE token = ?",
            (account_id, format_utc(claimed_at), token),
        )

    def add_benefit(self, benefit_id: str, title: str) -> None:
        """2つ目の特典（ファイル1つ）を追加。"""
        self.execute("INSERT INTO benefits VALUES (?, 'work-001', ?, '')", (benefit_id, title))
        self.execute(
            "INSERT INTO benefit_files VALUES (?, ?, 1, '曲X', ?, 'audio/wav')",
            (f"{benefit_id}-file", benefit_id, f"{benefit_id}/01.wav"),
        )
        path = self.settings.media_dir / benefit_id / "01.wav"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x" * 16)

    def add_entitlement(self, token: str, benefit_id: str, status: str = "unused",
                        account_id: str | None = None, claimed_at: datetime | None = None) -> None:
        self.execute(
            "INSERT INTO entitlements (token, benefit_id, uid, status, account_id, issued_at, claimed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token, benefit_id, f"UID-{token}", status, account_id, format_utc(NOW),
             None if claimed_at is None else format_utc(claimed_at)),
        )

    def assert_error(self, response: httpx.Response, status: int, code: str) -> None:
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertTrue(response.json()["error"]["message"])

    # ---- デモログイン ----

    def test_accounts_are_listed_by_id(self) -> None:
        response = self.client.get("/api/auth/accounts")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accounts": [
            {"id": "demo-alice", "name": "アリス"},
            {"id": "demo-bob", "name": "ボブ"},
            {"id": "demo-carol", "name": "キャロル"},
        ]})
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_prepared_return_to_is_used_once(self) -> None:
        response = self.post(self.client, "/api/auth/prepare", {"token": TOKEN})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"return_to": f"/r/{TOKEN}"})
        first = self.login(self.client, "demo-alice").json()
        self.assertEqual(first["account"], {"id": "demo-alice", "name": "アリス"})
        self.assertEqual(first["return_to"], f"/r/{TOKEN}")
        self.assertEqual(self.client.get("/api/session").json()["csrf_token"], first["csrf_token"])
        # 使った戻り先は消えるので、2回目のログインはライブラリへ。
        self.assertEqual(self.login(self.client, "demo-bob").json()["return_to"], "/library")
        self.assertEqual(SESSION_RETURN_TO_KEY, "return_to")

    def test_prepare_rejects_missing_issuing_and_revoked_tokens(self) -> None:
        self.add_entitlement("issuing-token-1234", "benefit-001", status="issuing")
        self.execute("UPDATE entitlements SET status = 'revoked' WHERE token = ?", (DEV_TOKENS[1],))
        for token in ("missing-token-5678", "issuing-token-1234", DEV_TOKENS[1], "https://evil.example/"):
            with self.subTest(token=token):
                response = self.post(self.client, "/api/auth/prepare", {"token": token})
                self.assert_error(response, 404, "entitlement_not_found")
                self.assertNotIn(token, response.text)
        # 拒否した戻り先は保存されない。
        self.assertEqual(self.login(self.client, "demo-alice").json()["return_to"], "/library")

    def test_login_errors(self) -> None:
        self.assert_error(self.post(self.client, "/api/auth/login", {"account_id": "nobody"}),
                          404, "account_not_found")
        self.assert_error(self.post(self.client, "/api/auth/login", {}), 422, "validation_error")
        self.assertIsNone(self.client.get("/api/session").json()["account"])

    def test_logout(self) -> None:
        self.assert_error(self.post(self.client, "/api/auth/logout"), 401, "login_required")
        login_token = self.login(self.client, "demo-alice").json()["csrf_token"]
        response = self.post(self.client, "/api/auth/logout")
        self.assertEqual(response.status_code, 200)
        csrf_token = response.json()["csrf_token"]
        self.assertNotEqual(csrf_token, login_token)
        session = self.client.get("/api/session").json()
        self.assertIsNone(session["account"])
        self.assertEqual(session["csrf_token"], csrf_token)

    # ---- ライブラリ ----

    def test_library_requires_login(self) -> None:
        for path in ("/api/library", "/api/library/benefit-001",
                     "/api/library/benefit-001/archive", "/api/library/files/file-001"):
            with self.subTest(path=path):
                self.assert_error(self.client.get(path), 401, "login_required")

    def test_library_groups_reissued_and_revoked_entitlements(self) -> None:
        # 同じ特典を再発行前後の2つの取得権で受け取り、古い方は無効にした状態。
        self.add_benefit("benefit-002", "特典2")
        self.claim_for_account(TOKEN, "demo-alice", NOW - timedelta(hours=2))
        self.execute("UPDATE entitlements SET status = 'revoked' WHERE token = ?", (TOKEN,))
        self.claim_for_account(DEV_TOKENS[1], "demo-alice", NOW - timedelta(hours=1))
        self.add_entitlement("token-b2", "benefit-002", "account", "demo-alice", NOW)
        self.login(self.client, "demo-alice")
        response = self.client.get("/api/library")
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        # 最初に受け取った日時の新しい順で、特典ごとに1件。
        self.assertEqual([item["benefit"]["id"] for item in items], ["benefit-002", "benefit-001"])
        self.assertEqual(items[1], {"work": EXPECTED_WORK, "benefit": EXPECTED_BENEFIT})

        # 無効にした取得権だけで受け取った特典も、詳細を取得できる。
        self.execute("UPDATE entitlements SET status = 'revoked' WHERE token = ?", (DEV_TOKENS[1],))
        detail = self.client.get("/api/library/benefit-001")
        self.assertEqual(detail.status_code, 200)
        body = detail.json()
        self.assertEqual(set(body), {"work", "benefit", "files"})
        self.assertEqual([file["id"] for file in body["files"]], ["file-001", "file-002", "file-003"])
        self.assertNotIn("file_path", detail.text)
        self.assertNotIn(".wav", detail.text)

    def test_other_accounts_benefit_is_not_found(self) -> None:
        self.claim_for_account(TOKEN, "demo-alice")
        self.login(self.client, "demo-bob")
        self.assertEqual(self.client.get("/api/library").json(), {"items": []})
        self.assert_error(self.client.get("/api/library/benefit-001"), 404, "benefit_not_found")
        self.assert_error(self.client.get("/api/library/benefit-001/archive"), 404, "benefit_not_found")
        self.assert_error(self.client.get("/api/library/missing"), 404, "benefit_not_found")
        self.assert_error(self.client.get("/api/library/files/file-001"), 404, "file_not_found")

    # ---- ファイル ----

    def test_file_is_inline_and_supports_range(self) -> None:
        self.claim_for_account(TOKEN, "demo-alice")
        self.login(self.client, "demo-alice")
        whole = (self.settings.media_dir / "benefit-001/01.wav").read_bytes()
        response = self.client.get("/api/library/files/file-001")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, whole)
        self.assertEqual(response.headers["content-type"], "audio/wav")
        self.assertTrue(response.headers["Content-Disposition"].startswith("inline;"))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        partial = self.client.get("/api/library/files/file-001", headers={"Range": "bytes=0-9"})
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, whole[:10])
        self.assertEqual(partial.headers["Content-Range"], f"bytes 0-9/{len(whole)}")

    def test_archive_is_attachment_with_japanese_name(self) -> None:
        self.claim_for_account(TOKEN, "demo-alice")
        self.login(self.client, "demo-alice")
        response = self.client.get("/api/library/benefit-001/archive")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        disposition = response.headers["Content-Disposition"]
        self.assertTrue(disposition.startswith("attachment;"))
        self.assertIn('filename="benefit.zip"', disposition)
        self.assertIn("filename*=UTF-8''" + quote("星めぐりの物語_特装版CD 特典音源.zip", safe=""), disposition)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(archive.namelist(), ["01_朝の光.wav", "02_風の便り.wav", "03_星の帰り道.wav"])
            self.assertTrue(all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist()))

    def test_missing_media_file_returns_file_not_found(self) -> None:
        self.claim_for_account(TOKEN, "demo-alice")
        self.login(self.client, "demo-alice")
        (self.settings.media_dir / "benefit-001/02.wav").unlink()
        self.assert_error(self.client.get("/api/library/files/file-002"), 404, "file_not_found")
        self.assert_error(self.client.get("/api/library/benefit-001/archive"), 404, "file_not_found")


if __name__ == "__main__":
    unittest.main()
