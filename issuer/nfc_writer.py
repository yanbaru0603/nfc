"""RC-S380でNTAG215を判定し、NDEFのURIレコードを書き込んで読み直す。

NFC機器の操作だけを扱い、backend/ は読み込まない。nfcpyの例外はここで
NfcWriterError の派生に変換し、呼び出し側がnfcpyに依存しないようにする。
"""

import ctypes
import time
from collections.abc import Iterator
from contextlib import contextmanager

import libusb_package
import ndef
import nfc
import nfc.clf
import nfc.clf.pn53x
import nfc.clf.rcs380
import nfc.tag
import usb1
from nfc.tag.tt2_nxp import NTAG215


NTAG215_PRODUCT = "NXP NTAG215"

# リーダーやドライバーから伝わる既知の例外。nfcpyはドライバー固有の例外
# （RC-S380のステータス応答など）を共通の例外に変換しないことがあるため、ここで捕まえる。
_DRIVER_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    usb1.USBError,
    nfc.clf.Error,
    nfc.clf.rcs380.StatusError,
    nfc.clf.rcs380.CommunicationError,
    nfc.clf.pn53x.Chipset.Error,
)


class NfcWriterError(Exception):
    """NFC機器操作で投げる例外の基底。"""


class ReaderError(NfcWriterError):
    """RC-S380を開けない・閉じられない、またはリーダーとの通信に失敗した。

    書き込み中の失敗では tag_modified に、タグの内容を変更し始めていたかを入れる。
    書き込み以外の操作ではNone。
    """

    def __init__(self, message: str, *, tag_modified: bool | None = None) -> None:
        super().__init__(message)
        self.tag_modified = tag_modified


class NotNtag215(NfcWriterError):
    """検出したタグがNTAG215ではない。"""


class TagWriteError(NfcWriterError):
    """NDEF用の初期化・書き込みに失敗した。

    tag_modified は、タグの内容を変更し始めていたか（内容が壊れている可能性があるか）。
    """

    def __init__(self, message: str, *, tag_modified: bool) -> None:
        super().__init__(message)
        self.tag_modified = tag_modified


class TagReadError(NfcWriterError):
    """照合のための読み直しで、タグとの通信に失敗した。"""


class TagWriteInterrupted(KeyboardInterrupt):
    """書き込み中のCtrl+C。tag_modified は、タグの内容を変更し始めていたか。

    KeyboardInterrupt の派生なので、捕まえなければ通常の中断として扱われる。
    """

    def __init__(self, *, tag_modified: bool) -> None:
        super().__init__("タグへの書き込み中に中断しました")
        self.tag_modified = tag_modified


def _describe_error(error: BaseException) -> str:
    """例外の型名と内容。RC-S380のステータス応答は内容がステータス名（例: RFCA_ERROR）になる。"""
    detail = str(error)
    return f"{type(error).__name__}: {detail}" if detail else repr(error)


def _communication_error(error: BaseException, *, tag_modified: bool | None = None) -> ReaderError:
    return ReaderError(f"リーダーとの通信に失敗しました: {_describe_error(error)}", tag_modified=tag_modified)


@contextmanager
def open_reader(path: str = "usb:054c") -> Iterator[nfc.ContactlessFrontend]:
    """仮想環境内のlibusbを読み込んでRC-S380を開き、with文を抜けるときに閉じる。

    開く・閉じるときのドライバーの例外は ReaderError に変換する。with文の中で例外が
    起きた場合は、閉じるときの失敗よりも元の例外を優先する。
    """
    library_path = libusb_package.get_library_path()
    if library_path is None:
        raise ReaderError("libusbが見つかりません。poetry syncで依存ライブラリを再インストールしてください")
    try:
        usb1.loadLibrary(ctypes.CDLL(str(library_path)))
        reader = nfc.ContactlessFrontend(path)
    except _DRIVER_ERRORS as error:
        raise ReaderError(
            f"RC-S380を開けません: {_describe_error(error)}。"
            "USB接続と、ほかの読み取りアプリが使用していないかを確認してください"
        ) from error
    try:
        yield reader
    except BaseException:
        try:
            reader.close()
        except _DRIVER_ERRORS:
            pass  # 元の例外を優先する。
        raise
    try:
        reader.close()
    except _DRIVER_ERRORS as error:
        raise ReaderError(f"RC-S380を閉じられませんでした: {_describe_error(error)}") from error


def wait_for_tag(
    reader: nfc.ContactlessFrontend, *, timeout: float, interval: float = 0.1,
) -> nfc.tag.Tag | None:
    """106Aのタグを待ち受けて有効化する。timeout秒以内に検出できなければNone。

    Type 2 Tag以外（Type 4など）も返すため、呼び出し側で is_ntag215 を確認する。
    """
    target = nfc.clf.RemoteTarget("106A")
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            detected = reader.sense(target, iterations=1)
            if detected is not None:
                tag = nfc.tag.activate(reader, detected)
                if tag is not None:
                    return tag
            time.sleep(interval)
    except _DRIVER_ERRORS as error:
        raise _communication_error(error) from error
    return None


