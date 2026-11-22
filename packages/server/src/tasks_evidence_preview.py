"""Read an existing local raster image referenced by task activity."""

from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlsplit

from PIL import Image


MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 40_000_000
RASTER_FORMATS = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".webp": "WEBP",
    ".gif": "GIF",
}


def raster_path(url):
    parsed = urlsplit(url)
    if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
        return None
    path = Path(unquote(parsed.path))
    if path.is_absolute() and path.suffix.lower() in RASTER_FORMATS:
        return path
    return None


def read_image(url):
    path = raster_path(url)
    if path is None:
        return None
    try:
        # The offline CLI saves the resolved path. Do not follow a replacement symlink.
        if not path.is_file() or path.resolve(strict=True) != path:
            return None
        with path.open("rb") as source:
            contents = source.read(MAX_BYTES + 1)
        if len(contents) > MAX_BYTES:
            return None
        with Image.open(BytesIO(contents), formats=[RASTER_FORMATS[path.suffix.lower()]]) as image:
            if image.width * image.height > MAX_PIXELS:
                return None
            mime_type = Image.MIME[image.format]
            image.verify()
        return contents, mime_type
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        return None
