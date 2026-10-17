"""発行処理・NFC機器操作・発行CLIを、偽のタグとリーダーで確認（実機には接続しない）。"""

import io
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import ndef
import nfc.clf
from nfc.clf.rcs380 import Chipset, StatusError
from nfc.tag.tt2 import INVALID_PAGE_ERROR, Type2Tag, Type2TagCommandError
from nfc.tag.tt2_nxp import NTAG213, NTAG215

import issue
from backend.config import Settings
from backend.db import connect, init_db
from backend.entitlements import (
    ActiveEntitlementExists,
    BenefitNotFound,
    claim_for_account,
    create_issuing,
    activate_issued,
    find_active_by_uid,
)
from issuer.issuing import (
    NTAG215_NDEF_CAPACITY,
    IssueError,
    IssueInterrupted,
    UrlTooLong,
    build_url,
    check_url_fits,
    issue_one,
)
from issuer.nfc_writer import (
    NfcTagWriter,
    NotNtag215,
    ReaderError,
    TagWriteError,
    TagWriteInterrupted,
    is_ntag215,
    open_reader,
    read_uri,
    tag_uid,
    wait_for_removal,
    wait_for_tag,
    write_uri,
)


NOW = datetime(2026, 10, 17, 1, 0, 0, tzinfo=UTC)
BASE_URL = "https://nfc.example.test"
UID = "04A1B2C3D4E580"


# ---- 偽のタグ ----

class FakeWriter:
    """issue_one に渡す偽の TagWriter。失敗や読み直しの値を差し替えられる。"""

    def __init__(self, uid: str = UID) -> None:
        self._uid = uid
        self.content: str | None = None
        self.writes: list[str] = []
        self.write_error: BaseException | None = None
        self.read_override: str | None = None
        self.use_read_override = False

    def uid(self) -> str:
        return self._uid

    def write_uri(self, url: str) -> None:
        self.writes.append(url)
        if self.write_error is not None:
            raise self.write_error
        self.content = url

    def read_uri(self) -> str | None:
        return self.read_override if self.use_read_override else self.content


def ntag215_memory(*, ndef_tlv: bytes = b"\x03\x00\xFE", cc_access: int = 0x00) -> bytearray:
    """NTAG215（135ページ）のメモリ。ページ3がCC、ページ4からユーザー領域。"""
    memory = bytearray(135 * 4)
    memory[0:9] = bytes.fromhex("04A1B2" "88" "C3D4E580" "00")
    memory[12:16] = bytes([0xE1, 0x10, 0x3E, cc_access])
    memory[16:16 + len(ndef_tlv)] = ndef_tlv
    return memory


class SimulatedNtag215(NTAG215):
    """nfcpyのNTAG215クラスのまま、READ・WRITEコマンドだけをメモリ上で模擬する。"""

    def __init__(self, memory: bytearray, uid: str = UID, *, fail_after_writes: int | None = None) -> None:
        super().__init__(None, nfc.clf.RemoteTarget("106A", sdd_res=bytearray.fromhex(uid)))
        self.memory = memory
        self.write_count = 0
        self.fail_after_writes = fail_after_writes
        self.read_count = 0

    def read(self, page: int) -> bytearray:
        self.read_count += 1
        data = self.memory[page * 4:page * 4 + 16]
        return bytearray(data + bytes(16 - len(data)))

    def write(self, page: int, data: bytes) -> bool:
        if self.fail_after_writes is not None and self.write_count >= self.fail_after_writes:
            raise Type2TagCommandError(INVALID_PAGE_ERROR)
        self.write_count += 1
        self.memory[page * 4:page * 4 + 4] = data
        return True


class InterruptedNtag215(SimulatedNtag215):
    """after_writes 回のWRITEの後、次のREAD・WRITEでCtrl+Cを起こす。"""

    def __init__(self, memory: bytearray, *, after_writes: int) -> None:
        super().__init__(memory)
        self.after_writes = after_writes

    def read(self, page: int) -> bytearray:
        if self.write_count >= self.after_writes:
            raise KeyboardInterrupt()
        return super().read(page)

    def write(self, page: int, data: bytes) -> bool:
        if self.write_count >= self.after_writes:
            raise KeyboardInterrupt()
        return super().write(page, data)


def status_error_chipset() -> Chipset:
    """RC-S380のチップセットのうち、コマンドの送受信だけを差し替えたもの。

    USBには接続せず、すべてのコマンドにステータス 03（RFCA_ERROR）を返す。
    """
    chipset = object.__new__(Chipset)
    chipset.send_command = lambda *args: bytearray([3])  # type: ignore[method-assign]
    return chipset


class StatusErrorReader:
    """sense のたびに、RC-S380のドライバーと同じく in_set_rf でステータスを確認する偽のリーダー。"""

    def __init__(self) -> None:
        self.chipset = status_error_chipset()

    def sense(self, target: nfc.clf.RemoteTarget, **options: object) -> None:
        self.chipset.in_set_rf(target.brty)

    def __str__(self) -> str:
        return "ステータスエラーの偽のリーダー"


