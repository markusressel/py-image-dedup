from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

Pair = Tuple[str, str]


@dataclass(frozen=True)
class PairMetrics:
    """
    Pairwise precision/recall of a grouping compared to a ground truth.
    """
    true_positives: int
    false_positives: int
    false_negatives: int

    @property
    def precision(self) -> float:
        found = self.true_positives + self.false_positives
        return self.true_positives / found if found else 1.0

    @property
    def recall(self) -> float:
        expected = self.true_positives + self.false_negatives
        return self.true_positives / expected if expected else 1.0


def pairs_of_groups(groups: Iterable[Iterable[str]]) -> Set[Pair]:
    """
    :param groups: groups of paths
    :return: all unordered pairs (a < b) of paths within the same group
    """
    result = set()
    for group in groups:
        for a, b in combinations(sorted(set(group)), 2):
            result.add((a, b))
    return result


def ground_truth_by_folder(paths: Iterable[str]) -> List[List[str]]:
    """
    Ground truth convention of tests/images/: all files in the same folder show the same photo.

    :param paths: file paths
    :return: groups of paths with the same parent folder (only groups with at least two files)
    """
    by_folder: Dict[str, List[str]] = {}
    for path in paths:
        by_folder.setdefault(str(Path(path).parent), []).append(path)
    return [sorted(group) for group in by_folder.values() if len(group) > 1]


def evaluate_pairs(found: Set[Pair], expected: Set[Pair]) -> PairMetrics:
    """
    :param found: pairs detected as duplicates
    :param expected: pairs that are duplicates according to the ground truth
    :return: pairwise metrics
    """
    return PairMetrics(
        true_positives=len(found & expected),
        false_positives=len(found - expected),
        false_negatives=len(expected - found),
    )
