"""Unified file ingress: temporary storage, fake QQ/CDN and synthetic documents."""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import zipfile

import httpx
import pytest

from agent.session import AgentSession
from attachments.documents import read_document
from attachments.inputs import AUTO_ROUND_CHARS, attachment_context, safe_filename
from attachments.manager import FileManager
from plugins.bundled.qqbot.adapter import QQAdapter
import plugins.bundled.qqbot.attachments as downloads
from web.server import create_app
from web.settings import WebSettings


@pytest.fixture
def fm(tmp_path, monkeypatch):
    monkeypatch.setattr(FileManager, "_instance", None)
    return FileManager(str(tmp_path / "records.db"))


async def upload(fm, root, name, data):
    path = root / "stage"
    path.write_bytes(data)
    return await fm.upload_file_path(path, filename=name)


async def test_local_documents_use_date_directories_without_remote_config(fm, tmp_path):
    record = await upload(fm, tmp_path, "报告.txt", "正文".encode())
    parts = Path(record["path"]).parts
    assert parts[:2] == ("data", "files")
    assert parts[2] == datetime.fromtimestamp(record["created_time"]).strftime("%Y-%m-%d")
    assert parts[-1].endswith("_报告.txt")
    assert record["file_id"] is None
    assert (tmp_path / record["path"]).read_text() == "正文"
    again = await upload(fm, tmp_path, "again.txt", "正文".encode())
    assert again["local_id"] == record["local_id"]
    assert await fm.delete_file(record["local_id"])
    assert not (tmp_path / record["path"]).exists()


async def test_image_storage_is_dated_and_legacy_paths_still_read(fm, tmp_path):
    fm._configured = True
    fm._uploader = SimpleNamespace(name="fake", connection_scope="", base_url="fake")
    fm._upload_remote = AsyncMock()
    data = b"\x89PNG\r\n\x1a\nsynthetic"
    record = await upload(fm, tmp_path, "photo.png", data)
    assert len(Path(record["path"]).parts) == 4
    fm._upload_remote.assert_awaited_once()
    # No directory relocation/migration is needed to resolve an older record.
    legacy = tmp_path / "data/files/legacy.png"
    legacy.write_bytes(data)
    stored = fm._db_get(record["local_id"])
    stored.update(local_id="legacy", path="data/files/legacy.png", sha256="legacy-sha")
    fm._db_insert(stored)
    assert (await fm.get_image_data_url("legacy")).startswith("data:image/png;base64,")


async def test_filename_traversal_collisions_and_unsupported_media(fm, tmp_path):
    first = await upload(fm, tmp_path, "../../unsafe[1].bin", b"one")
    second = await upload(fm, tmp_path, "../../unsafe[1].bin", b"two")
    assert first["path"] != second["path"]
    assert ".." not in Path(first["path"]).parts and "[" not in first["path"]
    assert (tmp_path / first["path"]).resolve().is_relative_to(tmp_path / "data/files")
    vector = await upload(fm, tmp_path, "drawing.svg", b"<svg/>")
    assert vector["mime"] == "application/octet-stream"
    with pytest.raises(ValueError, match="图片后缀"):
        await upload(fm, tmp_path, "wrong.png", b"not a png")
    assert len(safe_filename("超长" * 200 + ".md").encode()) < 200


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "gb18030"])
async def test_text_context_has_only_relative_marker_and_complete_body(fm, tmp_path, encoding):
    record = await upload(fm, tmp_path, "说明.md", "你好\n第二行".encode(encoding))
    text, _ = attachment_context(record, tmp_path, AUTO_ROUND_CHARS)
    assert text == f'[file:{record["path"]}]\n你好\n第二行'
    assert str(tmp_path) not in text and "大小" not in text and "MIME" not in text


async def test_large_text_and_binary_use_marker_without_partial_expansion(fm, tmp_path):
    for name, data in [("large.txt", b"x" * 65537), ("binary.txt", b"a\x00b"), ("archive.zip", b"PK-opaque")]:
        record = await upload(fm, tmp_path, name, data)
        assert attachment_context(record, tmp_path, AUTO_ROUND_CHARS)[0] == f'[file:{record["path"]}]'


async def test_model_message_and_archive_use_minimal_markers(fm, tmp_path):
    record = await upload(fm, tmp_path, "report.md", b"full synthetic contents")
    agent = AgentSession.__new__(AgentSession)
    agent._loop = SimpleNamespace(model_config=None)
    prepared, notes = await agent._prepare_user_media("question", [record])
    message = agent._build_model_user_message("question", "web", prepared)
    assert message["content"][1] == {"type": "text", "text": f'[file:{record["path"]}]\nfull synthetic contents'}
    assert all(block["type"] == "text" for block in message["content"])
    archive = agent._build_user_archive("question", "web", prepared, notes)
    assert archive["content"] == f'question\n[file:{record["path"]}]'
    assert archive["images"][0]["path"] == record["path"]