def is_ntag215(tag: object) -> bool:
    """nfcpyの型、または製品名でNTAG215かを判定。"""
    return isinstance(tag, NTAG215) or getattr(tag, "product", None) == NTAG215_PRODUCT


def tag_uid(tag: nfc.tag.Tag) -> str:
    """UIDを大文字の16進数（区切りなし）で取得。例：04A1B2C3D4E580。"""
    return bytes(tag.identifier).hex().upper()


def encode_uri_message(url: str) -> bytes:
    """URIレコード1件だけのNDEFメッセージにエンコード。"""
    return b"".join(ndef.message_encoder([ndef.UriRecord(url)]))


def write_uri(tag: nfc.tag.Tag, url: str) -> None:
    """NDEFのURIレコード1件を書き込む。NDEF用でないタグは先に初期化する。

    ロックされている・容量が足りない・タグとの通信に失敗した場合は TagWriteError、
    リーダーのドライバーの例外は ReaderError、Ctrl+C は TagWriteInterrupted。
    いずれも tag_modified にタグを変更し始めていたかを入れる。
    ロックやパスワード保護はしない（デモでタグを使い回すため）。
    """
    message = encode_uri_message(url)
    modified = False
    try:
        area = tag.ndef
        if area is None:
            modified = True
            if tag.format() is not True:
                raise TagWriteError("タグをNDEF用に初期化できませんでした", tag_modified=True)
            area = tag.ndef
            if area is None:
                raise TagWriteError("初期化後もNDEFの領域を読み取れません", tag_modified=True)
        if not area.is_writeable:
            raise TagWriteError("タグが書き込み禁止になっています", tag_modified=modified)
        if len(message) > area.capacity:
            raise TagWriteError(
                f"URLがタグの容量を超えています（{len(message)} / {area.capacity}バイト）",
                tag_modified=modified,
            )
        modified = True
        area.records = [ndef.UriRecord(url)]
    except nfc.tag.TagCommandError as error:
        raise TagWriteError(
            f"タグへの書き込みに失敗しました: {error}", tag_modified=modified,
        ) from error
    except _DRIVER_ERRORS as error:
        raise _communication_error(error, tag_modified=modified) from error
    except KeyboardInterrupt as error:
        raise TagWriteInterrupted(tag_modified=modified) from error


def read_uri(tag: nfc.tag.Tag) -> str | None:
    """タグを読み直し、URIレコード1件だけならそのURLを返す。それ以外はNone。

    nfcpyは tag.ndef を最初の読み取りで保持し、書き込み後は書いた内容を
    records として返す。has_changed を参照するとタグのメモリを読み直すため、
    照合ではキャッシュではなくタグの実際の内容を見る。
    """
    try:
        area = tag.ndef
        if area is not None:
            _ = area.has_changed  # タグから読み直す。失敗すると tag.ndef が None になる。
            area = tag.ndef
        if area is None:
            return None
        records = area.records
    except ndef.DecodeError:
        return None
    except nfc.tag.TagCommandError as error:
        raise TagReadError(f"タグの読み直しに失敗しました: {error}") from error
    except _DRIVER_ERRORS as error:
        raise _communication_error(error) from error
    if len(records) == 1 and isinstance(records[0], ndef.UriRecord):
        return records[0].iri
    return None


def wait_for_removal(
    reader: nfc.ContactlessFrontend,
    tag: nfc.tag.Tag,
    *,
    timeout: float,
    interval: float = 0.1,
    misses: int = 3,
) -> bool:
    """タグが離れるまで待つ。timeout秒たっても離れなければFalse。

    連続発行で同じタグをすぐ再検出しないために使う。同じUIDだけを探す待ち受けで
    misses 回続けて見つからなければ、離れたとみなす（一時的な通信の失敗で
    誤って離れたと判定しないため）。
    """
    # sel_reqにUIDを指定すると、そのUIDのタグだけが応答する。
    target = nfc.clf.RemoteTarget("106A", sel_req=bytearray(tag.identifier))
    deadline = time.monotonic() + timeout
    missed = 0
    try:
        while time.monotonic() < deadline:
            if reader.sense(target, iterations=1) is None:
                missed += 1
                if missed >= misses:
                    return True
            else:
                missed = 0
            time.sleep(interval)
    except _DRIVER_ERRORS as error:
        raise _communication_error(error) from error
    return False


class NfcTagWriter:
    """nfcpyのタグを、発行処理（issuer.issuing.TagWriter）の形に合わせる。

    NTAG215でなければ NotNtag215。
    """

    def __init__(self, tag: nfc.tag.Tag) -> None:
        if not is_ntag215(tag):
            product = getattr(tag, "product", type(tag).__name__)
            raise NotNtag215(f"NTAG215ではありません（検出したタグ: {product}）")
        self._tag = tag

    def uid(self) -> str:
        return tag_uid(self._tag)

    def write_uri(self, url: str) -> None:
        write_uri(self._tag, url)

    def read_uri(self) -> str | None:
        return read_uri(self._tag)
