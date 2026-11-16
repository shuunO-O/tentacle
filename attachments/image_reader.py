"""Decode a single raster frame inside the bounded document subprocess."""

import base64
from io import BytesIO
import warnings


def image_preview(source, frame):
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 16_000_000
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    # Explicit raster decoders only: never invoke external renderers such as
    # Ghostscript for EPS/PDF, and never resolve external SVG resources.
    formats = ("JPEG", "PNG", "GIF", "WEBP", "BMP", "DIB", "TIFF", "ICO", "AVIF",
               "PPM", "TGA", "PCX", "PSD", "JPEG2000")
    with Image.open(source, formats=formats) as image:
        if image.width * image.height > Image.MAX_IMAGE_PIXELS:
            raise ValueError("图片超过 1600 万像素上限")
        try:
            image.seek(frame)
        except EOFError:
            raise ValueError("frame 超出图片帧/页范围（从 0 开始）") from None
        width, height = image.size
        if width * height > Image.MAX_IMAGE_PIXELS:
            raise ValueError("图片超过 1600 万像素上限")
        image.thumbnail((2048, 2048))
        preview = ImageOps.exif_transpose(image).convert("RGBA" if "A" in image.getbands()
                                                            or "transparency" in image.info else "RGB")
        # Do not carry EXIF/XMP/other private metadata into model previews.
        preview.info.clear()
        output = BytesIO()
        preview.save(output, format="PNG")
        return {
            "image": base64.b64encode(output.getvalue()).decode("ascii"),
            "description": f"[图片 {image.format} {width}×{height}，frame={frame}；"
                           f"预览 {preview.width}×{preview.height}。多帧/多页文件可用 frame 读取其他帧/页]",
        }
