"""Passive text extraction helpers used only inside the document worker."""

from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
import posixpath
import re
import tarfile
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit
import zipfile

from attachments.inputs import decode_text

MAX_ENTRIES = 10000
DOCX_SUFFIXES = {".docx", ".docm", ".dotx", ".dotm"}
XLSX_SUFFIXES = {".xlsx", ".xlsm", ".xltx", ".xltm"}
PPTX_SUFFIXES = {".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm"}
ODF_SUFFIXES = {".odt", ".ott", ".ods", ".ots", ".odp", ".otp"}
FLAT_ODF_SUFFIXES = {".fodt", ".fods", ".fodp"}
IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".webp", ".bmp", ".dib", ".tif", ".tiff",
    ".ico", ".avif", ".ppm", ".pgm", ".pbm", ".pnm", ".tga", ".pcx", ".psd", ".jp2", ".j2k",
}
T = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
DRAW = "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}"


def archive_path(base, target):
    """Resolve an internal package reference; never fetch or open external paths."""
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        raise ValueError("文档包含无效的内部引用")
    value = posixpath.normpath(posixpath.join(base, unquote(parsed.path).lstrip("/")))
    if value.startswith("../") or value == ".." or "\\" in value:
        raise ValueError("文档包含无效的内部引用")
    return value


class VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden.append(tag)
        if not self.hidden and tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "section"}:
            self.parts.append("\n")
        if not self.hidden and tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        if not self.hidden and tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "section"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_text(text):
    parser = VisibleHTML()
    parser.feed(text)
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def presentation_units(archive):
    root = ET.fromstring(archive.read("ppt/presentation.xml"))
    relations = ET.fromstring(archive.read("ppt/_rels/presentation.xml.rels"))
    targets = {item.attrib["Id"]: item.attrib["Target"] for item in relations
               if item.attrib.get("TargetMode") != "External"}
    for index, slide in enumerate(root.findall(".//{*}sldId"), 1):
        relation = next((v for k, v in slide.attrib.items() if k.endswith("}id")), "")
        target = targets.get(relation)
        if not target:
            continue
        name = archive_path("" if target.startswith("/") else "ppt", target)
        yield f"[幻灯片 {index}]\n"
        with archive.open(name) as xml:
            for _, element in ET.iterparse(xml, events=("end",)):
                if element.tag.rsplit("}", 1)[-1] == "p":
                    yield "".join(item.text or "" for item in element.iter() if item.tag.endswith("}t")) + "\n"
                    element.clear()


def odf_text(element):
    parts = [element.text or ""]
    for child in element:
        if child.tag == T + "s":
            count = int(child.get(T + "c", "1"))
            if not 1 <= count <= 20000:
                raise ValueError("文档空白重复数超过读取上限")
            parts.append(" " * count)
        elif child.tag == T + "tab":
            parts.append("\t")
        elif child.tag == T + "line-break":
            parts.append("\n")
        else:
            parts.append(odf_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def odf_units(source):
    root = ET.parse(source).getroot()
    spreadsheet = root.find(".//" + OFFICE + "spreadsheet")
    if spreadsheet is not None:
        for sheet in spreadsheet.findall(TABLE + "table"):
            yield f"[{sheet.get(TABLE + 'name', 'Sheet')}]\n"
            empty_rows = 0
            for row in sheet.iter(TABLE + "table-row"):
                cells = []
                empty_columns = 0
                for cell in row:
                    if cell.tag not in {TABLE + "table-cell", TABLE + "covered-table-cell"}:
                        continue
                    repeat = int(cell.get(TABLE + "number-columns-repeated", "1"))
                    if repeat < 1:
                        raise ValueError("工作表重复列数无效")
                    value = " ".join(odf_text(p) for p in cell.iter(T + "p"))
                    if not value:
                        value = next((cell.get(OFFICE + key) for key in
                                      ("string-value", "value", "date-value", "time-value", "boolean-value")
                                      if cell.get(OFFICE + key) is not None), "")
                    if not value:
                        empty_columns += repeat
                        continue
                    if len(cells) + empty_columns + repeat > 1024:
                        raise ValueError("工作表列数超过读取上限")
                    cells.extend([""] * empty_columns)
                    empty_columns = 0
                    cells.extend([value] * repeat)
                repeat = int(row.get(TABLE + "number-rows-repeated", "1"))
                if repeat < 1:
                    raise ValueError("工作表重复行数无效")
                # LibreOffice often fills the remaining sheet with millions of
                # repeated empty rows/cells. Trim trailing blanks without
                # expanding them, while retaining gaps before actual data.
                if not cells:
                    empty_rows += repeat
                    continue
                if repeat + empty_rows > 10000:
                    raise ValueError("工作表重复行数超过读取上限")
                for _ in range(empty_rows):
                    yield "\n"
                empty_rows = 0
                for _ in range(repeat):
                    yield "\t".join(cells) + "\n"
    else:
        pages = root.findall(".//" + DRAW + "page")
        for index, section in enumerate(pages or [root], 1):
            if pages:
                yield f"[幻灯片 {index}]\n"
            for element in section.iter():
                if element.tag in {T + "p", T + "h"}:
                    yield odf_text(element) + "\n"


def epub_units(archive):
    container = ET.fromstring(archive.read("META-INF/container.xml"))
    package = container.find(".//{*}rootfile")
    if package is None:
        raise ValueError("EPUB 缺少书籍目录")
    name = archive_path("", package.attrib["full-path"])
    root = ET.fromstring(archive.read(name))
    items = {item.attrib["id"]: item for item in root.findall(".//{*}manifest/{*}item")}
    for index, entry in enumerate(root.findall(".//{*}spine/{*}itemref"), 1):
        item = items.get(entry.get("idref"))
        if item is None or item.get("media-type") not in {"application/xhtml+xml", "text/html"}:
            continue
        target = archive_path(posixpath.dirname(name), item.attrib["href"])
        yield f"[章节 {index}]\n{html_text(decode_text(archive.read(target)))}\n"


def email_units(source):
    message = BytesParser(policy=policy.default).parse(source)
    for name in ("Subject", "From", "To", "Cc", "Date"):
        if message[name]:
            yield f"{name}: {message[name]}\n"
    yield "\n"
    # Prefer one alternative body; attachments and inline images stay unread.
    body = message.get_body(preferencelist=("plain", "html"))
    if body is not None:
        text = body.get_content()
        yield html_text(text) if body.get_content_subtype() == "html" else text


def rtf_text(source):
    from striprtf.striprtf import rtf_to_text
    # Preserve raw 8-bit characters as RTF hex escapes so the parser applies
    # the document/font code page rather than guessing a Unicode encoding.
    text = source.read().decode("latin-1")
    if not text.lstrip().startswith("{\\rtf"):
        raise ValueError("RTF 文件头无效")
    text = re.sub(r"[\x80-\xff]", lambda item: "\\'%02x" % ord(item[0]), text)
    return rtf_to_text(text)


def archive_units(source, suffix):
    yield "[压缩包目录；不展开文件内容]\n"
    if suffix == ".zip":
        with zipfile.ZipFile(source) as archive:
            items = archive.infolist()
            if len(items) > MAX_ENTRIES:
                raise ValueError("压缩包条目超过 10000 上限")
            for item in items:
                yield f"{item.file_size}\t{item.filename}\n"
    else:
        with tarfile.open(fileobj=source, mode="r|*") as archive:
            for index, item in enumerate(archive):
                if index >= MAX_ENTRIES:
                    raise ValueError("压缩包条目超过 10000 上限")
                yield f"{item.size}\t{item.name}\n"
