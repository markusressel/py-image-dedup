"""
Rotation/mirror invariant perceptual hashes.

The 8 dihedral transforms (4 rotations, each optionally mirrored) of an image are hashed from the same small
resized grayscale arrays the hashes are computed from anyway (resizing commutes with rotating/mirroring up to
rounding), so hashing all 8 orientations costs a few extra DCTs on 32x32 arrays, not 8 decodes.
The identity orientation reproduces `imagehash.phash` / `imagehash.dhash` bit for bit.
"""
from enum import Enum
from typing import Dict, Tuple

import numpy as np
import scipy.fftpack
from PIL import Image

# imagehash defaults: 8x8 bit hashes, pHash computed from a 32x32 image
HASH_SIZE = 8
PHASH_IMAGE_SIZE = HASH_SIZE * 4


class Orientation(Enum):
    """
    A dihedral transform: rotation clockwise by 0/90/180/270 degrees, optionally after mirroring
    (flipping left-right). The value is the human readable label used in match notes.
    """
    IDENTITY = "identity"
    ROTATE_90 = "rotated 90°"
    ROTATE_180 = "rotated 180°"
    ROTATE_270 = "rotated 270°"
    MIRROR = "mirrored"
    MIRROR_ROTATE_90 = "mirrored + rotated 90°"
    MIRROR_ROTATE_180 = "mirrored + rotated 180°"
    MIRROR_ROTATE_270 = "mirrored + rotated 270°"

    @property
    def mirrored(self) -> bool:
        return self.name.startswith("MIRROR")

    @property
    def quarter_turns(self) -> int:
        """
        :return: clockwise rotation in multiples of 90 degrees (applied after mirroring)
        """
        if self.name in ("IDENTITY", "MIRROR"):
            return 0
        return int(self.name.rsplit("_", 1)[1]) // 90

    @property
    def transposes(self) -> bool:
        """
        :return: True if width and height are swapped
        """
        return self.quarter_turns % 2 == 1

    @property
    def inverse(self) -> "Orientation":
        """
        :return: the transform that undoes this one (mirrored transforms and 180° are their own inverse)
        """
        if self.mirrored or self.quarter_turns in (0, 2):
            return self
        return Orientation.ROTATE_270 if self is Orientation.ROTATE_90 else Orientation.ROTATE_90

    def apply(self, array: np.ndarray) -> np.ndarray:
        """
        :param array: 2D array (rows = y)
        :return: the transformed array
        """
        if self.mirrored:
            array = np.fliplr(array)
        # np.rot90 rotates counter-clockwise for positive k
        return np.rot90(array, k=-self.quarter_turns)

    def apply_to_image(self, image: Image.Image) -> Image.Image:
        """
        :param image: PIL image
        :return: the transformed image (used to generate test/benchmark variants)
        """
        if self.mirrored:
            image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        # PIL rotates counter-clockwise
        transposes = {1: Image.Transpose.ROTATE_270, 2: Image.Transpose.ROTATE_180, 3: Image.Transpose.ROTATE_90}
        if self.quarter_turns:
            image = image.transpose(transposes[self.quarter_turns])
        return image


# all transforms except the identity, in a fixed order (index into ImageSignature.orientation_hashes)
TRANSFORMS: Tuple[Orientation, ...] = tuple(o for o in Orientation if o is not Orientation.IDENTITY)

HashPair = Tuple[int, int]


def _bits_to_int(bits: np.ndarray) -> int:
    """
    :param bits: 8x8 bool array
    :return: the bits as unsigned int (row major, first bit is the most significant one), same as
             `signatures.hash_to_int`
    """
    return int.from_bytes(np.packbits(bits.flatten()).tobytes(), "big")


def _phash(pixels: np.ndarray) -> int:
    """
    Same as `imagehash.phash` on an already resized 32x32 grayscale array.
    """
    dct = scipy.fftpack.dct(scipy.fftpack.dct(pixels, axis=0), axis=1)
    low_frequencies = dct[:HASH_SIZE, :HASH_SIZE]
    return _bits_to_int(low_frequencies > np.median(low_frequencies))


def _dhash(pixels: np.ndarray) -> int:
    """
    Same as `imagehash.dhash` on an already resized grayscale array of 8 rows and 9 columns.
    """
    return _bits_to_int(pixels[:, 1:] > pixels[:, :-1])


def dihedral_hashes(image: Image.Image) -> Dict[Orientation, HashPair]:
    """
    Computes pHash and dHash of all 8 orientations of an image.

    :param image: image (any mode)
    :return: orientation -> (pHash, dHash) of the image transformed by that orientation
    """
    gray = image.convert("L")
    resample = Image.Resampling.LANCZOS
    phash_pixels = np.asarray(gray.resize((PHASH_IMAGE_SIZE, PHASH_IMAGE_SIZE), resample))
    # dHash compares horizontal neighbours of a 9 (wide) x 8 (high) image; for transposing transforms
    # the rotated 9x8 image corresponds to an 8 (wide) x 9 (high) image of the original
    dhash_wide = np.asarray(gray.resize((HASH_SIZE + 1, HASH_SIZE), resample))
    dhash_tall = np.asarray(gray.resize((HASH_SIZE, HASH_SIZE + 1), resample))
    result = {}
    for orientation in Orientation:
        dhash_source = dhash_tall if orientation.transposes else dhash_wide
        result[orientation] = (_phash(orientation.apply(phash_pixels)), _dhash(orientation.apply(dhash_source)))
    return result


def orientation_hashes(hashes: Dict[Orientation, HashPair]) -> Tuple[HashPair, ...]:
    """
    :param hashes: result of `dihedral_hashes`
    :return: (pHash, dHash) of the non-identity transforms in the order of `TRANSFORMS`
    """
    return tuple(hashes[orientation] for orientation in TRANSFORMS)


def encode_orientation_hashes(hashes: Tuple[HashPair, ...]) -> bytes:
    """
    :param hashes: (pHash, dHash) per transform, in the order of `TRANSFORMS`
    :return: compact binary representation (16 bytes per transform) for storage
    """
    return b"".join(p.to_bytes(8, "big") + d.to_bytes(8, "big") for p, d in hashes)


def decode_orientation_hashes(data: bytes) -> Tuple[HashPair, ...]:
    """
    :param data: result of `encode_orientation_hashes`
    :return: (pHash, dHash) per transform, in the order of `TRANSFORMS`
    """
    return tuple((int.from_bytes(data[i:i + 8], "big"), int.from_bytes(data[i + 8:i + 16], "big"))
                 for i in range(0, len(data), 16))