def stored_message(memory: bytearray) -> bytes:
    """メモリのNDEFメッセージTLV（オフセット16、1バイト長）の値。"""
    assert memory[16] == 0x03
    return bytes(memory[18:18 + memory[17]])


class DbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db_path = Path(self.temporary.name).resolve() / "app.db"
        self.conn = connect(self.db_path)
        self.addCleanup(self.conn.close)
        init_db(self.conn)
        self.conn.execute("INSERT INTO works VALUES ('work-1', '作品1', '/covers/1.svg')")
        self.conn.execute("INSERT INTO benefits VALUES ('benefit-1', 'work-1', '特典1', '')")
        self.conn.execute("INSERT INTO accounts VALUES ('account-1', 'デモ')")

    def statuses(self, uid: str = UID) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT token, status FROM entitlements WHERE uid = ? ORDER BY rowid", (uid,),
        ).fetchall()
        return [(row["token"], row["status"]) for row in rows]

    def issue_existing(self, uid: str = UID) -> str:
        token = create_issuing(self.conn, benefit_id="benefit-1", uid=uid, now=NOW)
        activate_issued(self.conn, token)
        return token


# ---- issue_one ----

class IssueOneTest(DbTestCase):
    def test_issue_writes_url_and_marks_unused(self) -> None:
        writer = FakeWriter()
        result = issue_one(self.conn, writer, benefit_id="benefit-1", base_url=BASE_URL, now=NOW)
        self.assertEqual(result.url, f"{BASE_URL}/r/{result.token}")
        self.assertEqual(result.uid, UID)
        self.assertIsNone(result.revoked)
        self.assertEqual(writer.writes, [result.url])
        self.assertEqual(self.statuses(), [(result.token, "unused")])
        row = self.conn.execute("SELECT * FROM entitlements").fetchone()
        self.assertEqual(row["benefit_id"], "benefit-1")
        self.assertEqual(row["issued_at"], "2026-10-17T01:00:00Z")

    def test_active_entitlement_stops_before_writing(self) -> None:
        existing = self.issue_existing()
        writer = FakeWriter()
        with self.assertRaises(ActiveEntitlementExists) as caught:
            issue_one(self.conn, writer, benefit_id="benefit-1", base_url=BASE_URL, now=NOW)
        self.assertEqual(caught.exception.existing.token, existing)
        self.assertEqual(writer.writes, [])
        self.assertEqual(self.statuses(), [(existing, "unused")])

    def test_reissue_revokes_previous_and_keeps_account(self) -> None:
        existing = self.issue_existing()
        claim_for_account(self.conn, existing, "account-1", now=NOW)
        result = issue_one(
            self.conn, FakeWriter(), benefit_id="benefit-1", base_url=BASE_URL, now=NOW, reissue=True,
        )
        assert result.revoked is not None
        self.assertEqual(result.revoked.token, existing)
        self.assertEqual(result.revoked.status, "account")
        self.assertEqual(self.statuses(), [(existing, "revoked"), (result.token, "unused")])
        # 受け取った人のライブラリから使えるよう、account_id は残る。
        row = self.conn.execute(
            "SELECT account_id FROM entitlements WHERE token = ?", (existing,),
        ).fetchone()
        self.assertEqual(row["account_id"], "account-1")

    def test_reissue_without_existing_is_new_issue(self) -> None:
        result = issue_one(
            self.conn, FakeWriter(), benefit_id="benefit-1", base_url=BASE_URL, now=NOW, reissue=True,
        )
        self.assertIsNone(result.revoked)
        self.assertEqual(self.statuses(), [(result.token, "unused")])

    def test_write_failure_discards_issuing(self) -> None:
        writer = FakeWriter()
        writer.write_error = TagWriteError("通信エラー", tag_modified=True)
        with self.assertRaises(IssueError) as caught:
            issue_one(self.conn, writer, benefit_id="benefit-1", base_url=BASE_URL, now=NOW)
        self.assertTrue(caught.exception.failure.tag_may_be_corrupted)
        self.assertEqual(caught.exception.failure.uid, UID)
        self.assertIsNone(caught.exception.failure.previous)
        self.assertEqual(self.statuses(), [])

    def test_write_failure_before_modifying_tag(self) -> None:
        writer = FakeWriter()
        writer.write_error = TagWriteError("書き込み禁止", tag_modified=False)
        with self.assertRaises(IssueError) as caught:
            issue_one(self.conn, writer, benefit_id="benefit-1", base_url=BASE_URL, now=NOW)
        self.assertFalse(caught.exception.failure.tag_may_be_corrupted)
        self.assertEqual(self.statuses(), [])

    def test_verify_mismatch_discards_issuing(self) -> None:
        for read_value in (None, f"{BASE_URL}/r/other"):
            with self.subTest(read_value=read_value):
                writer = FakeWriter()
                writer.use_read_override = True
                writer.read_override = read_value
                with self.assertRaises(IssueError) as caught:
                    issue_one(self.conn, writer, benefit_id="benefit-1", base_url=BASE_URL, now=NOW)
                self.assertTrue(caught.exception.failure.tag_may_be_corrupted)
                self.assertEqual(self.statuses(), [])

    def test_failed_reissue_keeps_previous_active(self) -> None:
        existing = self.issue_existing()
        claim_for_account(self.conn, existing, "account-1", now=NOW)
        for case in ("write", "verify"):
            with self.subTest(case=case):
                writer = FakeWriter()
                if case == "write":
                    writer.write_error = TagWriteError("通信エラー", tag_modified=True)
                else:
                    writer.use_read_override = True
                with self.assertRaises(IssueError) as caught:
                    issue_one(self.conn, writer, benefit_id="benefit-1", base_url=BASE_URL,
                              now=NOW, reissue=True)
                previous = caught.exception.failure.previous
                assert previous is not None
                self.assertEqual(previous.token, existing)
                self.assertEqual(self.statuses(), [(existing, "account")])
                active = find_active_by_uid(self.conn, UID)
                assert active is not None
                self.assertEqual(active.token, existing)

    def test_interrupt_before_write_reports_tag_unchanged(self) -> None:
        existing = self.issue_existing()
        writer = FakeWriter()
        writer.write_error = TagWriteInterrupted(tag_modified=False)
        with self.assertRaises(IssueInterrupted) as caught:
            issue_one(self.conn, writer, benefit_id="benefit-1", base_url=BASE_URL, now=NOW,
                      reissue=True)
        failure = caught.exception.failure
        self.assertIsInstance(caught.exception, KeyboardInterrupt)
        self.assertEqual(failure.uid, UID)
        self.assertFalse(failure.tag_may_be_corrupted)
        self.assertTrue(failure.discarded)
        assert failure.previous is not None
        self.assertEqual(failure.previous.token, existing)
        self.assertEqual(self.statuses(), [(existing, "unused")])

    def test_interrupt_after_write_started_reports_tag_modified(self) -> None:
        cases = {
            "機器の層で書き込み開始後": TagWriteInterrupted(tag_modified=True),
            "機器の層の判定なし": KeyboardInterrupt(),
        }
        for name, interrupt in cases.items():
            with self.subTest(name):
                writer = FakeWriter()
                writer.write_error = interrupt
                with self.assertRaises(IssueInterrupted) as caught:
                    issue_one(self.conn, writer, benefit_id="benefit-1", base_url=BASE_URL, now=NOW)
                self.assertTrue(caught.exception.failure.tag_may_be_corrupted)
                self.assertTrue(caught.exception.failure.discarded)
                self.assertIsNone(caught.exception.failure.previous)
                self.assertEqual(self.statuses(), [])

    def test_interrupt_during_verify_reports_tag_modified(self) -> None:
        class InterruptOnRead(FakeWriter):
            def read_uri(self) -> str | None:
                raise KeyboardInterrupt()

        with self.assertRaises(IssueInterrupted) as caught:
            issue_one(self.conn, InterruptOnRead(), benefit_id="benefit-1", base_url=BASE_URL, now=NOW)
        # 書き込みは終わっているが、取得権を取り消したためタグのURLは無効になっている。
        self.assertTrue(caught.exception.failure.tag_may_be_corrupted)
        self.assertEqual(self.statuses(), [])

    def test_discard_failure_after_write_failure_keeps_cause(self) -> None:
        existing = self.issue_existing()
        writer = FakeWriter()
        write_error = TagWriteError("通信エラー", tag_modified=True)
        writer.write_error = write_error
        locked = sqlite3.OperationalError("database is locked")
        with patch("issuer.issuing.discard_issuing", side_effect=locked):
            with self.assertRaises(IssueError) as caught:
                issue_one(self.conn, writer, benefit_id="benefit-1", base_url=BASE_URL, now=NOW,
                          reissue=True)
        failure = caught.exception.failure
        self.assertIs(caught.exception.__cause__, write_error)
        self.assertFalse(failure.discarded)
        self.assertIs(failure.discard_error, locked)
        self.assertTrue(failure.tag_may_be_corrupted)
        assert failure.previous is not None
        self.assertEqual(failure.previous.token, existing)
        self.assertEqual([status for _, status in self.statuses()], ["unused", "issuing"])
        self.assertFalse(self.conn.in_transaction)

    def test_discard_failure_after_activation_failure_keeps_cause(self) -> None:
        activation_error = sqlite3.OperationalError("database is locked (activate)")
        locked = sqlite3.OperationalError("database is locked (discard)")
        with (
            patch("issuer.issuing.activate_issued", side_effect=activation_error),
            patch("issuer.issuing.discard_issuing", side_effect=locked),
            self.assertRaises(IssueError) as caught,
        ):
            issue_one(self.conn, FakeWriter(), benefit_id="benefit-1", base_url=BASE_URL, now=NOW)
        failure = caught.exception.failure
        self.assertIs(caught.exception.__cause__, activation_error)
        self.assertFalse(failure.discarded)
        self.assertIs(failure.discard_error, locked)
        self.assertTrue(failure.tag_may_be_corrupted)
        self.assertEqual([status for _, status in self.statuses()], ["issuing"])

    def test_activation_failure_discards_issuing(self) -> None:
        activation_error = sqlite3.OperationalError("database is locked")
        with (
            patch("issuer.issuing.activate_issued", side_effect=activation_error),
            self.assertRaises(IssueError) as caught,
        ):
            issue_one(self.conn, FakeWriter(), benefit_id="benefit-1", base_url=BASE_URL, now=NOW)
        self.assertTrue(caught.exception.failure.discarded)
        self.assertTrue(caught.exception.failure.tag_may_be_corrupted)
        self.assertEqual(self.statuses(), [])

    def test_discard_with_real_lock(self) -> None:
        # 別の接続が書き込みのロックを持ったまま、待ち時間切れになる場合。
        self.conn.execute("PRAGMA busy_timeout = 0")
        locker = connect(self.db_path)
        self.addCleanup(locker.close)

        class LockingWriter(FakeWriter):
            def write_uri(self, url: str) -> None:
                locker.execute("BEGIN IMMEDIATE")
                raise TagWriteError("通信エラー", tag_modified=True)

        with self.assertRaises(IssueError) as caught:
            issue_one(self.conn, LockingWriter(), benefit_id="benefit-1", base_url=BASE_URL, now=NOW)
        locker.execute("ROLLBACK")
        failure = caught.exception.failure
        self.assertIsInstance(caught.exception.__cause__, TagWriteError)
        self.assertFalse(failure.discarded)
        self.assertIsInstance(failure.discard_error, sqlite3.OperationalError)
        self.assertEqual([status for _, status in self.statuses()], ["issuing"])

    def test_unknown_benefit(self) -> None:
        writer = FakeWriter()
        with self.assertRaises(BenefitNotFound):
            issue_one(self.conn, writer, benefit_id="missing", base_url=BASE_URL, now=NOW)
        self.assertEqual(writer.writes, [])
        self.assertEqual(self.statuses(), [])

    def test_too_long_url_is_not_written(self) -> None:
        writer = FakeWriter()
        with self.assertRaises(IssueError) as caught:
            issue_one(self.conn, writer, benefit_id="benefit-1",
                      base_url="https://example.test/" + "a" * 500, now=NOW)
        self.assertIsInstance(caught.exception.__cause__, UrlTooLong)
        self.assertFalse(caught.exception.failure.tag_may_be_corrupted)
        self.assertEqual(writer.writes, [])
        self.assertEqual(self.statuses(), [])


