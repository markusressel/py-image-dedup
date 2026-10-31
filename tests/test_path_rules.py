from py_image_dedup.library.deduplicator import ImageMatchDeduplicator
from py_image_dedup.persistence.metadata_key import MetadataKey
from tests import TestBase

CAMERA = "/data/photos/Markus/Syncthing/Fold4/Camera/Camera/20250518_125742.jpg"
ALBUM = "/data/photos/Familie/Hochzeit/20250518_125742.jpg"
IMMICH = "/data/photos/Iris/Syncthing/FP4/Kamera/Immich/IMG_20250518_143957_826.jpg"
TELEGRAM = "/data/photos/Iris/Syncthing/FP4/Telegram Gesendet/IMG_20250518_143957_826.jpg"
OTHER = "/data/photos/Markus/Sonstiges/IMG_1.jpg"


def _candidate(path: str, modification_date: float) -> dict:
    return {
        MetadataKey.PATH.value: path,
        MetadataKey.DISTANCE.value: 0.0,
        MetadataKey.SCORE.value: 64,
        MetadataKey.METADATA.value: {
            MetadataKey.FILE_SIZE.value: 100,
            MetadataKey.FILE_MODIFICATION_DATE.value: modification_date,
            MetadataKey.PIXELCOUNT.value: 10000,
            MetadataKey.EXIF_DATA.value: {},
        },
    }


class PathRulesTest(TestBase):
    """
    The preferred-paths and avoided-paths prioritization rules.
    """

    def setUp(self):
        super().setUp()
        original = self.config.PRIORITIZATION_RULES.value
        self.addCleanup(setattr, self.config.PRIORITIZATION_RULES, "value", original)

    def _ranked(self, *candidates) -> list:
        return [c[MetadataKey.PATH.value]
                for c in ImageMatchDeduplicator._sort_by_quality_descending(list(candidates))]

    def test_original_before_copies(self):
        # same image: the Immich download is the newest file, the camera original the oldest
        candidates = [_candidate(TELEGRAM, 2), _candidate(CAMERA, 1), _candidate(IMMICH, 3)]

        self.config.PRIORITIZATION_RULES.value = [{"name": "newer-file-modification-date"}]
        self.assertEqual([IMMICH, TELEGRAM, CAMERA], self._ranked(*candidates))

        self.config.PRIORITIZATION_RULES.value = [
            {"name": "preferred-paths", "patterns": ["/Familie/", "/Camera/"]},
            {"name": "avoided-paths", "patterns": ["/Immich/", "/Telegram "]},
            {"name": "older-file-modification-date"},
        ]
        self.assertEqual([CAMERA, IMMICH, TELEGRAM], self._ranked(*candidates))

    def test_preferred_paths_order(self):
        self.config.PRIORITIZATION_RULES.value = [
            {"name": "preferred-paths", "patterns": ["/Familie/", "/Camera/"]},
            {"name": "older-file-modification-date"},
        ]
        # an album copy wins over the (older) camera original, files matching no pattern come last
        self.assertEqual([ALBUM, CAMERA, OTHER],
                         self._ranked(_candidate(OTHER, 0), _candidate(CAMERA, 1), _candidate(ALBUM, 2)))

    def test_avoided_paths_order(self):
        self.config.PRIORITIZATION_RULES.value = [
            {"name": "avoided-paths", "patterns": ["/Immich/", "/Telegram "]},
            {"name": "older-file-modification-date"},
        ]
        # files matching no pattern first, then in pattern order
        self.assertEqual([OTHER, IMMICH, TELEGRAM],
                         self._ranked(_candidate(TELEGRAM, 0), _candidate(IMMICH, 1), _candidate(OTHER, 2)))

    def test_invalid_pattern_never_matches(self):
        self.config.PRIORITIZATION_RULES.value = [{"name": "preferred-paths", "patterns": ["([", "/Camera/"]}]
        self.assertEqual([CAMERA, OTHER], self._ranked(_candidate(OTHER, 0), _candidate(CAMERA, 1)))

    def test_criteria_explain_the_match(self):
        self.config.PRIORITIZATION_RULES.value = [{"name": "avoided-paths", "patterns": ["/Telegram "]}]
        criteria = ImageMatchDeduplicator._quality_criteria(_candidate(TELEGRAM, 0))
        self.assertEqual(("avoided-paths", 0, "matches avoided-paths pattern '/Telegram '"), criteria[0])
