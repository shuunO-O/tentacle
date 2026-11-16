"""Synthetic local files only; exercise the real bounded reader and file store."""

from copy import deepcopy
from email.message import EmailMessage
import gzip
from io import BytesIO
import json
import os
from pathlib import Path
import struct
import subprocess
import tarfile
from unittest.mock import AsyncMock
import zipfile
import zlib

from PIL import Image
import pytest

from agent.protocol import Context, ModelConfig
from attachments.documents import read_document
import attachments.manager as files
from dialects import get_dialect
from runtime.tool_registry import ToolRegistry
from tools.file_read import file_read, register_tools


def package(path, entries):
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in entries.items():
            archive.writestr(name, text)
    return path


@pytest.mark.parametrize("name,encoding", [
    ("README", "utf-8"), ("Makefile", "gb18030"), ("Dockerfile", "utf-32"), ("main.rs", "utf-8"),
    ("view.tsx", "utf-8"), ("main.unknown", "gb18030"), ("diagram.svg", "utf-8"),
])
def test_text_detection_and_pagination(tmp_path, name, encoding):
    path = tmp_path / name
    path.write_bytes("你好，synthetic\n第二行".encode(encoding))
    first = read_document(str(path), limit=3)
    assert first == {"content": "你好，", "next_offset": 3}
    assert read_document(str(path), offset=3)["content"] == "synthetic\n第二行"


@pytest.mark.parametrize("name", ["data.bin", "text.txt", "unknown"])
def test_binary_is_not_passed_as_text(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"\x01\x02\x00\xffdata")
    with pytest.raises(ValueError, match="二进制"):
        read_document(str(path))


@pytest.mark.parametrize("suffix", [".pptx", ".pptm", ".potx", ".ppsx"])
def test_presentation_order_and_external_links_are_not_followed(tmp_path, suffix):
    path = package(tmp_path / ("slides" + suffix), {
        "ppt/presentation.xml": '<p:presentation xmlns:p="urn:p" xmlns:r="urn:r"><p:sldIdLst>'
                                '<p:sldId r:id="second"/><p:sldId r:id="first"/>'
                                '<p:sldId r:id="remote"/></p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": '<Relationships><Relationship Id="first" Target="slides/slide1.xml"/>'
                                          '<Relationship Id="second" Target="/ppt/slides/slide2.xml"/>'
                                          '<Relationship Id="remote" Target="https://invalid/slide.xml" '
                                          'TargetMode="External"/></Relationships>',
        "ppt/slides/slide1.xml": '<slide xmlns:a="urn:drawing"><a:p><a:r><a:t>One</a:t></a:r></a:p></slide>',
        "ppt/slides/slide2.xml": '<slide xmlns:a="urn:drawing"><a:p><a:r><a:t>Two</a:t></a:r>'
                               '<a:r><a:t> parts</a:t></a:r></a:p></slide>',
        "ppt/vbaProject.bin": b"not executable",
    })
    result = read_document(str(path))["content"]
    assert result == "[幻灯片 1]\nTwo parts\n[幻灯片 2]\nOne\n"
    assert "not executable" not in result


ODF_HEADER = ('<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
              'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
              'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
              'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"><office:body>')


@pytest.mark.parametrize("suffix", [".odt", ".ott", ".fodt"])
def test_opendocument_paragraphs_and_whitespace(tmp_path, suffix):
    xml = (ODF_HEADER + '<office:text><text:h>Heading</text:h><text:p>Hello<text:s text:c="2"/>world'
           '<text:tab/>a<text:line-break/>b</text:p></office:text></office:body></office:document>')
    path = tmp_path / ("document" + suffix)
    if suffix == ".fodt":
        path.write_text(xml, encoding="utf-8")
    else:
        package(path, {"content.xml": xml})
    assert read_document(str(path))["content"] == "Heading\nHello  world\ta\nb\n"


