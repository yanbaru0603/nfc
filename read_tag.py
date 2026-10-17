"""RC-S380でNTAG215のUIDと、書き込まれた受け取りURLを1回読み取る。"""

import argparse
import sqlite3
import sys
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, closing
from typing import Any

from backend.config import load_settings
from backend.db import connect, init_db
from backend.entitlements import allow_repeat_for_uid
from issuer.nfc_writer import NfcTagWriter, NfcWriterError, open_reader, wait_for_tag


def positive_seconds(value: str) -> int:
    seconds = int(value)
    if seconds <= 0:
        raise argparse.ArgumentTypeError("1以上の秒数を指定してください")
    return seconds


def main(
    argv: Sequence[str] | None = None,
    *,
    reader_factory: Callable[[], AbstractContextManager[Any]] = open_reader,
) -> int:
    """通常は読み取りだけ。明示した場合だけ、読み取ったUIDをDBの例外に登録する。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout", type=positive_seconds, default=30,
        help="タグを待つ秒数（既定: 30）",
    )
    parser.add_argument(
        "--allow-repeat", action="store_true",
        help="今回読み取ったカードだけ、DBで何度でも特典を受け取れる例外に登録する",
    )
    args = parser.parse_args(argv)

    try:
        with reader_factory() as reader:
            print(f"リーダー: {reader}", flush=True)
            print(
                f"書き込み済みのNTAG215を1枚リーダーに置いてください"
                f"（{args.timeout}秒待機、Ctrl+Cで終了）",
                flush=True,
            )
            tag = wait_for_tag(reader, timeout=args.timeout)
            if tag is None:
                print("タグを検出できませんでした。位置を調整して再実行してください", file=sys.stderr)
                return 2
            reader_tag = NfcTagWriter(tag)
            print(f"UID: {reader_tag.uid()}", flush=True)
            url = reader_tag.read_uri()
            if args.allow_repeat:
                settings = load_settings()
                with closing(connect(settings.db_path)) as conn:
                    init_db(conn)
                    allow_repeat_for_uid(conn, reader_tag.uid())
                print("このカードを、何度でも特典を受け取れる例外に登録しました。", flush=True)
            if url is None:
                if args.allow_repeat:
                    print("URLは未登録です。issue.pyで特典を発行すると、繰り返し受取の例外が適用されます。",
                          flush=True)
                    return 0
                print("URIレコード1件のURLを読み取れませんでした。タグの書き込み内容を確認してください",
                      file=sys.stderr)
                return 1
            print(f"URL: {url}", flush=True)
            print("表示されたURLをブラウザで開くと、特典を受け取れます。", flush=True)
        return 0
    except NfcWriterError as error:
        print(f"エラー: {error}", file=sys.stderr)
        return 1
    except (sqlite3.Error, ValueError) as error:
        print(f"例外を登録できませんでした: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n終了しました")
        return 130


if __name__ == "__main__":
    sys.exit(main())
