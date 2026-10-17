"""RC-S380でNTAG215に受け取りページのURLを書き込み、取得権を発行する。"""

import argparse
import sqlite3
import sys
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, closing
from typing import Any

from backend.config import load_settings
from backend.db import connect, init_db, utc_now
from backend.entitlements import ActiveEntitlementExists, BenefitNotFound, Entitlement
from issuer.issuing import IssueError, IssueFailure, IssueInterrupted, IssueResult, issue_one
from issuer.nfc_writer import (
    NfcTagWriter,
    NfcWriterError,
    NotNtag215,
    open_reader,
    wait_for_removal,
    wait_for_tag,
)


EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_TAG = 2
EXIT_INTERRUPTED = 130

_STATUS_LABELS = {
    "unused": "未使用",
    "account": "アカウントに追加済み",
    "download": "登録なしで受け取り済み",
}


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("1以上の整数を指定してください")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benefit", required=True, help="発行する特典ID（例: benefit-001）")
    parser.add_argument(
        "--count", type=positive_int, default=1,
        help="連続して発行する個数（既定: 1）。1個ごとにタグを置き換える",
    )
    parser.add_argument(
        "--reissue", action="store_true",
        help="同じUIDに有効な取得権があっても再発行する（既存の取得権は無効になる）",
    )
    parser.add_argument(
        "--timeout", type=positive_int, default=30,
        help="1個ごとにタグを待つ秒数。タグを離すのを待つ秒数にも使う（既定: 30）",
    )
    return parser


def benefit_exists(conn: sqlite3.Connection, benefit_id: str) -> bool:
    return conn.execute("SELECT 1 FROM benefits WHERE id = ?", (benefit_id,)).fetchone() is not None


def _describe(entitlement: Entitlement) -> str:
    label = _STATUS_LABELS.get(entitlement.status, entitlement.status)
    return f"{label}（{entitlement.status}）、特典 {entitlement.benefit_id}"


def _print_recovery(failure: IssueFailure) -> None:
    """失敗・中断の後の、DBとタグの状態と復旧の手順を表示する。"""
    if failure.discarded:
        print("発行中の取得権は取り消しました。", file=sys.stderr)
    else:
        print(f"発行中の取得権を削除できませんでした（{failure.discard_error!r}）。", file=sys.stderr)
        print("DBに発行中（issuing）のまま残りますが、無効なトークンとして扱われるため"
              "受け取りには使えず、次の発行も妨げません。", file=sys.stderr)
    if failure.previous is not None:
        print(f"既存の取得権は有効なままです: {_describe(failure.previous)}", file=sys.stderr)
    if failure.tag_may_be_corrupted:
        print("タグの内容が書き換わった、または壊れている可能性があります。"
              "--reissue を付けて発行し直してください", file=sys.stderr)
    else:
        print("タグの内容は書き換えていません。", file=sys.stderr)


def _print_issued(result: IssueResult, index: int, count: int) -> None:
    print(f"[{index}/{count}] 発行しました", flush=True)
    print(f"  UID: {result.uid}")
    print(f"  URL: {result.url}")
    if result.revoked is not None:
        print(f"  無効にした既存の取得権: {_describe(result.revoked)}")


def main(
    argv: Sequence[str] | None = None,
    *,
    reader_factory: Callable[[], AbstractContextManager[Any]] = open_reader,
) -> int:
    """発行を実行し、終了コードを返す。reader_factory はテストで差し替える。"""
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except ValueError as error:
        print(f"設定が正しくありません: {error}", file=sys.stderr)
        return EXIT_FAILED
    try:
        with closing(connect(settings.db_path)) as conn:
            init_db(conn)
            if not benefit_exists(conn, args.benefit):
                print(f"特典が見つかりません: {args.benefit}", file=sys.stderr)
                return EXIT_FAILED
            with reader_factory() as reader:
                print(f"リーダー: {reader}", flush=True)
                return _issue_all(conn, reader, args, settings.base_url)
    except KeyboardInterrupt:
        print("\n終了しました")
        return EXIT_INTERRUPTED
    except NfcWriterError as error:
        print(f"エラー: {error}", file=sys.stderr)
        return EXIT_FAILED
    except sqlite3.Error as error:
        print(f"DBの操作に失敗しました: {error}", file=sys.stderr)
        return EXIT_FAILED


def _issue_all(conn: sqlite3.Connection, reader: Any, args: argparse.Namespace, base_url: str) -> int:
    for index in range(1, args.count + 1):
        print(
            f"[{index}/{args.count}] NTAG215を1枚リーダーに置いてください"
            f"（{args.timeout}秒待機、Ctrl+Cで終了）",
            flush=True,
        )
        tag = wait_for_tag(reader, timeout=args.timeout)
        if tag is None:
            print("タグを検出できませんでした。位置を調整して再実行してください", file=sys.stderr)
            return EXIT_NO_TAG
        try:
            writer = NfcTagWriter(tag)
            result = issue_one(
                conn, writer, benefit_id=args.benefit, base_url=base_url,
                now=utc_now(), reissue=args.reissue,
            )
        except NotNtag215 as error:
            print(f"エラー: {error}。書き込まずに終了します", file=sys.stderr)
            return EXIT_FAILED
        except ActiveEntitlementExists as error:
            print(f"警告: UID {error.existing.uid} には有効な取得権があります: "
                  f"{_describe(error.existing)}", file=sys.stderr)
            print("書き込みを中止しました。再発行する場合は --reissue を付けて実行してください",
                  file=sys.stderr)
            return EXIT_FAILED
        except BenefitNotFound as error:
            print(f"エラー: {error}", file=sys.stderr)
            return EXIT_FAILED
        except IssueError as error:
            print(f"エラー: UID {error.failure.uid}: {error}", file=sys.stderr)
            _print_recovery(error.failure)
            return EXIT_FAILED
        except IssueInterrupted as interrupted:
            print(f"\nUID {interrupted.failure.uid} の発行を中断しました", file=sys.stderr)
            _print_recovery(interrupted.failure)
            return EXIT_INTERRUPTED
        _print_issued(result, index, args.count)

        if index < args.count:
            print("タグをリーダーから離してください", flush=True)
            if not wait_for_removal(reader, tag, timeout=args.timeout):
                print(f"{args.timeout}秒以内にタグが離れませんでした。"
                      f"{index}個を発行して終了します", file=sys.stderr)
                return EXIT_NO_TAG
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
