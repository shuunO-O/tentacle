"""Read local documents and raster images in a bounded, passive worker."""

import asyncio
import gzip
import json
from runtime.paths import APP_ROOT
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

from attachments.inputs import TEXT_SUFFIXES, decode_text
from attachments.outgoing import open_outgoing_file
from attachments.document_formats import (
    DOCX_SUFFIXES, XLSX_SUFFIXES, PPTX_SUFFIXES, ODF_SUFFIXES, FLAT_ODF_SUFFIXES,
    IMAGE_SUFFIXES, MAX_ENTRIES, archive_path, archive_units, email_units, epub_units, odf_units,
    presentation_units, rtf_text,
)

MAX_BYTES = 64 * 1024 * 1024
MAX_CHARS = 20000
READ_SLOTS = asyncio.Semaphore(1)


def _office_archive(source):
    archive = zipfile.ZipFile(source)
    if (len(archive.infolist()) > MAX_ENTRIES
            or sum(item.file_size for item in archive.infolist()) > MAX_BYTES):
        archive.close()
        raise ValueError("文档解压后超过 64 MiB 或 10000 条目上限")
    return archive


def _units(source, suffix):
    if suffix in TEXT_SUFFIXES:
        yield _plain_text(source)
    elif suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(source)
        if reader.is_encrypted:
            raise ValueError("PDF 已加密，请提供解密后的文件")
        found = False
        for index, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            if text.strip():
                found = True
                yield f"[第 {index} 页]\n{text}\n"
        if not found:
            raise ValueError("PDF 未提取到文字，可能是扫描件，需要 OCR")
    elif suffix in DOCX_SUFFIXES:
        with _office_archive(source) as archive:
            with archive.open("word/document.xml") as xml:
                for _, element in ET.iterparse(xml, events=("end",)):
                    if element.tag.rsplit("}", 1)[-1] == "p":
                        yield "".join(element.itertext()) + "\n"
                        element.clear()
    elif suffix in XLSX_SUFFIXES:
        with _office_archive(source) as archive:
            strings = []
            if "xl/sharedStrings.xml" in archive.namelist():
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                strings = ["".join(item.itertext()) for item in root]
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            relations = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {item.attrib["Id"]: item.attrib["Target"] for item in relations
                       if item.attrib.get("TargetMode") != "External"}
            for sheet in workbook.findall(".//{*}sheet"):
                relation = next((v for k, v in sheet.attrib.items() if k.endswith("}id")), "")
                target = targets.get(relation, "")
                if not target:
                    continue
                name = archive_path("" if target.startswith("/") else "xl", target)
                yield f"[{sheet.attrib.get('name', 'Sheet')}]\n"
                with archive.open(name) as xml:
                    for _, element in ET.iterparse(xml, events=("end",)):
                        if element.tag.rsplit("}", 1)[-1] != "row":
                            continue
                        cells = []
                        for cell in element:
                            ref = cell.attrib.get("r", "")
                            column = 0
                            for char in ref:
                                if not char.isalpha():
                                    break
                                column = column * 26 + ord(char.upper()) - 64
                            if column > 1024:
                                raise ValueError("工作表列数超过读取上限")
                            while len(cells) < max(column - 1, 0):
                                cells.append("")
                            value = cell.findtext("{*}v", default="")
                            if cell.attrib.get("t") == "s":
                                value = strings[int(value)]
                            elif cell.attrib.get("t") == "inlineStr":
                                value = "".join(cell.itertext())
                            elif not value and cell.find("{*}f") is not None:
                                value = "=" + cell.findtext("{*}f", default="")
                            cells.append(value)
                        yield "\t".join(cells) + "\n"
                        element.clear()
    elif suffix in PPTX_SUFFIXES:
        with _office_archive(source) as archive:
            yield from presentation_units(archive)
    elif suffix in ODF_SUFFIXES:
        with _office_archive(source) as archive:
            with archive.open("content.xml") as xml:
                yield from odf_units(xml)
    elif suffix in FLAT_ODF_SUFFIXES:
        yield from odf_units(source)
    elif suffix == ".epub":
        with _office_archive(source) as archive:
            yield from epub_units(archive)
    elif suffix in {".eml", ".mht", ".mhtml"}:
        yield from email_units(source)
    elif suffix == ".rtf":
        yield rtf_text(source)
    elif suffix in {".zip", ".tar", ".tgz", ".tbz2", ".txz", ".tar.gz", ".tar.bz2", ".tar.xz"}:
        yield from archive_units(source, suffix)
    elif suffix == ".gz":
        with gzip.GzipFile(fileobj=source) as expanded:
            yield _plain_text(expanded)
    elif suffix in {".doc", ".xls", ".ppt", ".7z", ".rar", ".heic", ".heif"}:
        raise ValueError("此格式暂不支持，请先转换为 DOCX/XLSX/PPTX、文本、ZIP 或 PNG/JPEG")
    else:
        # Extensionless files (README, Makefile, Dockerfile) and unfamiliar code
        # extensions still work, but undecodable/binary data is never fabricated.
        yield _plain_text(source)


