from enum import Enum


class MetadataKey(Enum):
    METADATA = "metadata"

    DATAMODEL_VERSION = "py-image-dedup_datamodel-version"

    PATH = "path"
    DISTANCE = "dist"
    SCORE = "score"

    FILE_SIZE = "filesize"
    FILE_MODIFICATION_DATE = "file_modification_date"

    EXIF_DATA = "exif_data"
