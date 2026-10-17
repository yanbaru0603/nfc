"""デモ用にUvicornでSPAとAPIを配信する（自動リロードなし）。

アクセスログの受取トークンは `/r/***` に伏せる。設定は環境変数（BASE_URL など）から読む。
"""

import argparse
import sys
from collections.abc import Sequence
from urllib.parse import urlsplit

import uvicorn

from backend.config import load_settings
from backend.logs import uvicorn_log_config
from backend.main import create_app
from backend.spa import DEFAULT_DIST_DIR, INDEX_FILE


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="待ち受けるアドレス（既定: 0.0.0.0。同じLANのスマートフォンから接続できる）",
    )
    parser.add_argument("--port", type=int, default=8000, help="待ち受けるポート（既定: 8000）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except ValueError as error:
        print(f"設定が正しくありません: {error}", file=sys.stderr)
        return 1

    print(f"BASE_URL: {settings.base_url}", flush=True)
    print(f"Cookie の Secure: {'有効' if settings.cookie_secure else '無効'}", flush=True)
    if urlsplit(settings.base_url).hostname in _LOCAL_HOSTS:
        print(
            "注意: BASE_URL がこのMacだけのアドレスです。スマートフォンから開くタグを発行する前に、\n"
            "      MacのIPアドレスか公開URLを BASE_URL に指定してください。",
            file=sys.stderr, flush=True,
        )
    if not (DEFAULT_DIST_DIR / INDEX_FILE).is_file():
        print(
            f"警告: ビルド済みのフロントエンドがありません（{DEFAULT_DIST_DIR}）。APIだけを配信します。\n"
            "      画面も配信するには、先に `mise exec -- pnpm -C frontend build` を実行してください。",
            file=sys.stderr, flush=True,
        )
    uvicorn.run(
        create_app(settings, dist_dir=DEFAULT_DIST_DIR),
        host=args.host,
        port=args.port,
        log_config=uvicorn_log_config(),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
