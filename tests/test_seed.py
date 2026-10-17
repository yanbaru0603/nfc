"""シードの再実行、素材生成、CLIのリセットを一時ディレクトリで確認。"""

import io
import os
import shutil
import sqlite3
import tempfile
import unittest
import wave
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from backend.config import PROJECT_ROOT, Settings
from backend.db import connect, init_db
from seed import DEV_TOKENS, _generate_wav, main, seed


class SeedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.settings = Settings(
            db_path=self.root / "instance/app.db",
            media_dir=self.root / "media",
            base_url="https://example.test",
            session_secret="test-secret",
            retry_ttl_seconds=600,
            cookie_secure=True,
        )
        self.cover_path = self.root / "frontend/public/covers/work-001.svg"
        self.cover_path.parent.mkdir(parents=True)
        shutil.copyfile(PROJECT_ROOT / "frontend/public/covers/work-001.svg", self.cover_path)
        self.root_patch = patch("seed.PROJECT_ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.now = datetime(2026, 10, 17, 1, 23, 45, tzinfo=UTC)
        self.conn = connect(self.settings.db_path)
        self.addCleanup(lambda: self.conn.close())
        init_db(self.conn)

    def seed_data(self) -> None:
        seed(self.conn, self.settings, now=self.now)

    def test_initial_data_and_development_entitlements(self) -> None:
        self.seed_data()
        for table, expected in (("works", 1), ("benefits", 1), ("benefit_files", 3),
                                ("accounts", 3), ("entitlements", 3), ("download_sessions", 0)):
            with self.subTest(table=table):
                self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], expected)
        entitlements = self.conn.execute("SELECT * FROM entitlements ORDER BY uid").fetchall()
        self.assertEqual([row["token"] for row in entitlements], list(DEV_TOKENS))
        self.assertEqual([row["uid"] for row in entitlements], ["DEV-0001", "DEV-0002", "DEV-0003"])
        for row in entitlements:
            self.assertEqual(row["status"], "unused")
            self.assertEqual(row["benefit_id"], "benefit-001")
            self.assertEqual(row["issued_at"], "2026-10-17T01:23:45Z")
            self.assertIsNone(row["account_id"])
            self.assertIsNone(row["claimed_at"])

    def test_seed_is_idempotent_and_preserves_existing_files(self) -> None:
        self.seed_data()
        tables = ("works", "benefits", "benefit_files", "accounts", "entitlements")
        before = {table: list(self.conn.execute(f"SELECT * FROM {table}")) for table in tables}
        paths = list(self.settings.media_dir.rglob("*.wav")) + [self.cover_path]
        modified = {path: path.stat().st_mtime_ns for path in paths}
        seed(self.conn, self.settings, now=datetime(2026, 10, 18, tzinfo=UTC))
        for table in tables:
            self.assertEqual(list(self.conn.execute(f"SELECT * FROM {table}")), before[table])
        self.assertEqual({path: path.stat().st_mtime_ns for path in paths}, modified)

    def test_seed_preserves_claimed_and_revoked_states(self) -> None:
        self.seed_data()
        self.conn.execute(
            "UPDATE entitlements SET status = 'account', account_id = 'demo-alice', "
            "claimed_at = '2026-10-17T02:00:00Z' WHERE token = ?", (DEV_TOKENS[0],)
        )
        self.conn.execute("UPDATE entitlements SET status = 'download' WHERE token = ?", (DEV_TOKENS[1],))
        self.conn.execute("UPDATE entitlements SET status = 'revoked' WHERE token = ?", (DEV_TOKENS[2],))
        self.seed_data()
        rows = self.conn.execute("SELECT * FROM entitlements ORDER BY uid").fetchall()
        self.assertEqual([row["status"] for row in rows], ["account", "download", "revoked"])
        self.assertEqual(rows[0]["account_id"], "demo-alice")
        self.assertEqual(rows[0]["claimed_at"], "2026-10-17T02:00:00Z")

    def test_seed_rejects_another_active_token_for_the_same_uid(self) -> None:
        self.seed_data()
        self.conn.execute("DELETE FROM entitlements WHERE token = ?", (DEV_TOKENS[0],))
        self.conn.execute("DELETE FROM accounts WHERE id = 'demo-carol'")
        for status in ("unused", "account", "download"):
            with self.subTest(status=status):
                self.conn.execute(
                    "INSERT INTO entitlements (token, benefit_id, uid, status, account_id, issued_at) "
                    "VALUES ('other-token', 'benefit-001', 'DEV-0001', ?, ?, '2026-10-17T02:00:00Z')",
                    (status, "demo-alice" if status == "account" else None),
                )
                before = list(self.conn.execute("SELECT * FROM entitlements ORDER BY token"))
                with self.assertRaisesRegex(sqlite3.IntegrityError, "entitlements.uid"):
                    self.seed_data()
                self.assertEqual(list(self.conn.execute("SELECT * FROM entitlements ORDER BY token")), before)
                self.assertIsNone(self.conn.execute("SELECT * FROM accounts WHERE id = 'demo-carol'").fetchone())
                self.conn.execute("DELETE FROM entitlements WHERE token = 'other-token'")

    def test_seed_rejects_conflicting_benefit_file_position(self) -> None:
        self.seed_data()
        self.conn.execute("UPDATE benefit_files SET id = 'other-file' WHERE id = 'file-001'")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "benefit_files.benefit_id, benefit_files.position"):
            self.seed_data()
        self.assertIsNone(self.conn.execute("SELECT * FROM benefit_files WHERE id = 'file-001'").fetchone())

    def test_seed_does_not_ignore_not_null_violation(self) -> None:
        with patch("seed.TRACKS", ((None, 261.63),)):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "NOT NULL"):
                self.seed_data()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM works").fetchone()[0], 0)

    def test_generated_wavs_are_readable_and_distinct(self) -> None:
        self.seed_data()
        frames: list[bytes] = []
        rows = self.conn.execute("SELECT * FROM benefit_files ORDER BY position").fetchall()
        self.assertEqual([row["position"] for row in rows], [1, 2, 3])
        self.assertEqual([row["title"] for row in rows], ["朝の光", "風の便り", "星の帰り道"])
        for row in rows:
            relative_path = Path(row["file_path"])
            self.assertFalse(relative_path.is_absolute())
            self.assertEqual(row["mime_type"], "audio/wav")
            with wave.open(str(self.settings.media_dir / relative_path), "rb") as audio:
                self.assertEqual(audio.getnchannels(), 1)
                self.assertEqual(audio.getsampwidth(), 2)
                duration = audio.getnframes() / audio.getframerate()
                self.assertGreaterEqual(duration, 5)
                self.assertLessEqual(duration, 8)
                data = audio.readframes(audio.getnframes())
                self.assertEqual(len(data), audio.getnframes() * 2)
                self.assertTrue(any(data))
                frames.append(data)
        self.assertEqual(len(set(frames)), 3)

    def test_interrupted_wav_generation_leaves_no_partial_file_and_can_retry(self) -> None:
        path = self.settings.media_dir / "benefit-001/01.wav"
        original_writeframes = wave.Wave_write.writeframes

        def interrupt_write(output: wave.Wave_write, frames: bytes | bytearray) -> None:
            original_writeframes(output, frames[:100])
            self.assertFalse(path.exists())
            self.assertEqual(len(list(path.parent.iterdir())), 1)
            raise KeyboardInterrupt

        with patch.object(wave.Wave_write, "writeframes", interrupt_write):
            with self.assertRaises(KeyboardInterrupt):
                _generate_wav(path, 261.63)
        self.assertFalse(path.exists())
        self.assertEqual(list(path.parent.iterdir()), [])
        _generate_wav(path, 261.63)
        self.assertEqual(list(path.parent.iterdir()), [path])
        with wave.open(str(path), "rb") as audio:
            self.assertEqual(len(audio.readframes(audio.getnframes())), audio.getnframes() * 2)

    def test_wav_is_complete_before_atomic_replacement(self) -> None:
        path = self.settings.media_dir / "benefit-001/01.wav"
        original_replace = os.replace

        def replace_complete(source: str, destination: Path) -> None:
            self.assertEqual(Path(source).parent, path.parent)
            self.assertEqual(destination, path)
            self.assertFalse(path.exists())
            with wave.open(source, "rb") as audio:
                self.assertEqual(audio.getnframes(), audio.getframerate() * 6)
                self.assertEqual(len(audio.readframes(audio.getnframes())), audio.getnframes() * 2)
            original_replace(source, destination)

        with patch("seed.os.replace", side_effect=replace_complete) as replace:
            _generate_wav(path, 261.63)
        replace.assert_called_once()
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_cover_is_svg_and_uses_public_url(self) -> None:
        before = self.cover_path.read_bytes()
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.seed_data()
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(self.cover_path.read_bytes(), before)
        cover = ElementTree.parse(self.cover_path)
        self.assertEqual(cover.getroot().tag, "{http://www.w3.org/2000/svg}svg")
        self.assertEqual(self.conn.execute("SELECT cover_path FROM works").fetchone()[0],
                         "/covers/work-001.svg")

    def test_missing_cover_warns_on_stderr_without_generating_a_file(self) -> None:
        shutil.rmtree(self.cover_path.parent)
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.seed_data()
        self.assertIn("警告", errors.getvalue())
        self.assertIn(str(self.cover_path), errors.getvalue())
        self.assertFalse(self.cover_path.parent.exists())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entitlements").fetchone()[0], 3)

    def test_cli_is_idempotent_and_prints_development_urls(self) -> None:
        with patch("seed.load_settings", return_value=self.settings):
            for _ in range(2):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main([]), 0)
                self.assertEqual(output.getvalue().splitlines(),
                                 [f"https://example.test/r/{token}" for token in DEV_TOKENS])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entitlements").fetchone()[0], 3)

    def test_cli_prints_non_unused_states_from_database(self) -> None:
        self.seed_data()
        for status, label in (("account", "受け取り済み"), ("download", "受け取り済み"),
                              ("issuing", "発行中"), ("revoked", "無効")):
            with self.subTest(status=status):
                self.conn.execute(
                    "UPDATE entitlements SET status = ?, account_id = ? WHERE token = ?",
                    (status, "demo-alice" if status == "account" else None, DEV_TOKENS[0]),
                )
                output = io.StringIO()
                with patch("seed.load_settings", return_value=self.settings), redirect_stdout(output):
                    self.assertEqual(main([]), 0)
                self.assertEqual(output.getvalue().splitlines(), [
                    f"https://example.test/r/{DEV_TOKENS[0]}"
                    f" （{label}: {status}。--reset で未使用に戻せます）",
                    *(f"https://example.test/r/{token}" for token in DEV_TOKENS[1:]),
                ])

    def test_cli_reset_help_mentions_stopping_server_and_issuer(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
            main(["--help"])
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("サーバーと発行CLIを止めてから実行してください", "".join(output.getvalue().split()))

    def test_cli_reset_removes_database_and_sidecars_and_restores_unused(self) -> None:
        self.seed_data()
        self.conn.execute(
            "UPDATE entitlements SET status = 'account', account_id = 'demo-alice' WHERE token = ?",
            (DEV_TOKENS[0],),
        )
        self.conn.execute("UPDATE entitlements SET status = 'download' WHERE token = ?", (DEV_TOKENS[1],))
        self.conn.execute(
            "INSERT INTO download_sessions VALUES ('session', ?, '2026-10-17T01:33:45Z')", (DEV_TOKENS[1],)
        )
        self.conn.close()
        for suffix in ("-wal", "-shm"):
            Path(f"{self.settings.db_path}{suffix}").write_bytes("古いファイル".encode("utf-8"))
        with patch("seed.load_settings", return_value=self.settings):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--reset"]), 0)
        self.conn = connect(self.settings.db_path)
        rows = self.conn.execute("SELECT * FROM entitlements ORDER BY uid").fetchall()
        self.assertEqual([row["status"] for row in rows], ["unused"] * 3)
        self.assertTrue(all(row["account_id"] is None and row["claimed_at"] is None for row in rows))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM download_sessions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
