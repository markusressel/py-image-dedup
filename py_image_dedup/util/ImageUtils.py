import PIL.ExifTags
from PIL import Image


def get_exif_data(image_file_path: str) -> {}:
    """
    Tries to extract all exif data from the given image file
    :param image_file_path: path of the image file
    :return: dictionary containing all available exif data entries and their values
    """

    try:
        img = Image.open(image_file_path)

        exif_data = img._getexif()
        if exif_data:
            return {
                PIL.ExifTags.TAGS[key]: value for key, value in exif_data.items() if key in PIL.ExifTags.TAGS
            }
    except Exception as e:
        pass
    return {}
