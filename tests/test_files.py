"""認可に依存しないパス検証、Range配信、日本語ZIPと削除を確認。"""

import io
import os
import tempfile
import unittest
import zipfile
from dataclasses import FrozenInstanceError
from functools import partial
from pathlib import Path
from typing import cast
from unittest.mock import patch
from urllib.parse import quote

import anyio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import FileResponse, Response
from starlette.types import Message, Scope

from backend import files
from backend.files import (
    ArchiveItem,
    MediaFileError,
    archive_entry_name,
    archive_name,
    archive_response,
    content_disposition,
    inline_file_response,
    resolve_media_path,
    safe_filename,
)


class MediaPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.media_dir = self.root / "media"
        self.media_dir.mkdir()
        self.track = self.media_dir / "benefit/曲名.wav"
        self.track.parent.mkdir()
        self.track.write_bytes(b"audio")

    def test_relative_path_resolves_to_existing_file(self) -> None:
        self.assertEqual(resolve_media_path(self.media_dir, "benefit/曲名.wav"), self.track)

    def test_parent_components_are_rejected_even_when_staying_inside(self) -> None:
        for relative in ("..", "../outside.wav", "benefit/../benefit/曲名.wav"):
            with self.subTest(relative=relative), self.assertRaises(MediaFileError):
                resolve_media_path(self.media_dir, relative)

    def test_absolute_path_inside_media_is_also_rejected(self) -> None:
        with self.assertRaises(MediaFileError):
            resolve_media_path(self.media_dir, str(self.track))

    def test_external_file_and_directory_symlinks_are_rejected(self) -> None:
        outside = self.root / "media-other"
        outside.mkdir()
        (outside / "outside.wav").write_bytes(b"outside")
        (self.media_dir / "file-link").symlink_to(outside / "outside.wav")
        (self.media_dir / "directory-link").symlink_to(outside, target_is_directory=True)
        for relative in ("file-link", "directory-link/outside.wav"):
            with self.subTest(relative=relative), self.assertRaises(MediaFileError):
                resolve_media_path(self.media_dir, relative)

    def test_internal_symlink_resolves_to_target(self) -> None:
        (self.media_dir / "internal-link").symlink_to(self.track)
        self.assertEqual(resolve_media_path(self.media_dir, "internal-link"), self.track)

    def test_media_directory_symlink_resolves_to_target(self) -> None:
        media_link = self.root / "media-link"
        media_link.symlink_to(self.media_dir, target_is_directory=True)
        self.assertEqual(resolve_media_path(media_link, "benefit/曲名.wav"), self.track)

    def test_missing_files_and_broken_or_looping_symlinks_are_rejected(self) -> None:
        (self.media_dir / "broken-link").symlink_to(self.media_dir / "missing.wav")
        (self.media_dir / "loop-link").symlink_to(self.media_dir / "loop-link")
        for relative in ("missing.wav", "broken-link", "loop-link"):
            with self.subTest(relative=relative), self.assertRaises(MediaFileError):
                resolve_media_path(self.media_dir, relative)

    def test_directory_and_empty_path_are_rejected(self) -> None:
        for relative in ("benefit", "", "."):
            with self.subTest(relative=relative), self.assertRaises(MediaFileError):
                resolve_media_path(self.media_dir, relative)

    def test_nul_is_rejected(self) -> None:
        with self.assertRaises(MediaFileError):
            resolve_media_path(self.media_dir, "benefit/曲名.wav\0")

    def test_non_regular_file_is_rejected(self) -> None:
        os.mkfifo(self.media_dir / "pipe")
        with self.assertRaises(MediaFileError):
            resolve_media_path(self.media_dir, "pipe")

    def test_missing_media_directory_is_rejected(self) -> None:
        with self.assertRaises(MediaFileError):
            resolve_media_path(self.root / "missing-media", "track.wav")


