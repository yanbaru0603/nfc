"""受取確定APIと登録なしダウンロードの認可・期限・ファイル配信を確認。"""

import io
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import unquote

from fastapi.testclient import TestClient
from httpx import Response

from backend.db import connect, format_utc
from backend.main import create_app
from backend.routes.receive import RETRY_COOKIE_NAME, get_now, retry_cookie_path
from seed import DEV_TOKENS, seed
from tests.test_receive_api import (
    EXPECTED_BENEFIT,
    EXPECTED_FILES,
    EXPECTED_WORK,
    NOW,
    OTHER_TOKEN,
    TOKEN,
    add_login_route,
    make_settings,
)


class ClaimApiTest(unittest.TestCase):
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

    def enter_client(self, *, base_url: str = "http://testserver") -> TestClient:
        client = TestClient(self.app, base_url=base_url)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def db(self) -> closing[sqlite3.Connection]:
        return closing(connect(self.settings.db_path))

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        with self.db() as conn:
            conn.execute(sql, params)

    def post_claim(self, method: str, token: str = TOKEN, client: TestClient | None = None) -> Response:
        client = self.client if client is None else client
        csrf_token = client.get("/api/session").json()["csrf_token"]
        return client.post(f"/api/r/{token}/claim/{method}", headers={"X-CSRF-Token": csrf_token})

    def login(self, client: TestClient | None = None) -> None:
        client = self.client if client is None else client
        csrf_token = client.get("/api/session").json()["csrf_token"]
        response = client.post("/api/_test/login/demo-alice", headers={"X-CSRF-Token": csrf_token})
        self.assertEqual(response.status_code, 200)

    def assert_error(self, response: Response, status: int, code: str) -> None:
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual(set(body), {"error"})
        self.assertEqual(set(body["error"]), {"code", "message"})
        self.assertEqual(body["error"]["code"], code)
        self.assertTrue(body["error"]["message"])
        self.assertNotIn(TOKEN, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")

    def download_paths(self, token: str = TOKEN) -> tuple[str, ...]:
        return (
            f"/api/r/{token}/download",
            f"/api/r/{token}/download/file-001",
            f"/api/r/{token}/archive",
        )

    def set_retry_cookie(self, client: TestClient, token: str, session_id: str) -> None:
        client.cookies.set(RETRY_COOKIE_NAME, session_id, domain="testserver.local",
                           path=retry_cookie_path(token))

    def invalid_tokens(self) -> tuple[str, ...]:
        self.execute(
            "INSERT INTO entitlements (token, benefit_id, uid, status, issued_at) "
            "VALUES ('issuing-token', 'benefit-001', 'UID-I', 'issuing', ?)",
            (format_utc(NOW),),
        )
        self.execute("UPDATE entitlements SET status = 'revoked' WHERE token = ?", (DEV_TOKENS[2],))
        return "missing-token", "issuing-token", DEV_TOKENS[2]

    def test_account_claim_requires_login(self) -> None:
        self.assert_error(self.post_claim("account"), 401, "login_required")

    def test_account_claim_returns_benefit_and_rejects_same_account_retry(self) -> None:
        self.login()
        response = self.post_claim("account")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"benefit_id": "benefit-001"})
        self.assert_error(self.post_claim("account"), 409, "already_claimed")
        with self.db() as conn:
            row = conn.execute("SELECT * FROM entitlements WHERE token = ?", (TOKEN,)).fetchone()
            self.assertEqual((row["status"], row["account_id"], row["claimed_at"]),
                             ("account", "demo-alice", format_utc(NOW)))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM download_sessions").fetchone()[0], 0)

    def test_claims_reject_missing_issuing_and_revoked_tokens(self) -> None:
        logged_in = self.enter_client()
        self.login(logged_in)
        for token in self.invalid_tokens():
            for method, client in (("account", logged_in), ("download", self.client)):
                with self.subTest(token=token, method=method):
                    response = self.post_claim(method, token, client)
                    self.assert_error(response, 404, "entitlement_not_found")
                    self.assertNotIn(token, response.text)

    def test_claims_without_csrf_are_rejected_without_consuming_token(self) -> None:
        logged_in = self.enter_client()
        self.login(logged_in)
        for method, client in (("account", logged_in), ("download", self.client)):
            with self.subTest(method=method):
                response = client.post(f"/api/r/{TOKEN}/claim/{method}")
                self.assert_error(response, 403, "csrf_invalid")
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM entitlements WHERE token = ?",
                                          (TOKEN,)).fetchone()[0], "unused")

    def test_download_claim_sets_cookie_and_commits_download_session(self) -> None:
        response = self.post_claim("download")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "expires_at": format_utc(NOW + timedelta(seconds=self.settings.retry_ttl_seconds)),
            "remaining_seconds": self.settings.retry_ttl_seconds,
        })
        cookie = SimpleCookie()
        for header in response.headers.get_list("set-cookie"):
            cookie.load(header)
        retry = cookie[RETRY_COOKIE_NAME]
        self.assertEqual(retry["path"], retry_cookie_path(TOKEN))
        self.assertTrue(retry["httponly"])
        self.assertEqual(retry["samesite"].lower(), "lax")
        self.assertEqual(retry["max-age"], str(self.settings.retry_ttl_seconds + 86400))
        self.assertFalse(retry["secure"])
        self.assertNotIn(retry.value, response.text)
        with self.db() as conn:
            entitlement = conn.execute("SELECT * FROM entitlements WHERE token = ?", (TOKEN,)).fetchone()
            session = conn.execute("SELECT * FROM download_sessions WHERE token = ?", (TOKEN,)).fetchone()
            self.assertEqual((entitlement["status"], entitlement["account_id"], entitlement["claimed_at"]),
                             ("download", None, format_utc(NOW)))
            self.assertEqual(session["id"], retry.value)
            self.assertEqual(session["expires_at"], response.json()["expires_at"])

    def test_download_cookie_secure_follows_settings(self) -> None:
        self.app.state.settings = replace(self.settings, cookie_secure=True)
        client = self.enter_client(base_url="https://testserver")
        response = self.post_claim("download", client=client)
        self.assertEqual(response.status_code, 200)
        cookie = SimpleCookie()
        for header in response.headers.get_list("set-cookie"):
            cookie.load(header)
        self.assertTrue(cookie[RETRY_COOKIE_NAME]["secure"])
        self.assertEqual(client.get(self.download_paths()[0]).status_code, 200)

    def test_download_claim_rejects_logged_in_account(self) -> None:
        self.login()
        self.assert_error(self.post_claim("download"), 403, "logged_in")
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM entitlements WHERE token = ?",
                                          (TOKEN,)).fetchone()[0], "unused")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM download_sessions").fetchone()[0], 0)

    def test_download_retry_and_switching_claim_method_return_conflict(self) -> None:
        self.assertEqual(self.post_claim("download").status_code, 200)
        retry_id = self.client.cookies.get(RETRY_COOKIE_NAME)
        response = self.post_claim("download")
        self.assert_error(response, 409, "already_claimed")
        self.assertFalse(any(header.startswith(RETRY_COOKIE_NAME + "=")
                             for header in response.headers.get_list("set-cookie")))
        self.assertEqual(self.client.cookies.get(RETRY_COOKIE_NAME), retry_id)
        self.login()
        self.assert_error(self.post_claim("account"), 409, "already_claimed")
        self.assertEqual(self.post_claim("account", OTHER_TOKEN).status_code, 200)
        self.assert_error(self.post_claim("download", OTHER_TOKEN, self.enter_client()), 409, "already_claimed")
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM download_sessions").fetchone()[0], 1)

    def test_same_browser_can_list_play_range_and_download_zip(self) -> None:
        self.assertEqual(self.post_claim("download").status_code, 200)
        listing = self.client.get(self.download_paths()[0])
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json(), {
            "expires_at": format_utc(NOW + timedelta(seconds=self.settings.retry_ttl_seconds)),
            "remaining_seconds": self.settings.retry_ttl_seconds,
            "work": EXPECTED_WORK,
            "benefit": EXPECTED_BENEFIT,
            "files": EXPECTED_FILES,
        })
        self.assertNotIn(self.client.cookies.get(RETRY_COOKIE_NAME), listing.text)
        audio_bytes = (self.settings.media_dir / "benefit-001/01.wav").read_bytes()
        audio = self.client.get(self.download_paths()[1], headers={"Range": "bytes=0-43"})
        self.assertEqual(audio.status_code, 206)
        self.assertEqual(audio.content, audio_bytes[:44])
        self.assertEqual(audio.headers["Content-Range"], f"bytes 0-43/{len(audio_bytes)}")
        self.assertEqual(audio.headers["Content-Type"], "audio/wav")
        self.assertTrue(audio.headers["Content-Disposition"].startswith("inline;"))
        archive = self.client.get(self.download_paths()[2])
        self.assertEqual(archive.status_code, 200)
        self.assertEqual(archive.headers["Content-Type"], "application/zip")
        disposition = unquote(archive.headers["Content-Disposition"])
        self.assertTrue(disposition.startswith("attachment;"))
        self.assertIn('filename="benefit.zip"', disposition)
        self.assertIn("星めぐりの物語_特装版CD 特典音源.zip", disposition)
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
            self.assertEqual(zipped.namelist(), ["01_奥多摩の空.wav", "02_ビールを注ぐ音.wav", "03_出来立てビール.wav"])
            for item in zipped.infolist():
                self.assertEqual(item.compress_type, zipfile.ZIP_STORED)
                self.assertTrue(item.flag_bits & 0x800)
            self.assertEqual(zipped.read("01_奥多摩の空.wav"), audio_bytes)
        for response in (listing, audio, archive):
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")

    def test_missing_unknown_and_other_token_cookies_are_forbidden(self) -> None:
        self.assertEqual(self.post_claim("download").status_code, 200)
        first_session = self.client.cookies.get(RETRY_COOKIE_NAME)
        self.assertEqual(self.post_claim("download", OTHER_TOKEN).status_code, 200)
        for session_id in (None, "unknown-session", first_session):
            client = self.enter_client()
            if session_id is not None:
                self.set_retry_cookie(client, OTHER_TOKEN, session_id)
            for path in self.download_paths(OTHER_TOKEN):
                with self.subTest(session_id=session_id, path=path):
                    response = client.get(path, headers={"Range": "bytes=0-43"})
                    self.assert_error(response, 403, "retry_forbidden")

    def test_all_download_routes_expire_at_server_deadline(self) -> None:
        self.assertEqual(self.post_claim("download").status_code, 200)
        for extra in (0, 1):
            self.app.dependency_overrides[get_now] = lambda: NOW + timedelta(
                seconds=self.settings.retry_ttl_seconds + extra)
            for path in self.download_paths():
                with self.subTest(extra=extra, path=path):
                    self.assert_error(self.client.get(path, headers={"Range": "bytes=0-43"}),
                                      410, "retry_expired")

    def test_invalid_token_is_checked_before_cookie_and_expiry(self) -> None:
        self.assertEqual(self.post_claim("download").status_code, 200)
        session_id = self.client.cookies.get(RETRY_COOKIE_NAME)
        self.app.dependency_overrides[get_now] = lambda: NOW + timedelta(days=1)
        tokens = self.invalid_tokens()
        self.execute("UPDATE entitlements SET status = 'revoked' WHERE token = ?", (TOKEN,))
        for token in (*tokens, TOKEN):
            self.set_retry_cookie(self.client, token, session_id)
            for client in (self.client, self.enter_client()):
                for path in self.download_paths(token):
                    with self.subTest(token=token, path=path):
                        response = client.get(path)
                        self.assert_error(response, 404, "entitlement_not_found")
                        self.assertNotIn(token, response.text)

    def test_file_from_another_tokens_benefit_is_not_found(self) -> None:
        self.execute("INSERT INTO benefits VALUES ('benefit-other', 'work-001', '別の特典', '')")
        self.execute(
            "INSERT INTO benefit_files VALUES ('file-other', 'benefit-other', 1, '別の曲', 'other.wav', 'audio/wav')",
        )
        self.execute("UPDATE entitlements SET benefit_id = 'benefit-other' WHERE token = ?", (OTHER_TOKEN,))
        self.assertEqual(self.post_claim("download").status_code, 200)
        for file_id in ("file-other", "missing-file"):
            with self.subTest(file_id=file_id):
                self.assert_error(self.client.get(f"/api/r/{TOKEN}/download/{file_id}"), 404, "file_not_found")

    def test_missing_media_returns_file_not_found_for_audio_and_archive(self) -> None:
        self.assertEqual(self.post_claim("download").status_code, 200)
        (self.settings.media_dir / "benefit-001/01.wav").unlink()
        for path in self.download_paths()[1:]:
            with self.subTest(path=path):
                self.assert_error(self.client.get(path), 404, "file_not_found")

    def test_remaining_seconds_uses_server_time_and_never_becomes_negative(self) -> None:
        self.assertEqual(self.post_claim("download").status_code, 200)
        for elapsed, remaining in ((123, 477), (599.9, 0)):
            self.app.dependency_overrides[get_now] = lambda: NOW + timedelta(seconds=elapsed)
            response = self.client.get(self.download_paths()[0])
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["remaining_seconds"], remaining)
            self.assertEqual(response.json()["expires_at"], format_utc(NOW + timedelta(seconds=600)))

    def test_retry_cookies_for_multiple_tokens_remain_independent(self) -> None:
        for token in (TOKEN, OTHER_TOKEN):
            self.assertEqual(self.post_claim("download", token).status_code, 200)
        retries = [cookie for cookie in self.client.cookies.jar if cookie.name == RETRY_COOKIE_NAME]
        self.assertEqual({cookie.path for cookie in retries}, {retry_cookie_path(TOKEN), retry_cookie_path(OTHER_TOKEN)})
        self.assertEqual(len({cookie.value for cookie in retries}), 2)
        for token in (TOKEN, OTHER_TOKEN):
            self.assertEqual(self.client.get(self.download_paths(token)[0]).status_code, 200)

    def test_new_response_models_are_exposed_in_openapi(self) -> None:
        document = self.client.get("/openapi.json").json()
        schemas = document["components"]["schemas"]
        for name, fields in (
            ("ClaimAccountOut", {"benefit_id"}),
            ("ClaimDownloadOut", {"expires_at", "remaining_seconds"}),
            ("DownloadOut", {"expires_at", "remaining_seconds", "work", "benefit", "files"}),
        ):
            with self.subTest(name=name):
                self.assertEqual(set(schemas[name]["properties"]), fields)
        self.assertEqual(schemas["DownloadOut"]["properties"]["remaining_seconds"]["minimum"], 0)
        self.assertEqual(document["paths"][f"/api/r/{{token}}/claim/account"]["post"]
                         ["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
                         "#/components/schemas/ClaimAccountOut")


if __name__ == "__main__":
    unittest.main()