def test_opendocument_spreadsheets_use_cached_values_and_repeats(tmp_path):
    xml = (ODF_HEADER + '<office:spreadsheet><table:table table:name="Sheet">'
           '<table:table-row table:number-rows-repeated="2"><table:table-cell '
           'table:number-columns-repeated="2"><text:p>你好</text:p></table:table-cell>'
           '<table:table-cell table:formula="of:=WEBSERVICE(&quot;https://invalid&quot;)" '
           'office:value="42"/></table:table-row></table:table></office:spreadsheet>'
           '</office:body></office:document>')
    path = package(tmp_path / "book.ods", {"content.xml": xml})
    assert read_document(str(path))["content"] == "[Sheet]\n你好\t你好\t42\n你好\t你好\t42\n"
    package(path, {"content.xml": xml.replace('repeated="2"', 'repeated="9999999"')})
    with pytest.raises(ValueError, match="上限"):
        read_document(str(path))


def test_opendocument_presentation(tmp_path):
    path = package(tmp_path / "slides.odp", {"content.xml": ODF_HEADER + '<office:presentation>'
                   '<draw:page><text:p>First</text:p></draw:page><draw:page><text:p>Second</text:p>'
                   '</draw:page></office:presentation></office:body></office:document>'})
    assert read_document(str(path))["content"] == "[幻灯片 1]\nFirst\n[幻灯片 2]\nSecond\n"


def test_ods_trims_unbounded_trailing_blank_cells_and_rows(tmp_path):
    xml = (ODF_HEADER + '<office:spreadsheet><table:table table:name="Data"><table:table-row>'
           '<table:table-cell/><table:table-cell><text:p>value</text:p></table:table-cell>'
           '<table:table-cell table:number-columns-repeated="16382"/></table:table-row>'
           '<table:table-row table:number-rows-repeated="1048575"><table:table-cell '
           'table:number-columns-repeated="16384"/></table:table-row></table:table>'
           '</office:spreadsheet></office:body></office:document>')
    path = package(tmp_path / "typical.ods", {"content.xml": xml})
    assert read_document(str(path))["content"] == "[Data]\n\tvalue\n"


def test_epub_uses_spine_and_only_visible_text(tmp_path):
    path = package(tmp_path / "book.epub", {
        "META-INF/container.xml": '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>',
        "OPS/book.opf": '<package><manifest><item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>'
                        '<item id="b" href="b.xhtml" media-type="application/xhtml+xml"/></manifest>'
                        '<spine><itemref idref="b"/><itemref idref="a"/></spine></package>',
        "OPS/a.xhtml": '<html><head><title>Hidden</title></head><body><p>A &amp; B</p></body></html>',
        "OPS/b.xhtml": '<html><body><script>hidden()</script><p>First</p><img src="https://invalid"/></body></html>',
    })
    assert read_document(str(path))["content"] == "[章节 1]\nFirst\n[章节 2]\nA & B\n"


def test_eml_decodes_headers_and_chooses_one_body_not_attachments(tmp_path):
    message = EmailMessage()
    message["Subject"] = "合成邮件"
    message["From"] = "synthetic@example.test"
    message.set_content("Plain body")
    message.add_alternative("<p>Duplicate HTML</p>", subtype="html")
    message.add_attachment(b"private-looking synthetic attachment", maintype="application", subtype="octet-stream",
                           filename="example.bin")
    path = tmp_path / "mail.eml"
    path.write_bytes(message.as_bytes())
    text = read_document(str(path))["content"]
    assert "Subject: 合成邮件" in text and "Plain body" in text
    assert "Duplicate" not in text and "attachment" not in text


def test_mhtml_extracts_html_without_loading_resources(tmp_path):
    path = tmp_path / "page.mhtml"
    path.write_bytes(b'MIME-Version: 1.0\nContent-Type: text/html; charset=utf-8\n\n'
                     b'<style>hidden</style><p>Visible</p><script>execute()</script><img src="file:///invalid"/>')
    text = read_document(str(path))["content"]
    assert "Visible" in text and "execute" not in text and "hidden" not in text