class UrlTest(unittest.TestCase):
    def test_build_url(self) -> None:
        self.assertEqual(build_url("https://h.example", "abc"), "https://h.example/r/abc")
        self.assertEqual(build_url("https://h.example/", "abc"), "https://h.example/r/abc")

    def test_check_url_fits(self) -> None:
        # 実際の長さ（22文字のトークン）は十分に収まる。
        check_url_fits(build_url("https://nfc-demo.example.com", "A" * 22))
        with self.assertRaises(UrlTooLong):
            check_url_fits("https://example.test/" + "a" * NTAG215_NDEF_CAPACITY)


# ---- nfc_writer（nfcpyのNTAG215クラスをメモリで模擬） ----

class NfcWriterTest(unittest.TestCase):
    URL = f"{BASE_URL}/r/AbCdEfGhIjKlMnOpQrStUv"

    def test_tag_uid_format(self) -> None:
        tag = SimulatedNtag215(ntag215_memory(), "04a1b2c3d4e580")
        self.assertEqual(tag_uid(tag), "04A1B2C3D4E580")

    def test_is_ntag215(self) -> None:
        target = nfc.clf.RemoteTarget("106A", sdd_res=bytearray.fromhex(UID))
        self.assertTrue(is_ntag215(NTAG215(None, target)))
        self.assertFalse(is_ntag215(NTAG213(None, target)))
        self.assertFalse(is_ntag215(Type2Tag(None, target)))

        class ProductOnly:
            product = "NXP NTAG215"
        self.assertTrue(is_ntag215(ProductOnly()))

    def test_nfc_tag_writer_rejects_other_tags(self) -> None:
        target = nfc.clf.RemoteTarget("106A", sdd_res=bytearray.fromhex(UID))
        with self.assertRaises(NotNtag215):
            NfcTagWriter(NTAG213(None, target))

    def test_write_and_read_uri(self) -> None:
        memory = ntag215_memory()
        tag = SimulatedNtag215(memory)
        write_uri(tag, self.URL)
        self.assertEqual(stored_message(memory), b"".join(ndef.message_encoder([ndef.UriRecord(self.URL)])))
        self.assertEqual(read_uri(SimulatedNtag215(memory)), self.URL)
        self.assertEqual(read_uri(tag), self.URL)

    def test_write_replaces_existing_records(self) -> None:
        memory = ntag215_memory()
        write_uri(SimulatedNtag215(memory), "https://old.example/r/old")
        tag = SimulatedNtag215(memory)
        write_uri(tag, self.URL)
        self.assertEqual(read_uri(tag), self.URL)

    def test_formats_tag_without_ndef(self) -> None:
        # CCはあるがNDEFメッセージのTLVがない（tag.ndef が None）。
        memory = ntag215_memory(ndef_tlv=b"\xFE")
        tag = SimulatedNtag215(memory)
        self.assertIsNone(tag.ndef)
        with patch.object(tag, "format", wraps=tag.format) as format_spy:
            write_uri(tag, self.URL)
        format_spy.assert_called_once()
        self.assertEqual(read_uri(SimulatedNtag215(memory)), self.URL)

    def test_existing_ndef_is_not_formatted(self) -> None:
        tag = SimulatedNtag215(ntag215_memory())
        with patch.object(tag, "format") as format_spy:
            write_uri(tag, self.URL)
        format_spy.assert_not_called()

    def test_format_failure(self) -> None:
        # CCがない（NDEF用でなく、nfcpyでは初期化できない）タグ。
        memory = ntag215_memory()
        memory[12:16] = b"\0\0\0\0"
        with self.assertRaises(TagWriteError) as caught:
            write_uri(SimulatedNtag215(memory), self.URL)
        self.assertTrue(caught.exception.tag_modified)

    def test_read_only_tag(self) -> None:
        memory = ntag215_memory(cc_access=0x0F)
        tag = SimulatedNtag215(memory)
        with self.assertRaises(TagWriteError) as caught:
            write_uri(tag, self.URL)
        self.assertFalse(caught.exception.tag_modified)
        self.assertEqual(tag.write_count, 0)

    def test_write_command_failure(self) -> None:
        tag = SimulatedNtag215(ntag215_memory(), fail_after_writes=1)
        with self.assertRaises(TagWriteError) as caught:
            write_uri(tag, self.URL)
        self.assertTrue(caught.exception.tag_modified)

    def test_read_uri_reads_tag_not_cache(self) -> None:
        memory = ntag215_memory()
        tag = SimulatedNtag215(memory)
        write_uri(tag, self.URL)
        # 書き込み後にタグの内容が変わっても、キャッシュではなく実際の内容を返す。
        other = "https://other.example/r/x"
        write_uri(SimulatedNtag215(memory), other)
        reads = tag.read_count
        self.assertEqual(read_uri(tag), other)
        self.assertGreater(tag.read_count, reads)
        # NDEFが消えた場合はNone。
        memory[16:19] = b"\xFE\x00\x00"
        self.assertIsNone(read_uri(tag))

    def test_read_uri_requires_single_uri_record(self) -> None:
        memory = ntag215_memory()
        tag = SimulatedNtag215(memory)
        write_uri(tag, self.URL)
        tag.ndef.records = [ndef.TextRecord("hello")]
        self.assertIsNone(read_uri(tag))
        tag.ndef.records = [ndef.UriRecord(self.URL), ndef.UriRecord(self.URL)]
        self.assertIsNone(read_uri(tag))


