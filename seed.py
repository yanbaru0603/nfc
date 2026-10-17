"""作品・音源・アカウント・開発用取得権を初期化する。"""

import argparse
import math
import os
import sqlite3
import struct
import sys
import tempfile
import wave
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path

from backend.config import PROJECT_ROOT, Settings, load_settings
from backend.db import connect, format_utc, init_db, transaction, utc_now


DEV_TOKENS = tuple(f"dev-unused-token-{number:04d}" for number in range(1, 4))
TRACKS = (("朝の光", 261.63), ("風の便り", 329.63), ("星の帰り道", 392.00))


def _generate_wav(path: Path, frequency: float) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 22050
    frame_count = sample_rate * 6
    frames = bytearray()
    for frame in range(frame_count):
        # 最初と最後を短くフェードし、再生開始・終了時のクリック音を抑える。
        envelope = min(1.0, frame / (sample_rate * 0.05),
                       (frame_count - 1 - frame) / (sample_rate * 0.05))
        sample = int(6000 * envelope * math.sin(2 * math.pi * frequency * frame / sample_rate))
        frames.extend(struct.pack("<h", sample))
    # 同じディレクトリで完成させてから公開し、中断時も壊れたWAVを残さない。
    with tempfile.NamedTemporaryFile(dir=path.parent) as temporary:
        with wave.open(temporary, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(frames)
        temporary.flush()
        os.replace(temporary.name, path)


def seed(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> None:
    """素材と初期データを登録。再実行時も既存の受取状態を維持。"""
    issued_at = format_utc(utc_now() if now is None else now)
    cover_path = PROJECT_ROOT / "frontend/public/covers/work-001.svg"
    if not cover_path.is_file():
        print(f"警告: 表紙ファイルが見つかりません: {cover_path}", file=sys.stderr)

    for position, (_, frequency) in enumerate(TRACKS, start=1):
        _generate_wav(settings.media_dir / f"benefit-001/{position:02d}.wav", frequency)

    with transaction(conn):
        conn.execute(
            "INSERT INTO works (id, title, cover_path) VALUES (?, ?, ?) ON CONFLICT(id) DO NOTHING",
            ("work-001", "星めぐりの物語", "/covers/work-001.svg"),
        )
        conn.execute(
            "INSERT INTO benefits (id, work_id, title, description) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
            ("benefit-001", "work-001", "特装版CD 特典音源", "デモ用に生成した3曲の単音音源"),
        )
        for position, (title, _) in enumerate(TRACKS, start=1):
            conn.execute(
                "INSERT INTO benefit_files "
                "(id, benefit_id, position, title, file_path, mime_type) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                (f"file-{position:03d}", "benefit-001", position, title,
                 f"benefit-001/{position:02d}.wav", "audio/wav"),
            )
        conn.executemany(
            "INSERT INTO accounts (id, name) VALUES (?, ?) ON CONFLICT(id) DO NOTHING",
            (("demo-alice", "アリス"), ("demo-bob", "ボブ"), ("demo-carol", "キャロル")),
        )
        for number, token in enumerate(DEV_TOKENS, start=1):
            conn.execute(
                "INSERT INTO entitlements "
                "(token, benefit_id, uid, status, issued_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(token) DO NOTHING",
                (token, "benefit-001", f"DEV-{number:04d}", "unused", issued_at),
            )


def main(argv: Sequence[str] | None = None) -> int:
    """シードを実行し、開発用の受け取りURLを表示。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset", action="store_true",
        help="DBを削除し、未使用の取得権で作り直す。サーバーと発行CLIを止めてから実行してください",
    )
    args = parser.parse_args(argv)
    settings = load_settings()
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{settings.db_path}{suffix}").unlink(missing_ok=True)
    with closing(connect(settings.db_path)) as conn:
        init_db(conn)
        seed(conn, settings)
        for token in DEV_TOKENS:
            status = conn.execute(
                "SELECT status FROM entitlements WHERE token = ?", (token,),
            ).fetchone()["status"]
            note = ""
            if status != "unused":
                label = {"account": "受け取り済み", "download": "受け取り済み",
                         "issuing": "発行中", "revoked": "無効"}[status]
                note = f" （{label}: {status}。--reset で未使用に戻せます）"
            print(f"{settings.base_url}/r/{token}{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