@pytest.mark.parametrize("suffix", [".zip", ".tar", ".tar.gz", ".tar.bz2", ".tar.xz"])
def test_archive_directory_does_not_extract_or_read_member_data(tmp_path, suffix):
    path = tmp_path / ("archive" + suffix)
    if suffix == ".zip":
        package(path, {"../outside.txt": "member body"})
    else:
        mode = {".tar.gz": "w:gz", ".tar.bz2": "w:bz2", ".tar.xz": "w:xz"}.get(suffix, "w")
        with tarfile.open(path, mode) as archive:
            info = tarfile.TarInfo("../outside.txt")
            info.size = len(b"member body")
            archive.addfile(info, BytesIO(b"member body"))
    text = read_document(str(path))["content"]
    assert "../outside.txt" in text and "member body" not in text
    assert not (tmp_path.parent / "outside.txt").exists()


def test_gzip_text(tmp_path):
    path = tmp_path / "notes.txt.gz"
    path.write_bytes(gzip.compress("压缩文本".encode()))
    assert read_document(str(path), limit=2) == {"content": "压缩", "next_offset": 2}


@pytest.mark.parametrize("data,expected", [
    (br"{\rtf1\ansi Hello\par \u20320?\u22909?}", "Hello\n你好"),
    (b"{\\rtf1\\ansi\\ansicpg1252 caf\xe9}", "café"),
    (br"{\rtf1\ansi before{\*\objdata 1234}after}", "beforeafter"),
])
def test_rtf_unicode_codepages_and_embedded_objects(tmp_path, data, expected):
    path = tmp_path / "document.rtf"
    path.write_bytes(data)
    assert read_document(str(path))["content"] == expected


@pytest.mark.parametrize("suffix", [".docm", ".dotx"])
def test_word_variants(tmp_path, suffix):
    path = package(tmp_path / ("document" + suffix), {"word/document.xml":
                   '<document xmlns="urn:word"><body><p><r><t>Document</t></r></p></body></document>'})
    assert read_document(str(path))["content"] == "Document\n"


@pytest.mark.parametrize("suffix", [".xlsm", ".xltm"])
def test_spreadsheet_variants_use_cache_and_ignore_external_sheets(tmp_path, suffix):
    path = package(tmp_path / ("book" + suffix), {
        "xl/workbook.xml": '<workbook xmlns="urn:sheet" xmlns:r="urn:r"><sheets>'
                           '<sheet name="Data" r:id="data"/><sheet name="Remote" r:id="remote"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="data" Target="/xl/worksheets/sheet1.xml"/>'
                                     '<Relationship Id="remote" Target="https://invalid" TargetMode="External"/>'
                                     '</Relationships>',
        "xl/worksheets/sheet1.xml": '<worksheet xmlns="urn:sheet"><sheetData><row><c r="A1">'
                                    '<f>WEBSERVICE("https://invalid")</f><v>42</v></c></row></sheetData></worksheet>',
    })
    assert read_document(str(path))["content"] == "[Data]\n42\n"


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(files.FileManager, "_instance", None)
    return files.FileManager(str(tmp_path / "records.db"))  # Deliberately unconfigured.