class WriteInterruptTest(unittest.TestCase):
    URL = f"{BASE_URL}/r/AbCdEfGhIjKlMnOpQrStUv"

    def test_interrupt_before_writing(self) -> None:
        memory = ntag215_memory()
        before = bytes(memory)
        with self.assertRaises(TagWriteInterrupted) as caught:
            write_uri(InterruptedNtag215(memory, after_writes=0), self.URL)
        self.assertFalse(caught.exception.tag_modified)
        self.assertEqual(bytes(memory), before)

    def test_interrupt_after_first_page(self) -> None:
        memory = ntag215_memory()
        write_uri(SimulatedNtag215(memory), "https://old.example/r/old")
        with self.assertRaises(TagWriteInterrupted) as caught:
            write_uri(InterruptedNtag215(memory, after_writes=1), self.URL)
        self.assertTrue(caught.exception.tag_modified)
        # nfcpyは最初にNDEFの長さを0にするため、途中で止まると空のNDEFが残る。
        self.assertIsNone(read_uri(SimulatedNtag215(memory)))


class DriverErrorTest(unittest.TestCase):
    """RC-S380のドライバー固有の例外（ステータス応答）を ReaderError に変換すること。"""

    def setUp(self) -> None:
        # libusbを読み込まず、USB機器にも触れない。
        for target in ("issuer.nfc_writer.usb1.loadLibrary", "issuer.nfc_writer.ctypes.CDLL"):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def status_error(self) -> StatusError:
        try:
            status_error_chipset().switch_rf("off")
        except StatusError as error:
            return error
        raise AssertionError("StatusError にならない")

    def test_open_reader(self) -> None:
        with patch("issuer.nfc_writer.nfc.ContactlessFrontend", side_effect=self.status_error()):
            with self.assertRaises(ReaderError) as caught:
                with open_reader():
                    self.fail("開けないリーダーで with の中に入った")
        self.assertIn("RC-S380を開けません", str(caught.exception))
        self.assertIn("RFCA_ERROR", str(caught.exception))

    def test_close_reader(self) -> None:
        class ClosingReader:
            closed = 0

            def close(inner) -> None:
                inner.closed += 1
                status_error_chipset().switch_rf("off")

        reader = ClosingReader()
        with patch("issuer.nfc_writer.nfc.ContactlessFrontend", return_value=reader):
            with self.assertRaises(ReaderError) as caught:
                with open_reader():
                    pass
            self.assertIn("閉じられませんでした", str(caught.exception))
            # with の中の例外は、閉じるときの失敗より優先する。
            with self.assertRaises(KeyboardInterrupt):
                with open_reader():
                    raise KeyboardInterrupt()
        self.assertEqual(reader.closed, 2)

    def test_wait_for_tag(self) -> None:
        with self.assertRaises(ReaderError) as caught:
            wait_for_tag(StatusErrorReader(), timeout=1, interval=0)
        self.assertIn("RFCA_ERROR", str(caught.exception))

    def test_wait_for_removal(self) -> None:
        tag = SimulatedNtag215(ntag215_memory())
        with self.assertRaises(ReaderError):
            wait_for_removal(StatusErrorReader(), tag, timeout=1, interval=0)

    def test_write_and_read(self) -> None:
        class StatusErrorTag(SimulatedNtag215):
            def write(self, page: int, data: bytes) -> bool:
                status_error_chipset().in_set_rf("106A")
                return True

        memory = ntag215_memory()
        with self.assertRaises(ReaderError) as caught:
            write_uri(StatusErrorTag(memory), BASE_URL + "/r/x")
        self.assertTrue(caught.exception.tag_modified)

        class StatusErrorReadTag(SimulatedNtag215):
            def read(self, page: int) -> bytearray:
                status_error_chipset().in_set_rf("106A")
                return bytearray(16)

        with self.assertRaises(ReaderError):
            read_uri(StatusErrorReadTag(memory))