class FilenameTest(unittest.TestCase):
    def test_forbidden_characters_are_replaced(self) -> None:
        for character in '/\\:*?"<>|\0\n\r\t\x1f\x7f\x85':
            with self.subTest(character=repr(character)):
                self.assertEqual(safe_filename(f"A{character}B"), "A_B")

    def test_unicode_format_and_line_or_paragraph_separators_are_replaced(self) -> None:
        for character in "\u202e\u200b\ufeff\u2066\u2069\u2028\u2029":
            with self.subTest(character=repr(character)):
                self.assertEqual(safe_filename(f"A{character}B.wav"), "A_B.wav")

    def test_surrounding_spaces_and_dots_are_removed(self) -> None:
        self.assertEqual(safe_filename(" .　.曲名 A .　. "), "曲名 A")

    def test_empty_names_have_a_replacement(self) -> None:
        for name in ("", " ", ".", " ...　. "):
            with self.subTest(name=name):
                self.assertEqual(safe_filename(name), "_")

    def test_japanese_emoji_and_semicolon_are_preserved(self) -> None:
        self.assertEqual(safe_filename("曲名;🎵.wav"), "曲名;🎵.wav")

    def test_content_disposition_has_ascii_fallback_and_encoded_unicode(self) -> None:
        name = "曲名;🎵.wav"
        header = content_disposition("inline", name, "track.wav")
        self.assertEqual(header, f'inline; filename="track.wav"; filename*=UTF-8\'\'{quote(name, safe="")}')
        self.assertTrue(header.isascii())

    def test_content_disposition_sanitizes_both_names(self) -> None:
        header = content_disposition("attachment", ' .曲/名";\n🎵.zip ', ' ../曲\\名";\r\n.zip. ')
        self.assertEqual(header, 'attachment; filename="_____;__.zip"; filename*=UTF-8\'\''
                         + quote("曲_名_;_🎵.zip", safe=""))
        self.assertTrue(header.isascii())
        self.assertNotIn("\r", header)
        self.assertNotIn("\n", header)

    def test_content_disposition_empty_names_and_percent_encoding(self) -> None:
        self.assertEqual(content_disposition("attachment", "", ""),
                         'attachment; filename="_"; filename*=UTF-8\'\'_')
        self.assertEqual(content_disposition("inline", "A %;B.wav", "A;B.wav"),
                         'inline; filename="A;B.wav"; filename*=UTF-8\'\'A%20%25%3BB.wav')

    def test_content_disposition_replaces_percent_only_in_ascii_fallback(self) -> None:
        name = "100%25 曲名.wav"
        self.assertEqual(content_disposition("inline", name, "100%25 track.wav"),
                         'inline; filename="100_25 track.wav"; filename*=UTF-8\'\''
                         + quote(name, safe=""))

    def test_archive_entry_uses_position_title_and_actual_extension(self) -> None:
        self.assertEqual(archive_entry_name(1, "曲名A", Path("original.wav")), "01_曲名A.wav")
        self.assertEqual(archive_entry_name(12, ' ../曲\\名" ', Path("original.MP3")), "12__曲_名_.MP3")
        self.assertEqual(archive_entry_name(100, "", Path("original")), "100__")

    def test_archive_name_sanitizes_titles(self) -> None:
        self.assertEqual(archive_name("作品名", "特典名"), "作品名_特典名.zip")
        self.assertEqual(archive_name(" ../作品 ", '特典"\n'), "_作品_特典__.zip")
        self.assertEqual(archive_name("", ""), "___.zip")


class FileResponseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.media_dir = self.root / "media"
        self.media_dir.mkdir()
        self.track = self.media_dir / "01.wav"
        self.audio = bytes(range(64))
        self.track.write_bytes(self.audio)
        self.second_track = self.media_dir / "02.mp3"
        self.second_track.write_bytes(b"second audio")
        self.items = (
            ArchiveItem(self.track, archive_entry_name(1, "曲名A", self.track)),
            ArchiveItem(self.second_track, archive_entry_name(2, "曲名B🎵", self.second_track)),
        )
        self.zip_name = archive_name("作品名", "特典名")
        self.fallback = "benefit.zip"
        self.zip_dir = self.root / "archives"
        self.zip_dir.mkdir()
        create_temporary = partial(tempfile.NamedTemporaryFile, dir=self.zip_dir)
        self.temp_patch = patch.object(files, "tempfile", wraps=tempfile)
        self.create_temporary = self.temp_patch.start().NamedTemporaryFile
        self.create_temporary.side_effect = create_temporary
        self.addCleanup(self.temp_patch.stop)
        self.archive_paths: list[Path] = []
        app = FastAPI()

        def play() -> Response:
            path = resolve_media_path(self.media_dir, "01.wav")
            return inline_file_response(path, mime_type="audio/wav", filename="曲名A.wav")

        def download() -> Response:
            response = archive_response(self.items, zip_name=self.zip_name, fallback=self.fallback)
            self.assertIsInstance(response, FileResponse)
            path = Path(cast(FileResponse, response).path)
            self.assertTrue(path.is_file())
            self.archive_paths.append(path)
            return response

        app.add_api_route("/play", play, methods=["GET", "HEAD"])
        app.add_api_route("/archive", download, methods=["GET", "HEAD"])
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_inline_response_contains_file_and_headers(self) -> None:
        response = self.client.get("/play")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.audio)
        self.assertEqual(response.headers["content-type"], "audio/wav")
        self.assertEqual(response.headers["content-length"], "64")
        self.assertEqual(response.headers["content-disposition"],
                         content_disposition("inline", "曲名A.wav", "01.wav"))
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_inline_byte_range_returns_partial_content(self) -> None:
        response = self.client.get("/play", headers={"Range": "bytes=0-9"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 0-9/64")
        self.assertEqual(response.headers["content-length"], "10")
        self.assertEqual(response.content, self.audio[:10])
        self.assertTrue(response.headers["content-disposition"].startswith("inline;"))
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_inline_safari_initial_range_returns_two_bytes(self) -> None:
        response = self.client.get("/play", headers={"Range": "bytes=0-1"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 0-1/64")
        self.assertEqual(response.headers["content-length"], "2")
        self.assertEqual(response.content, self.audio[:2])

    def test_inline_suffix_and_open_ended_ranges(self) -> None:
        for value in ("bytes=-10", "bytes=54-"):
            with self.subTest(value=value):
                response = self.client.get("/play", headers={"Range": value})
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.headers["content-range"], "bytes 54-63/64")
                self.assertEqual(response.content, self.audio[-10:])

    def test_inline_unsatisfiable_range_keeps_headers(self) -> None:
        response = self.client.get("/play", headers={"Range": "bytes=64-100"})
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["content-range"], "bytes */64")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertTrue(response.headers["content-disposition"].startswith("inline;"))

    def test_inline_malformed_range_keeps_no_store(self) -> None:
        response = self.client.get("/play", headers={"Range": "invalid"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_inline_head_returns_headers_without_body(self) -> None:
        response = self.client.head("/play")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["content-length"], "64")

    def test_inline_head_with_range_returns_partial_headers_without_body(self) -> None:
        response = self.client.head("/play", headers={"Range": "bytes=0-1"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 0-1/64")
        self.assertEqual(response.headers["content-length"], "2")
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["content-disposition"],
                         content_disposition("inline", "曲名A.wav", "01.wav"))

    def test_multiple_ranges_keep_no_store_and_content_disposition(self) -> None:
        for url, disposition in (
            ("/play", content_disposition("inline", "曲名A.wav", "01.wav")),
            ("/archive", content_disposition("attachment", self.zip_name, self.fallback)),
        ):
            with self.subTest(url=url):
                response = self.client.get(url, headers={"Range": "bytes=0-1,10-11"})
                self.assertEqual(response.status_code, 206)
                self.assertTrue(response.headers["content-type"].startswith("multipart/byteranges;"))
                self.assertEqual(int(response.headers["content-length"]), len(response.content))
                self.assertIn(b"Content-Range: bytes 0-1/", response.content)
                self.assertIn(b"Content-Range: bytes 10-11/", response.content)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["content-disposition"], disposition)
                self.assertEqual(response.headers["accept-ranges"], "bytes")
                self.assertEqual(list(self.zip_dir.iterdir()), [])

    def test_zip_contains_stored_files_with_unicode_names_and_utf8_flags(self) -> None:
        response = self.client.get("/archive")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["content-disposition"],
                         f'attachment; filename="benefit.zip"; filename*=UTF-8\'\'{quote(self.zip_name, safe="")}')
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(archive.namelist(), ["01_曲名A.wav", "02_曲名B🎵.mp3"])
            self.assertEqual(archive.read("01_曲名A.wav"), self.audio)
            self.assertEqual(archive.read("02_曲名B🎵.mp3"), b"second audio")
            for entry in archive.infolist():
                self.assertEqual(entry.compress_type, zipfile.ZIP_STORED)
                self.assertTrue(entry.flag_bits & 0x800)
            self.assertIsNone(archive.testzip())
        self.assertFalse(self.archive_paths[0].exists())
        self.assertEqual(list(self.zip_dir.iterdir()), [])

    def test_zip_is_created_for_each_request_from_current_files(self) -> None:
        first = self.client.get("/archive")
        self.track.write_bytes(b"updated audio")
        second = self.client.get("/archive")
        with zipfile.ZipFile(io.BytesIO(first.content)) as archive:
            self.assertEqual(archive.read(self.items[0].arcname), self.audio)
        with zipfile.ZipFile(io.BytesIO(second.content)) as archive:
            self.assertEqual(archive.read(self.items[0].arcname), b"updated audio")
        self.assertEqual(len(set(self.archive_paths)), 2)
        self.assertTrue(all(not path.exists() for path in self.archive_paths))

    def test_zip_name_is_sanitized_and_zip_extension_is_added_once(self) -> None:
        for name, expected in ((" .作品/特典\"\n ", "作品_特典__.zip"),
                               ("作品.zip", "作品.zip"), ("作品.ZIP", "作品.zip"),
                               ("", "_.zip")):
            with self.subTest(name=name):
                self.zip_name = name
                response = self.client.get("/archive")
                self.assertEqual(response.headers["content-disposition"],
                                 content_disposition("attachment", expected, "benefit.zip"))

    def test_zip_fallback_is_safe_ascii(self) -> None:
        self.fallback = ' ./代替\\名";\r\n.zip. '
        response = self.client.get("/archive")
        header = response.headers["content-disposition"]
        self.assertEqual(header, content_disposition("attachment", self.zip_name, self.fallback))
        self.assertTrue(header.isascii())
        self.assertNotIn("\r", header)
        self.assertNotIn("\n", header)

    def test_zip_cleanup_after_partial_range_and_range_errors(self) -> None:
        for value, status in (("bytes=0-9", 206), ("bytes=999999-", 416), ("invalid", 400)):
            with self.subTest(value=value):
                response = self.client.get("/archive", headers={"Range": value})
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertTrue(response.headers["content-disposition"].startswith("attachment;"))
                self.assertFalse(self.archive_paths[-1].exists())
                self.assertEqual(list(self.zip_dir.iterdir()), [])
                if status == 206:
                    self.assertEqual(len(response.content), 10)

    def test_zip_cleanup_after_head(self) -> None:
        response = self.client.head("/archive")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertFalse(self.archive_paths[0].exists())

    def test_zip_cleanup_after_head_with_range(self) -> None:
        response = self.client.head("/archive", headers={"Range": "bytes=0-1"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-length"], "2")
        self.assertTrue(response.headers["content-range"].startswith("bytes 0-1/"))
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["content-disposition"],
                         content_disposition("attachment", self.zip_name, self.fallback))
        self.assertFalse(self.archive_paths[0].exists())
        self.assertEqual(list(self.zip_dir.iterdir()), [])

    def test_zip_cleanup_after_outer_cancellation(self) -> None:
        async def cancel_transfer(spec_version: str) -> None:
            response = archive_response(self.items, zip_name=self.zip_name)
            path = Path(cast(FileResponse, response).path)
            scope: Scope = {
                "type": "http", "method": "GET", "headers": [],
                "asgi": {"spec_version": spec_version},
            }

            async def receive() -> Message:
                await anyio.sleep_forever()
                return {"type": "http.disconnect"}

            async def send(message: Message) -> None:
                if message["type"] == "http.response.body":
                    self.assertTrue(message["body"])
                    self.assertTrue(path.is_file())
                    # 送信開始後に外側の期限を発動させ、処理速度に依存させない。
                    cancellation.deadline = anyio.current_time()
                    await anyio.sleep_forever()

            with anyio.fail_after(5):
                with anyio.move_on_after(None) as cancellation:
                    await response(scope, receive, send)
            self.assertTrue(cancellation.cancelled_caught)
            self.assertFalse(path.exists())
            self.assertEqual(list(self.zip_dir.iterdir()), [])

        for spec_version in ("2.3", "2.4"):
            with self.subTest(spec_version=spec_version):
                anyio.run(cancel_transfer, spec_version)

    def test_zip_cleanup_after_client_disconnect(self) -> None:
        async def disconnect_transfer() -> None:
            response = archive_response(self.items, zip_name=self.zip_name)
            path = Path(cast(FileResponse, response).path)
            body_started = anyio.Event()
            scope: Scope = {
                "type": "http", "method": "GET", "headers": [],
                "asgi": {"spec_version": "2.3"},
            }

            async def receive() -> Message:
                await body_started.wait()
                return {"type": "http.disconnect"}

            async def send(message: Message) -> None:
                if message["type"] == "http.response.body":
                    self.assertTrue(message["body"])
                    self.assertTrue(path.is_file())
                    body_started.set()
                    await anyio.sleep_forever()

            with anyio.fail_after(5):
                await response(scope, receive, send)
            self.assertTrue(body_started.is_set())
            self.assertFalse(path.exists())
            self.assertEqual(list(self.zip_dir.iterdir()), [])

        anyio.run(disconnect_transfer)

    def test_zip_cleanup_after_disconnected_client_send_error(self) -> None:
        async def fail_transfer() -> None:
            response = archive_response(self.items, zip_name=self.zip_name)
            path = Path(cast(FileResponse, response).path)
            scope: Scope = {
                "type": "http", "method": "GET", "headers": [],
                "asgi": {"spec_version": "2.4"},
            }

            async def receive() -> Message:
                await anyio.sleep_forever()
                return {"type": "http.disconnect"}

            async def send(message: Message) -> None:
                if message["type"] == "http.response.body":
                    self.assertTrue(message["body"])
                    self.assertTrue(path.is_file())
                    raise OSError("client disconnected")

            with anyio.fail_after(5), self.assertRaisesRegex(OSError, "client disconnected"):
                await response(scope, receive, send)
            self.assertFalse(path.exists())
            self.assertEqual(list(self.zip_dir.iterdir()), [])

        anyio.run(fail_transfer)

    def test_zip_creation_failure_removes_temporary_file(self) -> None:
        missing = ArchiveItem(self.media_dir / "missing.wav", "03_存在しない.wav")
        with self.assertRaises(FileNotFoundError):
            archive_response((*self.items, missing), zip_name=self.zip_name)
        self.assertEqual(list(self.zip_dir.iterdir()), [])

    def test_interrupted_zip_creation_removes_temporary_file(self) -> None:
        with patch("backend.files.zipfile.ZipFile.write", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                archive_response(self.items, zip_name=self.zip_name)
        self.assertEqual(list(self.zip_dir.iterdir()), [])

    def test_duplicate_archive_names_are_rejected_before_creating_zip(self) -> None:
        duplicate = ArchiveItem(self.second_track, self.items[0].arcname)
        with self.assertRaisesRegex(ValueError, "重複"):
            archive_response((self.items[0], duplicate), zip_name=self.zip_name)
        self.create_temporary.assert_not_called()
        self.assertEqual(list(self.zip_dir.iterdir()), [])

    def test_unsafe_archive_names_are_rejected_before_creating_zip(self) -> None:
        for arcname in (
            "../escape.wav", "..\\escape.wav", "/absolute.wav", "C:\\absolute.wav",
            "folder/track.wav", "folder\\track.wav", "..", ".", "",
            " leading.wav", "trailing.wav ", "trailing.wav.",
            "A\0B.wav", "A\nB.wav", "A\x7fB.wav", "A\u202eB.wav",
            "A\u200bB.wav", "A\u2028B.wav", "A\u2029B.wav",
        ):
            with self.subTest(arcname=arcname), self.assertRaises(ValueError):
                archive_response((ArchiveItem(self.track, arcname),), zip_name=self.zip_name)
        self.create_temporary.assert_not_called()
        self.assertEqual(list(self.zip_dir.iterdir()), [])

    def test_archive_item_is_frozen(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            self.items[0].arcname = "changed.wav"


if __name__ == "__main__":
    unittest.main()
