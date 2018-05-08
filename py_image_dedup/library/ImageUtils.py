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
    except:
        return {}

    # if 'DateTimeDigitized' in exif_data:
    #     result['exif_date_time_digitized'] = exif_data['DateTimeDigitized']
    # if 'DateTimeOriginal' in exif_data:
    #     result['exif_date_time_original'] = exif_data['DateTimeOriginal']
    # if 'Orientation' in exif_data:
    #     metadata['exif_orientation'] = exif_data['Orientation']
    #
    # if 'XResolution' in exif_data:
    #     metadata['exif_x_resolution'] = exif_data['XResolution']
    # if 'YResolution' in exif_data:
    #     metadata['exif_y_resolution'] = exif_data['YResolution']
    #
    # if 'ExifImageWidth' in exif_data:
    #     metadata['exif_image_width'] = exif_data['ExifImageWidth']
    # if 'ExifImageHeight' in exif_data:
    #     metadata['exif_image_height'] = exif_data['ExifImageHeight']