@pytest.mark.parametrize("suffix,fmt", [
    (".png", "PNG"), (".jpg", "JPEG"), (".gif", "GIF"), (".webp", "WEBP"), (".bmp", "BMP"),
    (".tiff", "TIFF"), (".ico", "ICO"), (".ppm", "PPM"), (".tga", "TGA"), (".avif", "AVIF"),
])
async def test_image_read_uses_local_store_without_api_configuration(manager, tmp_path, suffix, fmt):
    path = tmp_path / ("sample" + suffix)
    Image.new("RGB", (32, 32), "red").save(path, format=fmt)
    manager._upload_remote = AsyncMock(side_effect=AssertionError("must not upload"))
    register_tools(ToolRegistry())
    result = await ToolRegistry().execute("file_read", {"path": str(path)})
    assert isinstance(result, list)
    assert result[0]["type"] == "text" and "32×32" in result[0]["text"]
    assert result[1]["type"] == "image" and result[1]["mime"] == "image/png"
    assert "file_id" not in result[1]
    url = await manager.image_block_to_data_url(result[1])
    assert url.startswith("data:image/png;base64,")
    stored = await manager.get_file(result[1]["local_id"])
    assert Path(stored["path"]).parts[:2] == ("data", "files")
    assert not Path(stored["path"]).is_absolute()
    assert (await file_read({"path": str(path)}))[1]["local_id"] == result[1]["local_id"]
    manager._upload_remote.assert_not_awaited()


async def test_extensionless_image_and_relative_path(manager, tmp_path):
    Image.new("RGB", (8, 8)).save(tmp_path / "preview", format="PNG")
    assert (await file_read({"path": "preview"}))[1]["type"] == "image"


def test_oversized_image_header_is_rejected_without_allocating_pixels(tmp_path):
    path = tmp_path / "oversized.png"
    data = struct.pack(">IIBBBBB", 8000, 8000, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + data
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(data)) + chunk
                     + struct.pack(">I", zlib.crc32(chunk)) + b"\x00\x00\x00\x00IEND\xaeB`\x82")
    with pytest.raises(ValueError, match="损坏|资源|像素"):
        read_document(str(path))


def test_reader_limits_and_timeout(tmp_path, monkeypatch):
    import attachments.documents as documents
    path = tmp_path / "sample.txt"
    path.write_text("0123456789")
    monkeypatch.setattr(documents, "MAX_BYTES", 4)
    with pytest.raises(ValueError, match="64 MiB"):
        read_document(str(path))
    path.write_text("x")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 20)

    monkeypatch.setattr(documents.subprocess, "run", timeout)
    with pytest.raises(ValueError, match="超时"):
        read_document(str(path))


def test_office_expansion_and_entry_limits(tmp_path, monkeypatch):
    import attachments.documents as documents
    path = package(tmp_path / "archive.docx", {"a": "12345", "b": "67"})
    monkeypatch.setattr(documents, "MAX_BYTES", 6)
    with pytest.raises(ValueError, match="上限"):
        documents._office_archive(path)
    monkeypatch.setattr(documents, "MAX_BYTES", 100)
    monkeypatch.setattr(documents, "MAX_ENTRIES", 1)
    with pytest.raises(ValueError, match="上限"):
        documents._office_archive(path)


def test_image_frame_selection_resize_and_invalid_inputs(tmp_path):
    path = tmp_path / "pages.tiff"
    Image.new("RGB", (2400, 12), "red").save(path, save_all=True,
                                           append_images=[Image.new("RGB", (24, 24), "blue")])
    first = read_document(str(path))
    assert "2400×12" in first["description"] and "2048×10" in first["description"]
    second = read_document(str(path), frame=1)
    import base64
    with Image.open(BytesIO(base64.b64decode(second["image"]))) as image:
        assert image.getpixel((0, 0)) == (0, 0, 255)
    for args, match in [({"frame": 2}, "frame"), ({"frame": True}, "frame"), ({"offset": 1}, "offset")]:
        with pytest.raises(ValueError, match=match):
            read_document(str(path), **args)
    path.write_bytes(b"not really an image")
    with pytest.raises(ValueError, match="损坏"):
        read_document(str(path))


def test_image_preview_applies_orientation_and_drops_exif(tmp_path):
    import base64
    path = tmp_path / "oriented.jpg"
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "synthetic metadata"
    Image.new("RGB", (12, 8), "red").save(path, exif=exif)
    result = read_document(str(path))
    with Image.open(BytesIO(base64.b64decode(result["image"]))) as image:
        assert image.size == (8, 12)
        assert not image.getexif()
    assert "synthetic metadata" not in result["description"]


