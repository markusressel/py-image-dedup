import hashlib
import os
from pathlib import Path

import imagehash
import numpy as np
from PIL import Image

from py_image_dedup.matching.decoding import decode_preview, decode_full, DEFAULT_PREVIEW_SIZE
from py_image_dedup.matching.models import ImageSignature
from py_image_dedup.matching.orientation import Orientation, dihedral_hashes, orientation_hashes

# a pixel counts as coloured if its channels differ by more than this (0..255)
_GRAYSCALE_CHROMA_THRESHOLD = 12
# an image is grayscale if less than this fraction of its pixels is coloured
_GRAYSCALE_MAX_COLOURED_FRACTION = 0.01


def file_sha256(path: Path or str) -> str:
    """
    :param path: file
    :return: hex SHA-256 of the file content
    """
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def pixel_hash(image: Image.Image) -> str:
    """
    Hashes decoded pixels (including the image size, so a transposed image does not collide).

    :param image: RGB image
    :return: hex SHA-256
    """
    digest = hashlib.sha256(f"{image.mode}:{image.width}x{image.height}:".encode())
    digest.update(image.tobytes())
    return digest.hexdigest()


def hash_to_int(image_hash: imagehash.ImageHash) -> int:
    """
    :param image_hash: a 64 bit imagehash
    :return: the hash bits as unsigned int (row major, first bit is the most significant one)
    """
    bits = image_hash.hash.flatten()
    if bits.size != 64:
        raise ValueError(f"Expected a 64 bit hash, got {bits.size} bits")
    return int.from_bytes(np.packbits(bits).tobytes(), "big")


def is_grayscale(image: Image.Image) -> bool:
    """
    Detects images without colour information (also grayscale images saved as RGB JPEG, which have chroma noise).

    :param image: RGB image
    :return: True if (almost) no pixel is coloured
    """
    small = image.copy()
    small.thumbnail((128, 128))
    pixels = np.asarray(small, dtype=np.int16)
    chroma = pixels.max(axis=2) - pixels.min(axis=2)
    return float(np.mean(chroma > _GRAYSCALE_CHROMA_THRESHOLD)) < _GRAYSCALE_MAX_COLOURED_FRACTION


def colour_moments(image: Image.Image) -> tuple:
    """
    :param image: RGB image (ideally already small, e.g. a 128 px thumbnail)
    :return: (mean R, mean G, mean B, std R, std G, std B), 0..255, rounded to 0.1
    """
    pixels = np.asarray(image, dtype=np.float64).reshape(-1, 3)
    return tuple(round(float(v), 1) for v in (*pixels.mean(axis=0), *pixels.std(axis=0)))


def compute_signature(path: Path or str,
                      preview_size: int = DEFAULT_PREVIEW_SIZE,
                      orientations: bool = True) -> ImageSignature:
    """
    Computes all signatures of an image file (except the lazy full resolution pixel hash).

    :param path: image file
    :param preview_size: minimum edge length of the (JPEG draft) preview used for hashing
    :param orientations: also compute the hashes of the 7 rotated/mirrored orientations
    :return: the signature
    """
    path = str(path)
    stat = os.stat(path)
    decoded = decode_preview(path, preview_size)
    preview = decoded.preview
    if orientations:
        hashes = dihedral_hashes(preview)
        phash, dhash = hashes[Orientation.IDENTITY]
    else:
        hashes = None
        phash, dhash = hash_to_int(imagehash.phash(preview)), hash_to_int(imagehash.dhash(preview))
    small = preview.copy()
    small.thumbnail((128, 128))
    return ImageSignature(
        path=path,
        file_size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256=file_sha256(path),
        preview_pixel_hash=pixel_hash(preview),
        phash=phash,
        dhash=dhash,
        width=decoded.full_width,
        height=decoded.full_height,
        is_grayscale=is_grayscale(small),
        metadata=decoded.metadata,
        orientation_hashes=orientation_hashes(hashes) if hashes is not None else None,
        colour_moments=colour_moments(small),
    )


def compute_full_pixel_hash(path: Path or str) -> str:
    """
    Hashes the full resolution decoded pixels. Expensive (full decode), only used to verify
    candidates whose preview pixel hashes are equal.

    :param path: image file
    :return: hex SHA-256
    """
    return pixel_hash(decode_full(path))
