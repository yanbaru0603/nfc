"""NFC読み取りCLIの動作と、タグを書き換えないことを確認。"""

import io
import tempfile
import unittest
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ndef

from issuer.nfc_writer import ReaderError, TagReadError
from backend.db import connect
from read_tag import main
from tests.test_issuer import SimulatedNtag215, UID, ntag215_memory


URL = "http://localhost:8000/r/test-read-token-0001"


class ReadTagTest(unittest.TestCase):
    def run_cli(self, tag: object | None, argv: list[str] | None = None,
                error: BaseException | None = None) -> tuple[int, str, str]:
        @contextmanager
        def reader_factory():
            yield "テスト用リーダー"

        output = io.StringIO()
        errors = io.StringIO()
        with patch("read_tag.wait_for_tag", return_value=tag, side_effect=error) as wait:
            with redirect_stdout(output), redirect_stderr(errors):
                code = main([] if argv is None else argv, reader_factory=reader_factory)
        if code != 130:
            timeout = int(argv[argv.index("--timeout") + 1]) if argv and "--timeout" in argv else 30
            wait.assert_called_once_with("テスト用リーダー", timeout=timeout)
        return code, output.getvalue(), errors.getvalue()

    def test_reads_url_and_uid_without_writing(self) -> None:
        message = b"".join(ndef.message_encoder([ndef.UriRecord(URL)]))
        memory = ntag215_memory(ndef_tlv=b"\x03" + bytes([len(message)]) + message + b"\xFE")
        before = bytes(memory)
        tag = SimulatedNtag215(memory)
        code, output, errors = self.run_cli(tag)
        self.assertEqual(code, 0)
        self.assertIn(f"UID: {UID}", output)
        self.assertIn(f"URL: {URL}", output)
        self.assertEqual(errors, "")
        self.assertEqual(tag.write_count, 0)
        self.assertEqual(bytes(memory), before)

    def test_timeout_can_be_changed(self) -> None:
        code, output, errors = self.run_cli(None, ["--timeout", "60"])
        self.assertEqual(code, 2)
        self.assertIn("60秒待機", output)
        self.assertIn("タグを検出できませんでした", errors)

    def test_empty_ndef_does_not_format_or_write(self) -> None:
        memory = ntag215_memory()
        before = bytes(memory)
        tag = SimulatedNtag215(memory)
        code, _, errors = self.run_cli(tag)
        self.assertEqual(code, 1)
        self.assertIn("URLを読み取れませんでした", errors)
        self.assertEqual(tag.write_count, 0)
        self.assertEqual(bytes(memory), before)

    def test_other_tag_is_rejected_without_writing(self) -> None:
        class OtherTag:
            product = "別のタグ"

        code, _, errors = self.run_cli(OtherTag())
        self.assertEqual(code, 1)
        self.assertIn("NTAG215ではありません", errors)

    def test_reader_and_tag_errors_are_reported(self) -> None:
        for error in (ReaderError("通信失敗"), TagReadError("読み取り失敗")):
            with self.subTest(error=type(error).__name__):
                code, _, errors = self.run_cli(None, error=error)
                self.assertEqual(code, 1)
                self.assertIn(str(error), errors)

    def test_open_reader_failure_is_reported(self) -> None:
        def reader_factory():
            raise ReaderError("RC-S380を開けません")

        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(main([], reader_factory=reader_factory), 1)
        self.assertIn("RC-S380を開けません", errors.getvalue())

    def test_interrupt_returns_130(self) -> None:
        code, output, _ = self.run_cli(None, error=KeyboardInterrupt())
        self.assertEqual(code, 130)
        self.assertIn("終了しました", output)

    def test_invalid_timeout_is_rejected(self) -> None:
        for value in ("0", "-1", "abc"):
            with self.subTest(value=value), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(["--timeout", value])
            self.assertEqual(error.exception.code, 2)

    def test_allow_repeat_registers_only_the_read_uid_even_before_issuing(self) -> None:
        memory = ntag215_memory()
        before = bytes(memory)
        tag = SimulatedNtag215(memory)
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "app.db"
            with patch("read_tag.load_settings", return_value=SimpleNamespace(db_path=db_path)):
                code, output, errors = self.run_cli(tag, ["--allow-repeat"])
            self.assertEqual(code, 0)
            self.assertIn("何度でも特典を受け取れる例外", output)
            self.assertIn("URLは未登録", output)
            self.assertEqual(errors, "")
            with closing(connect(db_path)) as conn:
                self.assertEqual([row["uid"] for row in conn.execute("SELECT uid FROM repeatable_cards")], [UID])
        self.assertEqual(tag.write_count, 0)
        self.assertEqual(bytes(memory), before)


if __name__ == "__main__":
    unittest.main()