@pytest.mark.parametrize("style", ["chat", "responses", "interactions", "gemini_generate"])
@pytest.mark.parametrize("vision", [True, False])
async def test_file_read_images_reach_each_dialect(manager, tmp_path, style, vision):
    path = tmp_path / "example.png"
    Image.new("RGB", (20, 10), "red").save(path)
    blocks = await file_read({"path": str(path)})
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "read1", "function": {"name": "file_read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "read1", "content": blocks},
    ]
    original = deepcopy(messages)
    context = Context("", [], "", "test", image_resolver=manager.image_block_to_data_url)
    cfg = ModelConfig("synthetic-model", "https://model.test", api_style=style, vision=vision)
    dialect = get_dialect(style)
    if style == "chat":
        body = await dialect._prepare_messages(cfg, messages, context)
        assert isinstance(body[1]["content"], str)
        if vision:
            assert [msg["role"] for msg in body] == ["assistant", "tool", "user"]
            assert body[2]["content"][1]["type"] == "image_url"
    elif style == "responses":
        body = await dialect._to_responses_input(messages, cfg, context)
    else:
        body = await dialect._request(cfg, messages, None, context)
    wire = json.dumps(body)
    assert "local_id" not in wire and "read-preview.png" not in wire
    assert ("base64" in wire or '"data"' in wire) == vision
    assert messages == original


async def test_chat_defers_images_until_all_parallel_tool_replies(manager, tmp_path):
    path = tmp_path / "example.png"
    Image.new("RGB", (8, 8)).save(path)
    blocks = await file_read({"path": str(path)})
    messages = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": call, "function": {"name": "file_read", "arguments": "{}"}} for call in ("a", "b")]},
        {"role": "tool", "tool_call_id": "a", "content": blocks},
        {"role": "tool", "tool_call_id": "b", "content": "second result"},
        {"role": "assistant", "content": "I see it"}]
    cfg = ModelConfig("m", "https://model.test", vision=True)
    body = await get_dialect("chat")._prepare_messages(cfg, messages, Context(
        "", [], "", "test", image_resolver=manager.image_block_to_data_url))
    assert [msg["role"] for msg in body] == ["assistant", "tool", "tool", "user", "assistant"]


async def test_protected_image_is_not_opened_or_persisted(manager, tmp_path):
    path = tmp_path / "secrets" / "image.png"
    path.parent.mkdir()
    Image.new("RGB", (8, 8)).save(path)
    with pytest.raises(PermissionError):
        await file_read({"path": str(path)})
    assert not (tmp_path / "data/files").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows file-handle verification")
def test_windows_open_rejects_directories_hardlinks_and_path_replacement(tmp_path, monkeypatch):
    import _winapi
    from attachments.outgoing import open_outgoing_file
    import attachments.windows_files as windows

    folder = tmp_path / "folder"
    folder.mkdir()
    path = folder / "test.txt"
    path.write_text("synthetic public")
    hardlink = tmp_path / "hardlink.txt"
    hardlink.hardlink_to(path)
    with pytest.raises(PermissionError):
        open_outgoing_file(str(hardlink))
    hardlink.unlink()
    with pytest.raises(PermissionError):
        open_outgoing_file(str(folder))
    secret = tmp_path / "secrets"
    secret.mkdir()
    (secret / "test.txt").write_text("synthetic secret")
    original = windows.open_regular_file

    def swap(target):
        folder.rename(tmp_path / "old-folder")
        _winapi.CreateJunction(str(secret), str(folder))
        return original(target)

    monkeypatch.setattr(windows, "open_regular_file", swap)
    with pytest.raises(PermissionError):
        open_outgoing_file(str(path))
