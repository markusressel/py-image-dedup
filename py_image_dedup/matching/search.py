from dataclasses import dataclass
from typing import List, Sequence

import numpy as np

# number of rows compared against all hashes at once, bounds the temporary memory to
# BLOCK_SIZE * n * 8 bytes (e.g. 256 * 40_000 * 8 = 80 MB)
DEFAULT_BLOCK_SIZE = 256


@dataclass(frozen=True)
class CandidatePair:
    """
    Two hashes (by index, i < j) within the distance limits.
    """
    i: int
    j: int
    phash_distance: int
    dhash_distance: int


def to_array(hashes: Sequence[int]) -> np.ndarray:
    """
    :param hashes: unsigned 64 bit hashes
    :return: numpy uint64 array
    """
    return np.array(hashes, dtype=np.uint64)


def hamming_distance(a: int, b: int) -> int:
    """
    :param a: unsigned 64 bit hash
    :param b: unsigned 64 bit hash
    :return: number of differing bits
    """
    return (a ^ b).bit_count()


def find_candidate_pairs(phashes: np.ndarray,
                         dhashes: np.ndarray,
                         max_phash_distance: int,
                         max_dhash_distance: int,
                         block_size: int = DEFAULT_BLOCK_SIZE) -> List[CandidatePair]:
    """
    Brute force all-pairs Hamming distance search, vectorised with numpy.

    Both hashes have to be within their limit. Complexity is O(n²) popcounts, which takes a few seconds
    for 40k images; memory stays bounded by processing `block_size` rows at a time.

    :param phashes: uint64 pHashes, one per image
    :param dhashes: uint64 dHashes, same order as phashes
    :param max_phash_distance: maximum pHash Hamming distance (inclusive)
    :param max_dhash_distance: maximum dHash Hamming distance (inclusive)
    :param block_size: rows per block
    :return: all pairs (i < j) within both limits
    """
    if phashes.shape != dhashes.shape:
        raise ValueError("phashes and dhashes must have the same shape")
    n = phashes.shape[0]
    result = []
    for start in range(0, n, block_size):
        end = min(start + block_size, n)
        # only compare against hashes with a higher index, every pair is visited once
        column_offset = start + 1
        if column_offset >= n:
            break
        block = np.bitwise_xor(phashes[start:end, None], phashes[None, column_offset:])
        phash_distances = np.bitwise_count(block)
        del block
        # exclude pairs j <= i inside the block: row r (i = start + r) and column c (j = start + 1 + c)
        # are a valid pair only if c >= r
        rows = end - start
        width = min(rows, n - column_offset)
        phash_distances[np.tril_indices(rows, k=-1, m=width)] = np.iinfo(phash_distances.dtype).max
        row_index, column_index = np.nonzero(phash_distances <= max_phash_distance)
        if row_index.size == 0:
            continue
        i = row_index + start
        j = column_index + column_offset
        dhash_distances = np.bitwise_count(dhashes[i] ^ dhashes[j])
        keep = dhash_distances <= max_dhash_distance
        for ii, jj, dp, dd in zip(i[keep], j[keep], phash_distances[row_index, column_index][keep],
                                  dhash_distances[keep]):
            result.append(CandidatePair(int(ii), int(jj), int(dp), int(dd)))
    return result
