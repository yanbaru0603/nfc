"""設定の検証と秘密鍵の永続化を一時ディレクトリで確認。"""

import os
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from backend.config import PROJECT_ROOT, load_settings


class ConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root_patch = patch("backend.config.PROJECT_ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def test_project_root(self) -> None:
        self.assertEqual(PROJECT_ROOT, Path(__file__).resolve().parent.parent)
        self.assertTrue(PROJECT_ROOT.is_absolute())

    def test_defaults_and_frozen_settings(self) -> None:
        settings = load_settings({"SESSION_SECRET": "test-secret"})
        self.assertEqual(settings.db_path, self.root / "instance/app.db")
        self.assertEqual(settings.media_dir, self.root / "media")
        self.assertEqual(settings.base_url, "http://localhost:5173")
        self.assertEqual(settings.retry_ttl_seconds, 600)
        self.assertFalse(settings.cookie_secure)
        with self.assertRaises(FrozenInstanceError):
            settings.base_url = "https://example.test"

    def test_relative_paths_do_not_depend_on_working_directory(self) -> None:
        other_directory = self.root / "elsewhere"
        other_directory.mkdir()
        previous_directory = Path.cwd()
        try:
            os.chdir(other_directory)
            settings = load_settings({
                "DB_PATH": "data/../database/demo.db",
                "MEDIA_DIR": "audio",
                "SESSION_SECRET": "test-secret",
            })
        finally:
            os.chdir(previous_directory)
        self.assertEqual(settings.db_path, self.root / "database/demo.db")
        self.assertEqual(settings.media_dir, self.root / "audio")

    def test_absolute_paths_are_preserved(self) -> None:
        settings = load_settings({
            "DB_PATH": str(self.root / "absolute.db"),
            "MEDIA_DIR": str(self.root / "absolute-media"),
            "SESSION_SECRET": "test-secret",
        })
        self.assertEqual(settings.db_path, self.root / "absolute.db")
        self.assertEqual(settings.media_dir, self.root / "absolute-media")

    def test_os_environment_is_read_when_argument_is_omitted(self) -> None:
        with patch.dict(os.environ, {"SESSION_SECRET": "environment-secret"}, clear=True):
            self.assertEqual(load_settings().session_secret, "environment-secret")

    def test_explicit_environment_does_not_fall_back_to_os_environment(self) -> None:
        with patch.dict(os.environ, {"RETRY_TTL_SECONDS": "bad", "SESSION_SECRET": "other"}):
            settings = load_settings({"SESSION_SECRET": "test-secret"})
        self.assertEqual(settings.retry_ttl_seconds, 600)
        self.assertEqual(settings.session_secret, "test-secret")

    def test_cookie_secure_default_and_url_trailing_slashes(self) -> None:
        for url, expected in (("http://localhost:5173/", False),
                              ("https://example.test///", True)):
            with self.subTest(url=url):
                settings = load_settings({"BASE_URL": url, "SESSION_SECRET": "test-secret"})
                self.assertEqual(settings.base_url, url.rstrip("/"))
                self.assertEqual(settings.cookie_secure, expected)

    def test_base_url_accepts_http_and_https_with_a_host(self) -> None:
        for url in ("http://localhost:5173", "https://example.test", "http://127.0.0.1:8000",
                    "https://[::1]:8443", "HTTPS://example.test"):
            with self.subTest(url=url):
                settings = load_settings({"BASE_URL": url, "SESSION_SECRET": "test-secret"})
                self.assertEqual(settings.base_url, url)
                self.assertEqual(settings.cookie_secure, url.lower().startswith("https:"))

    def test_base_url_rejects_other_schemes_or_missing_hosts(self) -> None:
        for url in ("", "example.test", "//example.test", "ftp://example.test",
                    "file:///tmp/demo", "http://", "https:///receive", "https://:443",
                    "https://user@", "https://[invalid"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "BASE_URL.*ホスト名"):
                load_settings({"BASE_URL": url})
        self.assertFalse((self.root / "instance").exists())

    def test_cookie_secure_override(self) -> None:
        for value in ("1", "true", "YES", " on "):
            with self.subTest(value=value):
                self.assertTrue(load_settings({
                    "COOKIE_SECURE": value, "SESSION_SECRET": "test-secret",
                }).cookie_secure)
        for value in ("0", "false", "NO", " off "):
            with self.subTest(value=value):
                self.assertFalse(load_settings({
                    "BASE_URL": "https://example.test", "COOKIE_SECURE": value,
                    "SESSION_SECRET": "test-secret",
                }).cookie_secure)
        with self.assertRaisesRegex(ValueError, "COOKIE_SECURE"):
            load_settings({"COOKIE_SECURE": "invalid", "SESSION_SECRET": "test-secret"})

    def test_retry_ttl_validation(self) -> None:
        self.assertEqual(load_settings({
            "RETRY_TTL_SECONDS": "1", "SESSION_SECRET": "test-secret",
        }).retry_ttl_seconds, 1)
        for value in ("0", "-1", "1.5", "abc", ""):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "RETRY_TTL_SECONDS"):
                load_settings({"RETRY_TTL_SECONDS": value, "SESSION_SECRET": "test-secret"})

    def test_secret_is_persisted_with_private_permissions(self) -> None:
        first = load_settings({})
        second = load_settings({})
        secret_path = self.root / "instance/session_secret"
        self.assertEqual(first.session_secret, second.session_secret)
        self.assertEqual(secret_path.read_text(encoding="utf-8"), first.session_secret)
        self.assertIsNotNone(re.fullmatch(r"[A-Za-z0-9_-]{43}", first.session_secret))
        self.assertEqual(secret_path.stat().st_mode & 0o777, 0o600)

    def test_existing_secret_is_reused_and_permissions_are_corrected(self) -> None:
        secret_path = self.root / "instance/session_secret"
        secret_path.parent.mkdir()
        secret_path.write_text("saved-secret\n", encoding="utf-8")
        secret_path.chmod(0o644)
        self.assertEqual(load_settings({}).session_secret, "saved-secret")
        self.assertEqual(secret_path.stat().st_mode & 0o777, 0o600)

    def test_environment_secret_does_not_create_a_file(self) -> None:
        self.assertEqual(load_settings({"SESSION_SECRET": "test-secret"}).session_secret, "test-secret")
        self.assertFalse((self.root / "instance").exists())

    def test_empty_environment_secret_reuses_persisted_secret(self) -> None:
        first = load_settings({"SESSION_SECRET": ""})
        self.assertEqual(load_settings({}).session_secret, first.session_secret)
        self.assertEqual(load_settings({"SESSION_SECRET": ""}).session_secret, first.session_secret)
        self.assertEqual((self.root / "instance/session_secret").read_text(encoding="utf-8"),
                         first.session_secret)

    def test_empty_persisted_secret_is_rejected(self) -> None:
        secret_path = self.root / "instance/session_secret"
        secret_path.parent.mkdir()
        secret_path.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "空"):
            load_settings({})

    def test_concurrent_initialization_reuses_one_secret(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as executor:
            secrets = list(executor.map(lambda _: load_settings({}).session_secret, range(16)))
        self.assertEqual(len(set(secrets)), 1)
        self.assertEqual(list((self.root / "instance").iterdir()),
                         [self.root / "instance/session_secret"])


if __name__ == "__main__":
    unittest.main()
