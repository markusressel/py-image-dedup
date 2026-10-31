from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from py_image_dedup.matching.metadata import extract_metadata
from py_image_dedup.matching.models import ImageMetadata

EXIF_ORIENTATION = 0x0112
# orientations that swap width and height
_TRANSPOSING_ORIENTATIONS = {5, 6, 7, 8}

DEFAULT_PREVIEW_SIZE = 512


@dataclass
class DecodedImage:
    """
    A decoded, normalised (EXIF orientation applied, RGB) image.
    """
    # the (possibly reduced) RGB image used for perceptual hashing
    preview: Image.Image
    # size of the full image after applying the EXIF orientation
    full_width: int
    full_height: int
    metadata: ImageMetadata


def _to_rgb(image: Image.Image) -> Image.Image:
    """
    :param image: image in any mode
    :return: the image in RGB mode
    """
    if image.mode == "RGB":
        return image
    if image.mode in ("I;16", "I;16B", "I;16L", "I"):
        # 16 bit greyscale (e.g. PNG): scale down to 8 bit before converting
        image = image.point(lambda v: v / 256).convert("L")
    if image.mode == "P":
        image = image.convert("RGBA")
    return image.convert("RGB")


def decode_preview(path: Path or str, preview_size: int = DEFAULT_PREVIEW_SIZE) -> DecodedImage:
    """
    Decodes an image for perceptual hashing.

    JPEG files are decoded in draft mode (DCT scaling), which is several times faster than a full decode
    and yields an image that is at least `preview_size` pixels in both dimensions (up to 2x larger).
    Other formats are decoded fully. The EXIF orientation is applied, so a rotated copy that only differs
    in its orientation tag yields the same preview.

    :param path: image file
    :param preview_size: minimum edge length of the preview (JPEG only)
    :return: the decoded image
    """
    with Image.open(path) as image:
        exif = image.getexif()
        metadata = extract_metadata(exif)
        full_width, full_height = image.size
        if exif.get(EXIF_ORIENTATION) in _TRANSPOSING_ORIENTATIONS:
            full_width, full_height = full_height, full_width

        if image.format == "JPEG":
            image.draft(image.mode, (preview_size, preview_size))
        oriented = ImageOps.exif_transpose(image)
        preview = _to_rgb(oriented)
        preview.load()

    return DecodedImage(preview=preview, full_width=full_width, full_height=full_height, metadata=metadata)


def decode_full(path: Path or str) -> Image.Image:
    """
    Decodes the full resolution image with the EXIF orientation applied.

    :param path: image file
    :return: RGB image
    """
    with Image.open(path) as image:
        oriented = ImageOps.exif_transpose(image)
        result = _to_rgb(oriented)
        result.load()
    return result