class FakeReader:
    """sense の戻り値を順に返す偽のリーダー。"""

    def __init__(self, results: list[object | None]) -> None:
        self.results = list(results)
        self.targets: list[nfc.clf.RemoteTarget] = []

    def sense(self, target: nfc.clf.RemoteTarget, **options: object) -> object | None:
        self.targets.append(target)
        return self.results.pop(0) if self.results else None

    def __str__(self) -> str:
        return "偽のリーダー"


class WaitTest(unittest.TestCase):
    def test_wait_for_tag_returns_activated_tag(self) -> None:
        detected = object()
        tag = SimulatedNtag215(ntag215_memory())
        reader = FakeReader([None, detected])
        with patch("issuer.nfc_writer.nfc.tag.activate", return_value=tag) as activate:
            self.assertIs(wait_for_tag(reader, timeout=5, interval=0), tag)
        activate.assert_called_once_with(reader, detected)
        self.assertEqual(reader.targets[0].brty, "106A")

    def test_wait_for_tag_timeout(self) -> None:
        self.assertIsNone(wait_for_tag(FakeReader([]), timeout=0.05, interval=0.01))

    def test_wait_for_removal(self) -> None:
        tag = SimulatedNtag215(ntag215_memory())
        present = object()
        # 一時的に見つからなくても、続けて見つからなくなるまでは離れたとみなさない。
        reader = FakeReader([present, None, present, None, None, None])
        self.assertTrue(wait_for_removal(reader, tag, timeout=5, interval=0))
        self.assertEqual(len(reader.targets), 6)
        self.assertEqual(bytes(reader.targets[0].sel_req), bytes.fromhex(UID))

    def test_wait_for_removal_timeout(self) -> None:
        tag = SimulatedNtag215(ntag215_memory())
        reader = FakeReader([object()] * 1000)
        self.assertFalse(wait_for_removal(reader, tag, timeout=0.05, interval=0.01))


