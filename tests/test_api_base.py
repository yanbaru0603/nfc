"""FastAPIの基盤（セッション、CSRF、共通ヘッダー、エラー形式）を確認。"""

import base64
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from backend.config import Settings
from backend.db import connect
from backend.deps import get_conn
from backend.main import create_app
from backend.routes import API_PREFIX
from backend.security import (
    SESSION_COOKIE_NAME,
    Account,
    current_account,
    ensure_csrf_token,
    login_session,
    logout_session,
    require_account,
)


class Payload(BaseModel):
    value: int


class TokenLookupError(Exception):
    """メッセージに受取トークンを含む例外（取得権が見つからない例外の代わり）。"""


def make_settings(root: Path, *, cookie_secure: bool = False) -> Settings:
    return Settings(
        db_path=root / "missing/dir/app.db",
        media_dir=root / "media",
        base_url="http://testserver",
        session_secret="test-secret",
        retry_ttl_seconds=600,
        cookie_secure=cookie_secure,
    )


def add_test_routes(app: FastAPI) -> None:
    """テスト用のダミーのルート。本物のルートと同じく /api 配下に足す。"""
    router = APIRouter(prefix=API_PREFIX + "/_test")

    @router.post("/echo")
    def echo(payload: Payload) -> dict[str, int]:
        return {"value": payload.value}

    @router.post("/login/{account_id}")
    def login(request: Request, account_id: str) -> dict[str, str]:
        return {"csrf_token": login_session(request, account_id)}

    @router.post("/logout")
    def logout(request: Request) -> dict[str, str]:
        return {"csrf_token": logout_session(request)}

    @router.get("/csrf")
    def csrf(request: Request) -> dict[str, str]:
        return {"csrf_token": ensure_csrf_token(request)}

    @router.get("/me")
    def me(account: Annotated[Account, Depends(require_account)]) -> dict[str, str]:
        return {"id": account.id}

    @router.get("/current")
    def current(
        request: Request,
        conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    ) -> dict[str, str | None]:
        account = current_account(request, conn)
        return {"id": None if account is None else account.id, "session": request.session.get("account_id")}

    @router.get("/boom")
    def boom() -> None:
        raise RuntimeError("秘密の詳細")

    @router.get("/boom-token/{token}")
    def boom_token(token: str) -> None:
        # 例外のメッセージ、原因（__cause__）、処理中の例外（__context__）のすべてにトークンを含める。
        try:
            try:
                raise LookupError(f"処理中の例外 {token}")
            except LookupError:
                raise TokenLookupError(f"取得権がありません: {token}")
        except TokenLookupError as error:
            raise RuntimeError(f"受取に失敗しました: {token}") from error

    @router.get("/cache")
    def cache() -> dict[str, str]:
        return {}

    app.include_router(router)


class ApiBaseTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings = make_settings(self.root)
        self.app = create_app(self.settings)
        add_test_routes(self.app)
        self.client = self.enter_client(self.app)

    def enter_client(self, app: FastAPI, **kwargs: object) -> TestClient:
        client = TestClient(app, **kwargs)  # type: ignore[arg-type]
        client.__enter__()  # lifespan を実行する
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def add_account(self, account_id: str, name: str) -> None:
        with closing(connect(self.settings.db_path)) as conn:
            conn.execute("INSERT INTO accounts VALUES (?, ?)", (account_id, name))

    def csrf_token(self) -> str:
        return self.client.get("/api/session").json()["csrf_token"]

    def assert_error(self, response, status: int, code: str) -> None:  # type: ignore[no-untyped-def]
        self.assertEqual(response.status_code, status)
        self.assertTrue(response.headers["content-type"].startswith("application/json"))
        body = response.json()
        self.assertEqual(set(body), {"error"})
        self.assertEqual(set(body["error"]), {"code", "message"})
        self.assertEqual(body["error"]["code"], code)
        self.assertTrue(body["error"]["message"])

    def test_session_returns_null_account_and_stable_csrf_token(self) -> None:
        first = self.client.get("/api/session")
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertIsNone(body["account"])
        self.assertGreaterEqual(len(body["csrf_token"]), 40)
        self.assertIn(SESSION_COOKIE_NAME, self.client.cookies)
        second = self.client.get("/api/session").json()
        self.assertEqual(second["csrf_token"], body["csrf_token"])

    def test_separate_browsers_get_different_csrf_tokens(self) -> None:
        other = self.enter_client(self.app)
        self.assertNotEqual(self.csrf_token(), other.get("/api/session").json()["csrf_token"])

    def test_missing_db_path_is_initialized_on_startup(self) -> None:
        self.assertTrue(self.settings.db_path.exists())
        with closing(connect(self.settings.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)
        self.assertEqual(self.client.get("/api/session").status_code, 200)

    def test_post_without_csrf_token_is_rejected(self) -> None:
        # セッションがない状態（未ログインの初回POST）も拒否する。
        self.assert_error(self.client.post("/api/_test/echo", json={"value": 1}), 403, "csrf_invalid")
        self.csrf_token()
        self.assert_error(self.client.post("/api/_test/echo", json={"value": 1}), 403, "csrf_invalid")

    def test_post_with_wrong_csrf_token_is_rejected(self) -> None:
        token = self.csrf_token()
        for wrong in ("", "wrong", token + "x", token[:-1]):
            with self.subTest(wrong=wrong):
                response = self.client.post("/api/_test/echo", json={"value": 1}, headers={"X-CSRF-Token": wrong})
                self.assert_error(response, 403, "csrf_invalid")

    def test_csrf_token_from_another_browser_is_rejected(self) -> None:
        other = self.enter_client(self.app)
        other_token = other.get("/api/session").json()["csrf_token"]
        self.csrf_token()
        response = self.client.post("/api/_test/echo", json={"value": 1}, headers={"X-CSRF-Token": other_token})
        self.assert_error(response, 403, "csrf_invalid")

    def test_post_with_matching_csrf_token_succeeds(self) -> None:
        token = self.csrf_token()
        response = self.client.post("/api/_test/echo", json={"value": 7}, headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"value": 7})

    def test_csrf_is_checked_for_unsafe_methods_on_undefined_api_paths(self) -> None:
        # ルートの有無より先にCSRFを検証し、付け忘れが起きないことを確認する。
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                self.assert_error(self.client.request(method, "/api/undefined"), 403, "csrf_invalid")

    def test_common_headers_and_no_store(self) -> None:
        for path in ("/api/session", "/api/_test/cache", "/api/undefined"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
                self.assertEqual(response.headers["X-Robots-Tag"], "noindex")
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        csrf_error = self.client.post("/api/_test/echo", json={"value": 1})
        self.assertEqual(csrf_error.headers["Cache-Control"], "no-store")
        self.assertEqual(csrf_error.headers["Referrer-Policy"], "no-referrer")

    def test_non_api_paths_get_common_headers_without_no_store(self) -> None:
        response = self.client.get("/r/some-token")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.headers["X-Robots-Tag"], "noindex")
        self.assertNotIn("Cache-Control", response.headers)

    def test_undefined_api_path_returns_json_404(self) -> None:
        self.assert_error(self.client.get("/api/undefined"), 404, "not_found")
        self.assert_error(self.client.get("/api"), 404, "not_found")

    def test_method_not_allowed_returns_json_405(self) -> None:
        response = self.client.post("/api/session", headers={"X-CSRF-Token": self.csrf_token()})
        self.assert_error(response, 405, "method_not_allowed")
        self.assertIn("GET", response.headers["Allow"])

    def test_validation_error_returns_json_422(self) -> None:
        token = self.csrf_token()
        for body in ({"value": "abc"}, {}):
            with self.subTest(body=body):
                response = self.client.post("/api/_test/echo", json=body, headers={"X-CSRF-Token": token})
                self.assert_error(response, 422, "validation_error")

    def test_unexpected_error_returns_json_500_without_details(self) -> None:
        client = self.enter_client(self.app, raise_server_exceptions=False)
        with self.assertLogs("backend.main", level="ERROR"):
            response = client.get("/api/_test/boom")
        self.assert_error(response, 500, "internal_error")
        self.assertNotIn("秘密の詳細", response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")

    def test_unexpected_error_log_omits_token_in_messages_and_chained_exceptions(self) -> None:
        token = "SECRET-RECEIVE-TOKEN-1234"
        # 既定の TestClient は再送出された例外を投げるので、Uvicorn に例外が渡らないことも確認できる。
        with self.assertLogs("backend.main", level="ERROR") as logs:
            response = self.client.get(f"/api/_test/boom-token/{token}")
        self.assert_error(response, 500, "internal_error")
        self.assertNotIn(token, response.text)

        self.assertEqual(len(logs.records), 1)
        record = logs.records[0]
        self.assertIsNone(record.exc_info)
        output = "\n".join(logs.output)
        self.assertNotIn(token, output)
        self.assertNotIn("受取に失敗しました", output)
        # 型名とフレーム情報は残し、連鎖した例外も型名だけ記録する。
        self.assertIn("builtins.RuntimeError", output)
        self.assertIn("原因: tests.test_api_base.TokenLookupError", output)
        self.assertIn("処理中に発生した例外: builtins.LookupError", output)
        self.assertIn("in boom_token", output)
        self.assertIn('test_api_base.py", line ', output)

    def test_require_account_rejects_anonymous_with_401(self) -> None:
        self.assert_error(self.client.get("/api/_test/me"), 401, "login_required")

    def test_login_session_sets_account_and_rotates_csrf_token(self) -> None:
        self.add_account("alice", "アリス")
        before = self.csrf_token()
        response = self.client.post("/api/_test/login/alice", headers={"X-CSRF-Token": before})
        self.assertEqual(response.status_code, 200)
        after = response.json()["csrf_token"]
        self.assertNotEqual(after, before)

        session = self.client.get("/api/session").json()
        self.assertEqual(session, {"account": {"id": "alice", "name": "アリス"}, "csrf_token": after})
        self.assertEqual(self.client.get("/api/_test/me").json(), {"id": "alice"})
        # ログイン前のトークンは使えない。
        response = self.client.post("/api/_test/echo", json={"value": 1}, headers={"X-CSRF-Token": before})
        self.assert_error(response, 403, "csrf_invalid")

    def test_logout_session_clears_account_and_rotates_csrf_token(self) -> None:
        self.add_account("alice", "アリス")
        token = self.client.post("/api/_test/login/alice", headers={"X-CSRF-Token": self.csrf_token()}).json()[
            "csrf_token"
        ]
        response = self.client.post("/api/_test/logout", headers={"X-CSRF-Token": token})
        new_token = response.json()["csrf_token"]
        self.assertNotEqual(new_token, token)
        self.assertEqual(self.client.get("/api/session").json(), {"account": None, "csrf_token": new_token})

    def test_current_account_drops_unknown_account_from_session(self) -> None:
        token = self.csrf_token()
        self.client.post("/api/_test/login/ghost", headers={"X-CSRF-Token": token})
        self.assertEqual(self.client.get("/api/_test/current").json(), {"id": None, "session": None})
        # 消したことがCookieに保存され、次のリクエストでもセッションに残らない。
        self.assertEqual(self.client.get("/api/_test/current").json(), {"id": None, "session": None})
        self.assertIsNone(self.client.get("/api/session").json()["account"])
        self.assert_error(self.client.get("/api/_test/me"), 401, "login_required")

    def test_current_account_drops_deleted_account(self) -> None:
        self.add_account("alice", "アリス")
        self.client.post("/api/_test/login/alice", headers={"X-CSRF-Token": self.csrf_token()})
        self.assertEqual(self.client.get("/api/_test/current").json(), {"id": "alice", "session": "alice"})
        with closing(connect(self.settings.db_path)) as conn:
            conn.execute("DELETE FROM accounts WHERE id = 'alice'")
        self.assertIsNone(self.client.get("/api/session").json()["account"])
        self.assertEqual(self.client.get("/api/_test/current").json(), {"id": None, "session": None})

    def test_tampered_session_cookie_loses_login(self) -> None:
        self.add_account("alice", "アリス")
        self.add_account("bob", "ボブ")
        token = self.client.post("/api/_test/login/alice", headers={"X-CSRF-Token": self.csrf_token()}).json()[
            "csrf_token"
        ]
        self.assertEqual(self.client.get("/api/session").json()["account"], {"id": "alice", "name": "アリス"})

        # 署名はそのままで、中身だけを別のアカウントに書き換える。
        original = self.client.cookies[SESSION_COOKIE_NAME]
        _, timestamp, signature = original.split(".")
        forged = base64.b64encode(json.dumps({"account_id": "bob", "csrf_token": token}).encode()).decode()
        self.client.cookies.clear()
        self.client.cookies.set(SESSION_COOKIE_NAME, f"{forged}.{timestamp}.{signature}")

        body = self.client.get("/api/session").json()
        self.assertIsNone(body["account"])
        self.assertTrue(body["csrf_token"])
        self.assertNotEqual(body["csrf_token"], token)
        self.assert_error(self.client.get("/api/_test/me"), 401, "login_required")

    def test_session_cookie_attributes(self) -> None:
        cookie = self.session_set_cookie(self.client)
        attributes = [part.strip().lower() for part in cookie.split(";")]
        self.assertTrue(cookie.startswith(SESSION_COOKIE_NAME + "="))
        self.assertIn("httponly", attributes)
        self.assertIn("samesite=lax", attributes)
        self.assertIn("path=/", attributes)
        self.assertNotIn("secure", attributes)

    def test_session_cookie_is_secure_when_configured(self) -> None:
        app = create_app(make_settings(self.root, cookie_secure=True))
        client = self.enter_client(app, base_url="https://testserver")
        attributes = [part.strip().lower() for part in self.session_set_cookie(client).split(";")]
        self.assertIn("secure", attributes)
        self.assertIn("httponly", attributes)
        self.assertIn("samesite=lax", attributes)

    def session_set_cookie(self, client: TestClient) -> str:
        response = client.get("/api/session")
        cookies = [value for value in response.headers.get_list("set-cookie")
                   if value.startswith(SESSION_COOKIE_NAME + "=")]
        self.assertEqual(len(cookies), 1)
        return cookies[0]

    def test_openapi_lists_session_endpoint(self) -> None:
        schema = self.client.get("/openapi.json").json()
        self.assertIn("/api/session", schema["paths"])
        self.assertIn("SessionOut", schema["components"]["schemas"])
        self.assertIn("ErrorResponse", schema["components"]["schemas"])


class AppFactoryTest(unittest.TestCase):
    def test_importing_main_does_not_create_app(self) -> None:
        import backend.main

        self.assertFalse(hasattr(backend.main, "app"))

    def test_settings_are_stored_on_app_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory).resolve())
            app = create_app(settings)
            self.assertIs(app.state.settings, settings)
            # lifespan を実行するまではDBを作らない。
            self.assertFalse(settings.db_path.exists())


if __name__ == "__main__":
    unittest.main()