async def test_web_upload_generic_file_without_cloud_configuration(fm, tmp_path, monkeypatch):
    monkeypatch.setattr("web.server.get_file_manager", lambda: fm)
    monkeypatch.setattr("web.server.__file__", str(tmp_path / "web/server.py"))
    agent = SimpleNamespace(run_round=AsyncMock(return_value=SimpleNamespace(total_usage={}, stop_reason="complete")))
    settings = WebSettings(str(tmp_path / "settings.json"))
    app = create_app(None, None, agent, None, None, SimpleNamespace(db_path=fm.db_path), settings, None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://synthetic") as client:
        response = await client.post("/api/files/upload", files={"file": ("report.pdf", b"%PDF-synthetic", "application/pdf")})
        assert response.status_code == 200
        record = response.json()["file"]
        assert record["file_id"] is None
        response = await client.post("/api/chat/send", json={"text": "", "files": [record["local_id"]]})
        assert response.status_code == 200
        assert agent.run_round.call_args.kwargs["images"][0]["path"] == record["path"]
        settings.set_password("synthetic")
        response = await client.get(f'/api/files/{record["local_id"]}/content')
        assert response.status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.issue_token()
        response = await client.get(f'/api/files/{record["local_id"]}/content')
        assert response.status_code == 200
        assert response.content == b"%PDF-synthetic"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["x-content-type-options"] == "nosniff"


class Response:
    def __init__(self, data=b"synthetic", status=200, headers=None):
        self.data, self.status, self.headers = data, status, headers or {}
        self.content_length = len(data)
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        yield self.data


class HTTP:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


async def test_qq_attachment_only_message_uses_shared_ingress(fm, tmp_path, monkeypatch):
    monkeypatch.setattr(QQAdapter, "_load_latest_chat", lambda self: None)
    monkeypatch.setattr("plugins.bundled.qqbot.adapter.get_file_manager", lambda: fm)
    session = SimpleNamespace(run_round=AsyncMock(return_value=SimpleNamespace(stop_reason="complete")))
    adapter = QQAdapter(session, default_to="synthetic-user")
    adapter._save_latest_chat = Mock()
    adapter._http = HTTP(Response(b"hello from QQ"))
    await adapter._handle_c2c_message({"id": "synthetic-id", "author": {"user_openid": "synthetic-user"},
        "content": "", "attachments": [{"url": "https://files.qq.com/report", "filename": "report.txt"}]})
    session.run_round.assert_awaited_once()
    record = session.run_round.call_args.kwargs["images"][0]
    assert (tmp_path / record["path"]).read_bytes() == b"hello from QQ"
    assert adapter._http.calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "file:///etc/passwd", "https://qq.com.evil.test/x", "https://qq.com@evil.test/x"])
async def test_qq_download_rejects_non_cdn_targets(url):
    http = HTTP()
    with pytest.raises(ValueError):
        await downloads.receive_attachments([{"url": url}], http, None)
    assert not http.calls


async def test_qq_redirect_and_stream_size_are_checked(monkeypatch):
    http = HTTP(Response(status=302, headers={"Location": "http://169.254.169.254/metadata"}))
    with pytest.raises(ValueError, match="官方 CDN"):
        await downloads.receive_attachments([{"url": "https://files.qq.com/x"}], http, None)
    monkeypatch.setattr(downloads, "MAX_FILE_SIZE", 4)
    response = Response(b"too large")
    response.content_length = None
    with pytest.raises(ValueError, match="64 MiB"):
        await downloads.receive_attachments([{"url": "https://files.qq.com/x"}], HTTP(response), None)


def test_read_document_text_pagination(tmp_path):
    path = tmp_path / "sample.md"
    path.write_text("abcdef", encoding="utf-8")
    first = read_document(str(path), limit=3)
    assert first == {"content": "abc", "next_offset": 3}
    assert read_document(str(path), offset=3, limit=3) == {"content": "def", "next_offset": None}
    with pytest.raises(PermissionError):
        read_document(str(tmp_path / ".env"))


def test_document_worker_imports_package_from_a_different_working_directory(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.txt"
    path.write_text("跨目录解析😀", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert read_document(str(path))["content"] == "跨目录解析😀"


def office_file(path, entries):
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in entries.items():
            archive.writestr(name, value)


def test_read_docx_and_xlsx_without_executing_embedded_content(tmp_path):
    doc = tmp_path / "sample.docx"
    office_file(doc, {"word/document.xml": '<document xmlns="urn:word"><body><p><r><t>Hello document</t></r></p></body></document>'})
    assert "Hello document" in read_document(str(doc))["content"]
    book = tmp_path / "sample.xlsx"
    office_file(book, {
        "xl/workbook.xml": '<workbook xmlns="urn:sheet" xmlns:r="urn:relation"><sheets><sheet name="Table" r:id="r1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": '<worksheet xmlns="urn:sheet"><sheetData><row><c r="A1" t="inlineStr"><is><t>Hello</t></is></c><c r="C1"><v>42</v></c></row></sheetData></worksheet>',
    })
    assert read_document(str(book))["content"] == "[Table]\nHello\t\t42\n"


def test_read_pdf_text_and_scanned_fallback(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    path = tmp_path / "sample.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 30 200 Td (Synthetic PDF text) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(path)
    assert "Synthetic PDF text" in read_document(str(path))["content"]
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(path)
    with pytest.raises(ValueError, match="OCR"):
        read_document(str(path))


def test_corrupt_documents_and_invalid_pagination(tmp_path):
    path = tmp_path / "broken.docx"
    path.write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="文档"):
        read_document(str(path))
    with pytest.raises(ValueError, match="offset"):
        read_document(str(path), offset=-1)