def _plain_text(source):
    data = source.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("文本解压/读取后超过 64 MiB 上限")
    try:
        text = decode_text(data)
    except (UnicodeError, ValueError):
        raise ValueError("不支持的二进制格式或文本编码；请先转换为支持的文档/文本格式") from None
    if any(ord(char) < 32 and char not in "\t\n\r\f" for char in text):
        raise ValueError("文件包含二进制控制字符，不能作为文本读取")
    return text


def _extract(source, suffix, offset, limit):
    parts = []
    remaining = limit + 1
    for unit in _units(source, suffix):
        if offset >= len(unit):
            offset -= len(unit)
            continue
        piece = unit[offset:offset + remaining]
        offset = 0
        parts.append(piece)
        remaining -= len(piece)
        if remaining <= 0:
            break
    text = "".join(parts)
    return text[:limit], len(text) > limit


def read_document(path: str, offset=0, limit=MAX_CHARS, frame=0):
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset 必须是非负整数")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_CHARS:
        raise ValueError("limit 必须在 1–20000 之间")
    if isinstance(frame, bool) or not isinstance(frame, int) or not 0 <= frame <= 10000:
        raise ValueError("frame 必须是 0–10000 之间的整数")
    target, stream, metadata = open_outgoing_file(path)
    with stream:
        if metadata.st_size > MAX_BYTES:
            raise ValueError("文档超过 64 MiB 上限")
        suffix = target.suffix.lower()
        for combined in (".tar.gz", ".tar.bz2", ".tar.xz"):
            if target.name.lower().endswith(combined):
                suffix = combined
                break
        try:
            result = subprocess.run([sys.executable, "-m", "attachments.documents", "--worker",
                                     suffix, str(offset), str(limit), str(frame)],
                                    cwd=APP_ROOT,
                                    stdin=stream, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
        except subprocess.TimeoutExpired:
            raise ValueError("文档读取超时，请使用更小的文件") from None
    if result.returncode:
        raise ValueError("文档解析失败或超过资源限制")
    payload = json.loads(result.stdout)
    if "error" in payload:
        raise ValueError(payload["error"])
    if "image" in payload:
        return payload
    return {"content": payload["content"], "next_offset": offset + len(payload["content"]) if payload["more"] else None}


def _worker():
    # Set limits inside the child; no preexec_fn in the threaded application.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    except ImportError:
        pass
    try:
        suffix, offset, limit, frame = sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
        header = sys.stdin.buffer.peek(32)[:32]
        image_header = (header.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a",
                                           b"BM", b"II*\x00", b"MM\x00*", b"\x00\x00\x01\x00", b"8BPS"))
                        or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")
                        or header[4:12] in (b"ftypavif", b"ftypavis"))
        if suffix in IMAGE_SUFFIXES or image_header:
            if offset:
                raise ValueError("图片不支持字符 offset；多帧/多页图片请使用 frame")
            from attachments.image_reader import image_preview
            payload = image_preview(sys.stdin.buffer, frame)
        else:
            if frame:
                raise ValueError("frame 仅用于图片帧/页，文本和文档请使用 offset")
            text, more = _extract(sys.stdin.buffer, suffix, offset, limit)
            payload = {"content": text, "more": more}
    except ImportError:
        payload = {"error": "缺少文件读取依赖，请安装 requirements.txt"}
    except ValueError as exc:
        payload = {"error": str(exc)}
    except Exception:
        payload = {"error": "文档损坏、不支持或超过读取资源限制"}
    print(json.dumps(payload, ensure_ascii=True))


if __name__ == "__main__" and len(sys.argv) == 6 and sys.argv[1] == "--worker":
    _worker()