# ---- issue.py ----

class IssueCliTest(DbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = Settings(
            db_path=self.db_path,
            media_dir=Path(self.temporary.name).resolve() / "media",
            base_url=BASE_URL,
            session_secret="test-secret",
            retry_ttl_seconds=600,
            cookie_secure=True,
        )
        settings_patch = patch("issue.load_settings", return_value=self.settings)
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        self.reader = FakeReader([])
        self.reader_opened = 0

    @contextmanager
    def reader_factory(self) -> Iterator[FakeReader]:
        self.reader_opened += 1
        yield self.reader

    def run_cli(self, argv: list[str], tags: list[object | None]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("issue.wait_for_tag", side_effect=tags) as self.wait_for_tag,
            patch("issue.wait_for_removal", return_value=True) as self.wait_for_removal,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = issue.main(argv, reader_factory=self.reader_factory)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_parse_arguments(self) -> None:
        args = issue.build_parser().parse_args(["--benefit", "benefit-1"])
        self.assertEqual((args.benefit, args.count, args.reissue, args.timeout), ("benefit-1", 1, False, 30))
        args = issue.build_parser().parse_args(
            ["--benefit", "b", "--count", "3", "--reissue", "--timeout", "5"])
        self.assertEqual((args.count, args.reissue, args.timeout), (3, True, 5))

    def test_invalid_arguments(self) -> None:
        for argv in ([], ["--benefit", "b", "--count", "0"], ["--benefit", "b", "--timeout", "-1"],
                     ["--benefit", "b", "--count", "x"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    issue.main(argv, reader_factory=self.reader_factory)
                self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.reader_opened, 0)

    def test_help(self) -> None:
        with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as caught:
            issue.main(["--help"], reader_factory=self.reader_factory)
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--reissue", output.getvalue())

    def test_issue_one_tag(self) -> None:
        memory = ntag215_memory()
        code, out, _ = self.run_cli(["--benefit", "benefit-1"], [SimulatedNtag215(memory)])
        self.assertEqual(code, 0)
        [(token, status)] = self.statuses()
        self.assertEqual(status, "unused")
        url = f"{BASE_URL}/r/{token}"
        self.assertIn(f"UID: {UID}", out)
        self.assertIn(f"URL: {url}", out)
        self.assertEqual(read_uri(SimulatedNtag215(memory)), url)
        self.wait_for_removal.assert_not_called()

    def test_creates_db_when_missing(self) -> None:
        self.settings = Settings(**{**self.settings.__dict__, "db_path": self.db_path.parent / "new/app.db"})
        with patch("issue.load_settings", return_value=self.settings):
            code, _, err = self.run_cli(["--benefit", "benefit-1"], [])
        # 新しいDBには特典がないため、タグを待つ前にエラーになる。
        self.assertEqual(code, 1)
        self.assertIn("特典が見つかりません", err)
        self.assertTrue(self.settings.db_path.exists())
        self.assertEqual(self.reader_opened, 0)

    def test_unknown_benefit_fails_before_waiting(self) -> None:
        code, _, err = self.run_cli(["--benefit", "missing"], [])
        self.assertEqual(code, 1)
        self.assertIn("特典が見つかりません: missing", err)
        self.assertEqual(self.reader_opened, 0)
        self.wait_for_tag.assert_not_called()

    def test_no_tag_timeout(self) -> None:
        code, _, err = self.run_cli(["--benefit", "benefit-1", "--timeout", "1"], [None])
        self.assertEqual(code, 2)
        self.assertIn("タグを検出できませんでした", err)
        self.assertEqual(self.wait_for_tag.call_args.kwargs["timeout"], 1)

    def test_not_ntag215(self) -> None:
        target = nfc.clf.RemoteTarget("106A", sdd_res=bytearray.fromhex(UID))
        code, _, err = self.run_cli(["--benefit", "benefit-1"], [NTAG213(None, target)])
        self.assertEqual(code, 1)
        self.assertIn("NTAG215ではありません", err)
        self.assertEqual(self.statuses(), [])

    def test_active_entitlement_warning_and_reissue(self) -> None:
        existing = self.issue_existing()
        memory = ntag215_memory()
        code, _, err = self.run_cli(["--benefit", "benefit-1"], [SimulatedNtag215(memory)])
        self.assertEqual(code, 1)
        self.assertIn("警告", err)
        self.assertIn("未使用（unused）", err)
        self.assertIn("--reissue", err)
        self.assertEqual(stored_message(memory), b"")

        code, out, _ = self.run_cli(["--benefit", "benefit-1", "--reissue"], [SimulatedNtag215(memory)])
        self.assertEqual(code, 0)
        self.assertIn("無効にした既存の取得権", out)
        statuses = self.statuses()
        self.assertEqual(statuses[0], (existing, "revoked"))
        self.assertEqual(statuses[1][1], "unused")

    def test_write_failure_message(self) -> None:
        existing = self.issue_existing()
        tag = SimulatedNtag215(ntag215_memory(), fail_after_writes=1)
        code, _, err = self.run_cli(["--benefit", "benefit-1", "--reissue"], [tag])
        self.assertEqual(code, 1)
        self.assertIn("発行中の取得権は取り消しました", err)
        self.assertIn("既存の取得権は有効なままです", err)
        self.assertIn("--reissue を付けて発行し直してください", err)
        self.assertEqual(self.statuses(), [(existing, "unused")])

    def test_interrupt_before_writing_tag(self) -> None:
        existing = self.issue_existing()
        memory = ntag215_memory()
        code, _, err = self.run_cli(["--benefit", "benefit-1", "--reissue"],
                                    [InterruptedNtag215(memory, after_writes=0)])
        self.assertEqual(code, 130)
        self.assertIn(f"UID {UID} の発行を中断しました", err)
        self.assertIn("発行中の取得権は取り消しました", err)
        self.assertIn("既存の取得権は有効なままです: 未使用（unused）", err)
        self.assertIn("タグの内容は書き換えていません", err)
        self.assertNotIn("--reissue", err)
        self.assertEqual(self.statuses(), [(existing, "unused")])

    def test_interrupt_after_writing_started(self) -> None:
        existing = self.issue_existing()
        memory = ntag215_memory()
        write_uri(SimulatedNtag215(memory), "https://old.example/r/old")
        code, _, err = self.run_cli(["--benefit", "benefit-1", "--reissue"],
                                    [InterruptedNtag215(memory, after_writes=1)])
        self.assertEqual(code, 130)
        self.assertIn(f"UID {UID} の発行を中断しました", err)
        self.assertIn("発行中の取得権は取り消しました", err)
        self.assertIn("既存の取得権は有効なままです", err)
        self.assertIn("--reissue を付けて発行し直してください", err)
        self.assertEqual(self.statuses(), [(existing, "unused")])
        self.assertIsNone(read_uri(SimulatedNtag215(memory)))

    def test_discard_failure_message(self) -> None:
        existing = self.issue_existing()
        tag = SimulatedNtag215(ntag215_memory(), fail_after_writes=1)
        with patch("issuer.issuing.discard_issuing",
                   side_effect=sqlite3.OperationalError("database is locked")):
            code, _, err = self.run_cli(["--benefit", "benefit-1", "--reissue"], [tag])
        self.assertEqual(code, 1)
        self.assertIn("タグへの書き込み・照合に失敗しました", err)
        self.assertIn("発行中の取得権を削除できませんでした", err)
        self.assertIn("database is locked", err)
        self.assertIn("発行中（issuing）のまま残ります", err)
        self.assertIn("受け取りには使えず", err)
        self.assertNotIn("取り消しました", err)
        self.assertIn("既存の取得権は有効なままです", err)
        self.assertIn("--reissue を付けて発行し直してください", err)
        self.assertEqual([status for _, status in self.statuses()], ["unused", "issuing"])
        self.assertEqual(self.statuses()[0][0], existing)

    def test_discard_failure_after_activation_failure_message(self) -> None:
        with (
            patch("issuer.issuing.activate_issued",
                  side_effect=sqlite3.OperationalError("database is locked (activate)")),
            patch("issuer.issuing.discard_issuing",
                  side_effect=sqlite3.OperationalError("database is locked (discard)")),
        ):
            code, _, err = self.run_cli(["--benefit", "benefit-1"], [SimulatedNtag215(ntag215_memory())])
        self.assertEqual(code, 1)
        self.assertIn("取得権を有効にできませんでした: database is locked (activate)", err)
        self.assertIn("削除できませんでした", err)
        self.assertIn("--reissue を付けて発行し直してください", err)
        self.assertEqual([status for _, status in self.statuses()], ["issuing"])

    def test_reader_status_error(self) -> None:
        @contextmanager
        def status_reader() -> Iterator[StatusErrorReader]:
            yield StatusErrorReader()

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            code = issue.main(["--benefit", "benefit-1"], reader_factory=status_reader)
        self.assertEqual(code, 1)
        self.assertIn("リーダーとの通信に失敗しました", err.getvalue())
        self.assertIn("RFCA_ERROR", err.getvalue())

    def test_count_waits_for_removal_between_tags(self) -> None:
        tags = [SimulatedNtag215(ntag215_memory(), uid) for uid in ("04000000000001", "04000000000002")]
        code, out, _ = self.run_cli(["--benefit", "benefit-1", "--count", "2"], tags)
        self.assertEqual(code, 0)
        self.assertIn("[2/2] 発行しました", out)
        self.assertEqual(self.wait_for_removal.call_count, 1)
        self.assertIs(self.wait_for_removal.call_args.args[1], tags[0])
        self.assertEqual(len(self.statuses("04000000000001")), 1)
        self.assertEqual(len(self.statuses("04000000000002")), 1)

    def test_count_stops_when_tag_is_not_removed(self) -> None:
        tags = [SimulatedNtag215(ntag215_memory())]
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("issue.wait_for_tag", side_effect=tags),
            patch("issue.wait_for_removal", return_value=False),
            redirect_stdout(stdout), redirect_stderr(stderr),
        ):
            code = issue.main(["--benefit", "benefit-1", "--count", "2"], reader_factory=self.reader_factory)
        self.assertEqual(code, 2)
        self.assertIn("タグが離れませんでした", stderr.getvalue())

    def test_keyboard_interrupt(self) -> None:
        code, out, _ = self.run_cli(["--benefit", "benefit-1"], [KeyboardInterrupt()])
        self.assertEqual(code, 130)
        self.assertIn("終了しました", out)

    def test_reader_error(self) -> None:
        from issuer.nfc_writer import ReaderError

        @contextmanager
        def broken_reader() -> Iterator[FakeReader]:
            raise ReaderError("RC-S380を開けません")
            yield self.reader

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            code = issue.main(["--benefit", "benefit-1"], reader_factory=broken_reader)
        self.assertEqual(code, 1)
        self.assertIn("RC-S380を開けません", err.getvalue())


if __name__ == "__main__":
    unittest.main()
