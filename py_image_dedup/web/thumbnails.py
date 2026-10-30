import hashlib
import logging
import os
from pathlib import Path

from PIL import Image, ImageOps

LOGGER = logging.getLogger(__name__)

# requested sizes are rounded up to one of these, to keep the cache small
THUMBNAIL_SIZES = [160, 320, 640, 1280]


def normalize_size(size: int) -> int:
    for s in THUMBNAIL_SIZES:
        if size <= s:
            return s
    return THUMBNAIL_SIZES[-1]


class ThumbnailCache:
    """
    Creates JPEG thumbnails on demand (EXIF orientation applied) and caches them on disk.
    Original files are only ever opened for reading.
    """

    def __init__(self, cache_directory: Path):
        self.cache_directory = Path(cache_directory)

    def get(self, image_path: Path, size: int) -> Path:
        """
        :param image_path: the original image
        :param size: maximum width/height of the thumbnail
        :return: path of the cached thumbnail
        """
        size = normalize_size(size)
        stat = os.stat(image_path)
        key = hashlib.sha1(f"{image_path}|{stat.st_mtime_ns}|{stat.st_size}|{size}".encode("utf-8")).hexdigest()
        target = self.cache_directory / key[:2] / f"{key}.jpg"
        if target.exists():
            return target

        with Image.open(image_path) as image:
            # let the JPEG decoder downscale while decoding, which is a lot faster for big photos
            image.draft("RGB", (size, size))
            image = ImageOps.exif_transpose(image)
            image.thumbnail((size, size))
            if image.mode != "RGB":
                image = image.convert("RGB")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(f".{os.getpid()}.{id(image)}.tmp")
            image.save(tmp, format="JPEG", quality=82)
        os.replace(tmp, target)
        return target
