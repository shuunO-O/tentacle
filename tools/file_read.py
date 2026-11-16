"""Unified local document reader, independent of model provider."""

import asyncio
import base64

from attachments.documents import READ_SLOTS, read_document


async def file_read(args: dict):
    async with READ_SLOTS:
        result = await asyncio.to_thread(read_document, args.get("path", ""), args.get("offset", 0),
                                         args.get("limit", 20000), args.get("frame", 0))
        if "image" not in result:
            return result
        from attachments.manager import get_file_manager
        record = await get_file_manager().store_image(base64.b64decode(result["image"]), filename="read-preview.png")
        return [
            {"type": "text", "text": result["description"]},
            {"type": "image", "local_id": record["local_id"], "mime": record["mime"]},
        ]


def register_tools(registry):
    registry.register(
        name="file_read",
        description="读取本地文件。图片返回图像（需开启视觉能力，frame 从 0 选帧/页，长边超 2048 缩小预览），支持 JPEG/PNG/GIF/WebP/BMP/TIFF/ICO/AVIF 等。"
                    "文档支持文本/代码/CSV/HTML/SVG、PDF 文字（扫描件需 OCR）、DOCX/XLSX/PPTX（含宏模板）、ODT/ODS/ODP、RTF、EPUB、EML/MHTML；"
                    "ZIP/TAR 列目录，GZIP 解压读文本。支持相对路径，不执行宏、公式或外链；offset 按提取字符计数，next_offset 非空可续读。",
        parameters={"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20000},
            "frame": {"type": "integer", "minimum": 0, "maximum": 10000,
                      "description": "仅用于多帧/多页图片，从 0 开始，默认为第一帧/页。"},
        }, "required": ["path"]},
        func=file_read,
    )
