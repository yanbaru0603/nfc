"""RC-S380でSuicaのIDmと最新履歴の残高を1回読み取る。"""

import argparse
import ctypes
import sys
import time

import libusb_package
import nfc
import usb1
from nfc.tag.tt3 import BlockCode, ServiceCode


def decode_balance(record: bytes) -> int | None:
    """利用履歴1件（16バイト）から残高を取得。未使用の履歴はNone。"""
    if len(record) != 16:
        raise ValueError("利用履歴は16バイトである必要があります")
    if not any(record):
        return None
    return int.from_bytes(record[10:12], "little")


def positive_seconds(value: str) -> int:
    seconds = int(value)
    if seconds <= 0:
        raise argparse.ArgumentTypeError("1以上の秒数を指定してください")
    return seconds


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout", type=positive_seconds, default=30,
        help="カードを待つ秒数（既定: 30）",
    )
    args = parser.parse_args()

    try:
        # 仮想環境内のlibusbを使用。Homebrewへの追加インストールは不要。
        library_path = libusb_package.get_library_path()
        if library_path is None:
            print("libusbが見つかりません。poetry syncで依存ライブラリを再インストールしてください",
                  file=sys.stderr)
            return 1
        usb1.loadLibrary(ctypes.CDLL(str(library_path)))

        with nfc.ContactlessFrontend("usb:054c") as reader:
            print(f"リーダー: {reader}", flush=True)
            print(f"Suicaを1枚かざしてください（{args.timeout}秒待機、Ctrl+Cで終了）",
                  flush=True)

            # Polling: コマンド00 / Suicaのシステム0003 / システムコード要求01。
            target = nfc.clf.RemoteTarget(
                "212F", sensf_req=bytearray.fromhex("0000030100")
            )
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                detected = reader.sense(target, iterations=1)
                if detected is None:
                    time.sleep(0.1)
                    continue
                tag = nfc.tag.activate(reader, detected)
                if tag is None:
                    time.sleep(0.1)
                    continue

                print(f"IDm: {tag.idm.hex().upper()}")
                print(f"PMm: {tag.pmm.hex().upper()}")

                # 履歴サービス090Fのブロック0が最新の利用履歴。
                service = ServiceCode(0x090F >> 6, 0x090F & 0x3F)
                record = tag.read_without_encryption([service], [BlockCode(0)])
                balance = decode_balance(record)
                if balance is None:
                    print("利用履歴が空のため、残高を確認できませんでした")
                else:
                    print(f"最新履歴の残高: {balance:,}円")
                return 0

            print("カードを検出できませんでした。位置を調整して再実行してください",
                  file=sys.stderr)
            return 2
    except nfc.tag.TagCommandError as error:
        print(f"カードの読み取りに失敗しました。かざしたまま再実行してください: {error}",
              file=sys.stderr)
        return 1
    except (OSError, usb1.USBError) as error:
        print(f"リーダーとの通信に失敗しました: {error}", file=sys.stderr)
        print("USB接続と、ほかの読み取りアプリが使用していないかを確認してください",
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n終了しました")
        return 130


if __name__ == "__main__":
    sys.exit(main())
