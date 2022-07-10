import PIL.ExifTags
from PIL import Image, TiffImagePlugin


def get_exif_data(image_file_path: str) -> {}:
    """
    Tries to extract all exif data from the given image file
    :param image_file_path: path of the image file
    :return: dictionary containing all available exif data entries and their values
    """

    result = {}
    try:
        img = Image.open(image_file_path)

        exif_data = img._getexif()
        if not exif_data:
            return result

        for k, v in exif_data.items():
            if k in PIL.ExifTags.TAGS:
                tag_name = PIL.ExifTags.TAGS[k]
                if isinstance(v, TiffImagePlugin.IFDRational):
                    if v._denominator != 0:
                        v = v._numerator / v._denominator
                    else:
                        v = float(v._numerator)
                result[tag_name] = v
    except Exception as e:
        pass
    return result


def get_pixel_count(image_file_path: str) -> int:
    try:
        img = Image.open(image_file_path)
        width, height = img.size
        return width * height
    except Exception as e:
        pass
    return 0
