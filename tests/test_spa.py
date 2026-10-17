"""ビルド済みSPAの配信と、アクセスログの受取トークンの伏せ字を確認。"""

import io
import logging
import logging.config
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from fastapi.testclient import TestClient

from backend.config import Settings
from backend.logs import ACCESS_LOGGER_NAME, RedactTokenFilter, redact_receive_tokens, uvicorn_log_config
from backend.main import create_app
from backend.spa import SpaFiles


INDEX_HTML = "<!doctype html><title>SPA</title>"
SCRIPT = "console.log('app');"


def make_settings(root: Path) -> Settings:
    return Settings(
        db_path=root / "app.db",
        media_dir=root / "media",
        base_url="http://testserver",
        session_secret="test-secret",
        retry_ttl_seconds=600,
        cookie_secure=False,
    )


class SpaTestBase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.dist = self.root / "dist"
        (self.dist / "assets").mkdir(parents=True)
        (self.dist / "covers").mkdir()
        (self.dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        (self.dist / "assets/index-abc123.js").write_text(SCRIPT, encoding="utf-8")
        (self.dist / "covers/work-001.svg").write_text("<svg/>", encoding="utf-8")
        # dist の外に置いた、配信してはいけないファイル。
        (self.root / "secret.txt").write_text("secret", encoding="utf-8")

    def make_client(self, dist_dir: Path | None) -> TestClient:
        client = TestClient(create_app(make_settings(self.root), dist_dir=dist_dir))
        client.__enter__()  # lifespan を実行する
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def assert_json_404(self, response) -> None:  # type: ignore[no-untyped-def]
        self.assertEqual(response.status_code, 404)
        self.assertTrue(response.headers["content-type"].startswith("application/json"))
        self.assertEqual(response.json()["error"]["code"], "not_found")


class SpaServingTest(SpaTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.client = self.make_client(self.dist)

    def assert_index(self, response) -> None:  # type: ignore[no-untyped-def]
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.headers["X-Robots-Tag"], "noindex")

    def test_screen_urls_return_index_html_with_no_store(self) -> None:
        for path in ("/", "/library", "/library/benefit-001", "/login", "/r/xxx",
                     "/r/xxx/download", "/index.html", "/unknown/screen"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assert_index(response)
                self.assertEqual(response.text, INDEX_HTML)

    def test_head_returns_index_headers_without_body(self) -> None:
        response = self.client.head("/r/xxx")
        self.assert_index(response)
        self.assertEqual(response.content, b"")

    def test_real_files_are_served_as_is(self) -> None:
        script = self.client.get("/assets/index-abc123.js")
        self.assertEqual(script.status_code, 200)
        self.assertEqual(script.text, SCRIPT)
        self.assertIn("javascript", script.headers["content-type"])
        self.assertNotIn("Cache-Control", script.headers)
        self.assertEqual(script.headers["X-Content-Type-Options"], "nosniff")
        cover = self.client.get("/covers/work-001.svg")
        self.assertEqual(cover.status_code, 200)
        self.assertEqual(cover.headers["content-type"], "image/svg+xml")

    def test_missing_static_files_stay_404(self) -> None:
        for path in ("/assets/missing.js", "/assets/sub/missing", "/covers/missing.svg",
                     "/favicon.ico", "/assets", "/covers/"):
            with self.subTest(path=path):
                self.assert_json_404(self.client.get(path))

    def test_api_404_and_405_stay_json(self) -> None:
        for path in ("/api/nope", "/api", "/api/r/xxx/nope"):
            with self.subTest(path=path):
                self.assert_json_404(self.client.get(path))
        head = self.client.head("/api/nope")  # HEAD は本文なしで、ヘッダーだけJSONの404。
        self.assertEqual(head.status_code, 404)
        self.assertTrue(head.headers["content-type"].startswith("application/json"))
        csrf_token = self.client.get("/api/session").json()["csrf_token"]
        method_not_allowed = self.client.post("/api/session", headers={"X-CSRF-Token": csrf_token})
        self.assertEqual(method_not_allowed.status_code, 405)
        self.assertEqual(method_not_allowed.json()["error"]["code"], "method_not_allowed")

    def test_api_routes_still_work(self) -> None:
        response = self.client.get("/api/session")
        self.assertEqual(response.status_code, 200)
        self.assertIn("csrf_token", response.json())

    def test_non_get_methods_on_screen_urls_are_not_replaced(self) -> None:
        response = self.client.delete("/library")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(response.headers["content-type"].startswith("application/json"))

    def test_parent_directory_traversal_is_rejected(self) -> None:
        # httpx は `..` を正規化するので、エンコードした形で送る（Uvicornと同じくデコード後に判定）。
        for path in ("/%2e%2e/secret.txt", "/assets/%2e%2e/%2e%2e/secret.txt",
                     "/assets%2f..%2f..%2fsecret.txt", "/assets/..%5c..%5csecret.txt"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertNotIn("secret", response.text)
                self.assert_json_404(response)

    def test_symlink_outside_dist_is_not_served(self) -> None:
        (self.dist / "assets/link.txt").symlink_to(self.root / "secret.txt")
        response = self.client.get("/assets/link.txt")
        self.assertNotIn("secret", response.text)
        self.assert_json_404(response)

    def test_index_replaced_by_symlink_outside_dist_is_not_served(self) -> None:
        # 起動後に index.html が dist の外を指すリンクに置き換えられても、中身を返さない。
        (self.dist / "index.html").unlink()
        (self.dist / "index.html").symlink_to(self.root / "secret.txt")
        for path in ("/", "/library", "/r/xxx", "/index.html"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertNotIn("secret", response.text)
                self.assert_json_404(response)


class SpaMissingTest(SpaTestBase):
    def test_without_dist_non_api_paths_return_json_404(self) -> None:
        for dist_dir in (self.root / "missing", None):
            with self.subTest(dist_dir=dist_dir):
                client = self.make_client(dist_dir)
                for path in ("/library", "/r/xxx", "/assets/index-abc123.js"):
                    response = client.get(path)
                    self.assert_json_404(response)
                    self.assertNotIn("Cache-Control", response.headers)
                self.assertEqual(client.get("/api/session").status_code, 200)

    def test_dist_without_index_html_is_not_served(self) -> None:
        (self.dist / "index.html").unlink()
        client = self.make_client(self.dist)
        self.assert_json_404(client.get("/library"))
        self.assert_json_404(client.get("/assets/index-abc123.js"))

    def test_index_symlink_outside_dist_is_not_served(self) -> None:
        (self.dist / "index.html").unlink()
        (self.dist / "index.html").symlink_to(self.root / "secret.txt")
        self.assertIsNone(SpaFiles.load(self.dist))
        client = self.make_client(self.dist)
        for path in ("/", "/library", "/index.html", "/assets/index-abc123.js"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertNotIn("secret", response.text)
                self.assert_json_404(response)

    def test_index_symlink_inside_dist_is_served(self) -> None:
        (self.dist / "index.html").rename(self.dist / "app.html")
        (self.dist / "index.html").symlink_to(self.dist / "app.html")
        client = self.make_client(self.dist)
        response = client.get("/library")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, INDEX_HTML)
        self.assertEqual(response.headers["Cache-Control"], "no-store")


class RedactTokenTest(unittest.TestCase):
    def test_redact_receive_tokens(self) -> None:
        cases = {
            "/r/AbC_-123": "/r/***",
            "/api/r/AbC_-123": "/api/r/***",
            "/api/r/AbC_-123/download/file-001": "/api/r/***/download/file-001",
            "/api/r/AbC_-123/archive?x=1": "/api/r/***/archive?x=1",
            "/r/%E3%81%82": "/r/***",
            "/library/benefit-001": "/library/benefit-001",
            "/api/session": "/api/session",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(redact_receive_tokens(text), expected)

    def test_filter_redacts_access_log_arguments(self) -> None:
        record = logging.LogRecord(
            ACCESS_LOGGER_NAME, logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:5000", "GET", "/api/r/secret-token/archive", "1.1", 200), None,
        )
        self.assertTrue(RedactTokenFilter().filter(record))
        self.assertEqual(record.args, ("127.0.0.1:5000", "GET", "/api/r/***/archive", "1.1", 200))
        self.assertNotIn("secret-token", record.getMessage())

    def test_uvicorn_access_log_is_redacted(self) -> None:
        """serve.py と同じログ設定で、Uvicornの AccessFormatter の出力から伏せられることを確認。"""
        config = uvicorn_log_config()
        config["handlers"]["access"]["stream"] = "ext://sys.stdout"
        config["formatters"]["access"]["use_colors"] = False
        logger = logging.getLogger(ACCESS_LOGGER_NAME)
        # dictConfig が設定する uvicorn の各ロガーを、テスト後に元に戻す。
        loggers = [logging.getLogger(name) for name in ("uvicorn", "uvicorn.error", ACCESS_LOGGER_NAME)]
        saved = [(item.handlers[:], item.level, item.propagate, item.disabled) for item in loggers]

        def restore() -> None:
            for item, (handlers, level, propagate, disabled) in zip(loggers, saved):
                item.handlers[:], item.level, item.propagate, item.disabled = handlers, level, propagate, disabled

        self.addCleanup(restore)
        output = io.StringIO()
        with redirect_stdout(output):
            logging.config.dictConfig(config)  # ext://sys.stdout をここで解決する
            logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/r/secret-token", "1.1", 200)
            logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "POST",
                        "/api/r/secret-token/claim/download", "1.1", 201)
        text = output.getvalue()
        self.assertNotIn("secret-token", text)
        self.assertIn('"GET /r/*** HTTP/1.1" 200', text)
        self.assertIn('"POST /api/r/***/claim/download HTTP/1.1" 201', text)

    def test_filter_redacts_exception_and_stack_info(self) -> None:
        """uvicorn.error などに exc_info・stack_info 付きで渡っても、トレースバックから伏せる。"""
        from uvicorn.logging import DefaultFormatter

        config = uvicorn_log_config()
        self.assertIn("redact_token", config["handlers"]["default"]["filters"])
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.setFormatter(DefaultFormatter("%(levelprefix)s %(message)s", use_colors=False))
        handler.addFilter(RedactTokenFilter())
        logger = logging.getLogger("tests.test_spa.redact")
        logger.addHandler(handler)
        logger.propagate = False
        self.addCleanup(logger.removeHandler, handler)
        try:
            try:
                raise LookupError("not found: /api/r/secret-token/archive")
            except LookupError as error:
                raise RuntimeError("failed: /r/secret-token") from error
        except RuntimeError:
            logger.exception("Exception in ASGI application", stack_info=True)
        text = output.getvalue()
        self.assertNotIn("secret-token", text)
        self.assertIn("RuntimeError: failed: /r/***", text)
        self.assertIn("LookupError: not found: /api/r/***/archive", text)
        self.assertIn("Stack (most recent call last)", text)

    def test_filter_replaces_exc_info_with_redacted_text(self) -> None:
        try:
            raise ValueError("/api/r/secret-token")
        except ValueError:
            record = logging.LogRecord(
                "uvicorn.error", logging.ERROR, __file__, 1, "error", None, sys.exc_info(),
                sinfo="Stack (most recent call last):\n  GET /r/secret-token",
            )
        self.assertTrue(RedactTokenFilter().filter(record))
        self.assertIsNone(record.exc_info)
        self.assertIn("ValueError: /api/r/***", record.exc_text or "")
        self.assertNotIn("secret-token", record.stack_info or "")
        self.assertNotIn("secret-token", logging.Formatter().format(record))

    def test_log_config_does_not_change_uvicorn_defaults(self) -> None:
        from uvicorn.config import LOGGING_CONFIG

        uvicorn_log_config()
        self.assertNotIn("filters", LOGGING_CONFIG)
        self.assertNotIn("filters", LOGGING_CONFIG["handlers"]["access"])


if __name__ == "__main__":
    unittest.main()
