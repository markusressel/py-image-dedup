import logging
from datetime import datetime
from typing import Optional

from PIL import Image

from py_image_dedup.matching.models import ImageMetadata

LOGGER = logging.getLogger(__name__)

TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_SOFTWARE = 0x0131
IFD_EXIF = 0x8769
TAG_DATETIME_ORIGINAL = 0x9003
TAG_SUBSEC_TIME_ORIGINAL = 0x9291
TAG_IMAGE_UNIQUE_ID = 0xA420

_EXIF_DATETIME_FORMAT = "%Y:%m:%d %H:%M:%S"


def _clean(value) -> Optional[str]:
    """
    :param value: raw EXIF value
    :return: stripped string, None if empty
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = str(value).replace("\x00", "").strip()
    return value or None


def parse_capture_time(date_time: Optional[str], sub_sec: Optional[str]) -> (Optional[datetime], bool):
    """
    Parses an EXIF date time ("YYYY:MM:DD HH:MM:SS") with optional sub-seconds ("123" = 0.123 s).

    :param date_time: DateTimeOriginal value
    :param sub_sec: SubSecTimeOriginal value
    :return: (naive datetime or None, True if sub-seconds were applied)
    """
    date_time = _clean(date_time)
    if date_time is None:
        return None, False
    try:
        result = datetime.strptime(date_time[:19], _EXIF_DATETIME_FORMAT)
    except ValueError:
        LOGGER.debug(f"Unparsable EXIF date time: {date_time!r}")
        return None, False

    sub_sec = _clean(sub_sec)
    if sub_sec is not None and sub_sec.isdigit():
        # the digits are a decimal fraction: "5" = 0.5 s, "050" = 0.05 s
        microseconds = int(round(float(f"0.{sub_sec}") * 1_000_000))
        return result.replace(microsecond=min(microseconds, 999_999)), True
    return result, False


def extract_metadata(exif: Image.Exif) -> ImageMetadata:
    """
    Extracts the metadata used for matching from EXIF data.

    :param exif: EXIF data of an image (`Image.getexif()`)
    :return: extracted metadata, missing values are None
    """
    try:
        exif_ifd = exif.get_ifd(IFD_EXIF)
    except Exception as ex:
        LOGGER.debug(f"Could not read EXIF IFD: {ex}")
        exif_ifd = {}

    capture_time, has_subsec = parse_capture_time(
        exif_ifd.get(TAG_DATETIME_ORIGINAL),
        exif_ifd.get(TAG_SUBSEC_TIME_ORIGINAL),
    )
    return ImageMetadata(
        capture_time=capture_time,
        capture_time_has_subsec=has_subsec,
        camera_make=_clean(exif.get(TAG_MAKE)),
        camera_model=_clean(exif.get(TAG_MODEL)),
        image_unique_id=_clean(exif_ifd.get(TAG_IMAGE_UNIQUE_ID)),
        software=_clean(exif.get(TAG_SOFTWARE)),
    )
