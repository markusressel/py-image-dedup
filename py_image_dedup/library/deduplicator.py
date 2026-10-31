import datetime
import filecmp
import logging
import os
import re
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List

import click
from ordered_set import OrderedSet

from py_image_dedup import util
from py_image_dedup.config import DeduplicatorConfig, MATCHER_HASHES, MATCHER_IMAGE_MATCH
from py_image_dedup.integrations.immich import ImmichClient
from py_image_dedup.library import ActionEnum
from py_image_dedup.library.deduplication_result import DeduplicationResult
from py_image_dedup.library.progress_manager import ProgressManager
from py_image_dedup.persistence import ImageSignatureStore
from py_image_dedup.persistence.elasticsearchstorebackend import ElasticSearchStoreBackend
from py_image_dedup.persistence.metadata_key import MetadataKey
from py_image_dedup.stats import DUPLICATE_ACTION_MOVE_COUNT, DUPLICATE_ACTION_DELETE_COUNT, ANALYSIS_TIME, \
    FIND_DUPLICATES_TIME
from py_image_dedup.state.progress import StateProgressListener
from py_image_dedup.state.store import ActionStatus, DecisionSource, GroupStatus
from py_image_dedup.util import file, echo
from py_image_dedup.util.file import get_files_count, file_has_extension

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.DEBUG)

MATCHER_NAME = MATCHER_IMAGE_MATCH

# phase of failures recorded by the hash pipeline (failures.phase)
HASH_ANALYSIS_FAILURE_PHASE = "analysis (hashes)"

# prioritization rules which only break ties between otherwise equal copies.
# A group whose best copy was chosen by one of these is flagged for review.
WEAK_PRIORITIZATION_RULES = {
    "smaller-distance",
    "bigger-distance",
    "contains-copy-in-file-name",
    "doesnt-contain-copy-in-file-name",
    "longer-file-name",
    "shorter-file-name",
    "longer-folder-path",
    "shorter-folder-path",
    "higher-score",
    "lower-score",
    "file-path",  # implicit last criterion: alphabetical order of the file path
}

# prefer-edited: file name (without extension) and EXIF "Software" patterns of edited copies
DEFAULT_EDITED_FILE_NAME_PATTERNS = [r"(?i)[-_ .(](edit|edited|bearbeitet)(\W|_|$)"]
DEFAULT_EDITING_SOFTWARE_PATTERNS = [
    r"(?i)lightroom|photoshop|snapseed|gimp|darktable|rawtherapee|affinity|luminar|capture one|pixelmator|"
    r"vsco|picsart|facetune"
]

# EXIF values longer than this are not copied into the state database (e.g. maker notes)
MAX_STORED_EXIF_VALUE_LENGTH = 200


def _path_matches(pattern: str, path: str) -> bool:
    """
    :return: True if the regular expression matches (anywhere in) the path, False for invalid expressions
    """
    try:
        return re.search(pattern, path) is not None
    except re.error:
        LOGGER.warning(f"Invalid path pattern in prioritization rules: {pattern}")
        return False


def _edit_marker(candidate: dict, rule: dict) -> str | None:
    """
    Checks whether a candidate is an edited copy, see the prefer-edited prioritization rule.
    :return: why the candidate is considered edited, None if it is not
    """
    path = Path(str(candidate[MetadataKey.PATH.value]))
    for pattern in rule.get("file_name_patterns") or DEFAULT_EDITED_FILE_NAME_PATTERNS:
        if _path_matches(pattern, path.stem):
            return f"file name matches '{pattern}'"
    exif = candidate[MetadataKey.METADATA.value].get(MetadataKey.EXIF_DATA.value) or {}
    software = exif.get("Software")
    if software:
        for pattern in rule.get("software_patterns") or DEFAULT_EDITING_SOFTWARE_PATTERNS:
            if _path_matches(pattern, str(software)):
                return f"edited with '{str(software).strip()}'"
    return None


def _move_without_overwrite(source: Path, destination: Path):
    """
    Moves a file, failing with FileExistsError instead of replacing an existing destination file.
    """
    try:
        # hard link + unlink: atomically fails if the destination exists
        os.link(source, destination)
    except FileExistsError:
        raise
    except OSError:
        # other file system, or no hard link support: copy into a newly created file
        with open(source, "rb") as source_file, open(destination, "xb") as destination_file:
            try:
                shutil.copyfileobj(source_file, destination_file)
            except BaseException:
                destination_file.close()
                os.unlink(destination)
                raise
        shutil.copystat(source, destination)
    os.unlink(source)


def _format_timestamp(timestamp: float) -> str:
    try:
        return datetime.datetime.fromtimestamp(timestamp).isoformat(sep=" ", timespec="seconds")
    except Exception:
        return str(timestamp)


class ImageMatchDeduplicator:
    _config: DeduplicatorConfig
    _progress_manager: ProgressManager

    _processed_files: dict
    _deduplication_result: DeduplicationResult

    def __init__(self, interactive: bool, state_store=None):
        """

        :param interactive: whether cli output should be interactive or not
        :param state_store: optional StateStore to record runs, duplicate groups, decisions and actions in
        """
        self.interactive = interactive
        self._state = state_store
        self._run_id = None
        # held while a run is in progress, so user decisions from the web UI are executed in between runs
        self.work_lock = threading.RLock()
        # files really moved/deleted since the last Immich library rescan was triggered
        self._changed_paths = []
        self.reset_result()

        listener = StateProgressListener(state_store, lambda: self._run_id) if state_store is not None else None
        self._progress_manager = ProgressManager(listener=listener)
        self._config = DeduplicatorConfig()
        if state_store is not None and state_store.actionable_matchers is None:
            # groups of the other matcher (f.ex. the hash pipeline with backend "both") are record-only
            state_store.actionable_matchers = {self._config.acting_matcher}
        # image-match + Elasticsearch, None with matching.backend "hashes" (no Elasticsearch needed)
        self._persistence: ImageSignatureStore | None = None
        if self._config.uses_image_match:
            self._persistence = ElasticSearchStoreBackend(
                host=self._config.ELASTICSEARCH_HOST.value,
                port=self._config.ELASTICSEARCH_PORT.value,
                connections_per_node=self._config.ANALYSIS_THREADS.value,
                el_index=self._config.ELASTICSEARCH_INDEX.value,
                use_exif_data=self._config.ANALYSIS_USE_EXIF_DATA.value,
                max_dist=self._config.ELASTICSEARCH_MAX_DISTANCE.value,
                setup_database=self._config.ELASTICSEARCH_AUTO_CREATE_INDEX.value,
                connection_timeout=self._config.ELASTICSEARCH_CONNECTION_TIMEOUT.value,
            )
        # the hash pipeline (matching.backend "hashes" or "both"), created on first use, see hash_matcher
        self._hash_matcher = None
        # reads file metadata (size, pixel count, EXIF) of hash pipeline groups for the prioritization rules
        self._metadata_reader = ImageSignatureStore(use_exif_data=self._config.ANALYSIS_USE_EXIF_DATA.value)

    def reset_result(self):
        self._deduplication_result = DeduplicationResult()
        # statistics of the hash pipeline in the current run (added to the run summary)
        self._hash_summary = {}
        self._processed_files = {}
        # state store bookkeeping for the current result: file path -> group id, groups with failed actions
        self._group_of_path = {}
        self._failed_groups = set()
        self._group_recording_failed = False

    @property
    def hash_matcher(self):
        """
        :return: the hash pipeline (created on first access from the matching.* configuration)
        """
        if self._hash_matcher is None:
            from py_image_dedup.library.hash_matcher import HashMatcher
            self._hash_matcher = HashMatcher(self._config)
        return self._hash_matcher

    def begin_run(self, kind: str, trigger: str | None = None) -> bool:
        """
        Records the start of a run in the state store (if any and if no run is active).
        :param kind: "full" or "incremental"
        :param trigger: what triggered the run
        :return: True if a run was started (and has to be ended with end_run())
        """
        if self._state is None or self._run_id is not None:
            return False
        try:
            self._run_id = self._state.start_run(kind, dry_run=self._config.DRY_RUN.value, trigger=trigger)
            return True
        except Exception as ex:
            LOGGER.exception(ex)
            return False

    def end_run(self, started: bool, error: str | None = None):
        """
        Records the end of a run started with begin_run().
        :param started: return value of begin_run()
        :param error: error message, if the run failed
        """
        if not started or self._run_id is None:
            return
        result = self._deduplication_result
        summary = {
            "files_with_duplicates": result.get_duplicate_count(),
            "moved": len(result.get_file_with_action(ActionEnum.MOVE)),
            "deleted": len(result.get_file_with_action(ActionEnum.DELETE)),
            "removed_folders": len(result.get_removed_empty_folders()),
        }
        summary.update(self._hash_summary)
        try:
            self._state.finish_run(self._run_id, error=error, summary=summary)
        except Exception as ex:
            LOGGER.exception(ex)
        finally:
            self._run_id = None

    def start_phase(self, name: str, index: int | None = None, count: int | None = None):
        """
        Records the start of a phase of the current run in the state store.
        """
        if self._state is None or self._run_id is None:
            return
        try:
            self._state.start_phase(self._run_id, name, index, count)
        except Exception as ex:
            LOGGER.exception(ex)

    def analyse_all(self):
        """
        Runs the analysis phase independently.
        """
        directories = self._config.SOURCE_DIRECTORIES.value

        if self._config.uses_image_match:
            echo("Phase 1/2: Counting files ...", color='cyan')
            directory_map = self._count_files(directories)

            echo("Phase 2/2: Analyzing files ...", color='cyan')
            self.analyze_directories(directory_map)
        if self._config.uses_hashes:
            echo("Analyzing files (hashes) ...", color='cyan')
            self.analyze_files_with_hashes(directories)

    def deduplicate_all(self, skip_analyze_phase: bool = False, trigger: str | None = None) -> DeduplicationResult:
        """
        Runs the full deduplication phases (6, or 8 with matching.backend "both").
        :param skip_analyze_phase: useful if you already did a dry run and want to do a real run afterwards
        :param trigger: what triggered the run (state store)
        :return: result of the operation
        """
        # see: https://stackoverflow.com/questions/14861891/runtimewarning-invalid-value-encountered-in-divide
        # and: https://stackoverflow.com/questions/29347987/why-cant-i-suppress-numpy-warnings
        import warnings
        warnings.filterwarnings('ignore')

        directories = self._config.SOURCE_DIRECTORIES.value
        if len(directories) <= 0:
            raise ValueError("No root directories to scan")

        if self._config.DRY_RUN.value:
            echo("==> DRY RUN! No files or folders will actually be deleted! <==", color='yellow')

        use_image_match = self._config.uses_image_match
        use_hashes = self._config.uses_hashes
        # backend "both": image-match decides, the hash pipeline only records its groups
        hashes_record_only = use_image_match and use_hashes
        phase_count = 6 + (2 if hashes_record_only else 0)
        phase_index = 0

        def next_phase(name: str) -> str:
            nonlocal phase_index
            phase_index += 1
            self.start_phase(name, phase_index, phase_count)
            return f"Phase {phase_index}/{phase_count}: {name}"

        with self.work_lock:
            run_started = self.begin_run("full", trigger=trigger)
            # matchers that saw all files in this run: their groups that were not found again are resolved
            complete_matchers = []
            try:
                echo(next_phase("Cleaning up database") + " ...", color='cyan')
                self.cleanup_database(directories)
                self._forget_failures_of_missing_files()

                echo(next_phase("Counting files") + " ...", color='cyan')
                directory_map = self._count_files(directories)

                self.reset_result()
                if use_image_match:
                    phase_text = next_phase("Analyzing files")
                    if skip_analyze_phase:
                        echo(phase_text + " - Skipping", color='yellow')
                    else:
                        echo(phase_text, color='cyan')
                        self.analyze_directories(directory_map)

                    echo(next_phase("Finding duplicates") + " ...", color='cyan')
                    self.find_duplicates_in_directories(directory_map)
                    complete_matchers.append(MATCHER_IMAGE_MATCH)

                if use_hashes:
                    try:
                        self._run_hash_matcher(directories, next_phase, skip_analyze_phase,
                                               record_only=hashes_record_only)
                        complete_matchers.append(MATCHER_HASHES)
                    except Exception as ex:
                        if not hashes_record_only:
                            raise
                        # the comparison must never break the deduplication by image-match
                        LOGGER.exception(ex)
                        echo(f"Hash matching failed, continuing without it: {ex}", color='red')
                        self._hash_summary["hashes_error"] = f"{type(ex).__name__}: {ex}"
                        while phase_index < phase_count - 2:
                            next_phase("Skipped (hash matching failed)")

                # Phase 5/6: Move or Delete duplicate files
                phase_text = next_phase("Moving/deleting duplicates")
                self.process_duplicates(phase_label=phase_text.split(":")[0])

                phase_text = next_phase("Removing empty folders")
                self.remove_empty_folders(phase_label=phase_text.split(":")[0])

                self._resolve_unseen_groups(complete_matchers)
            except BaseException as ex:
                self.end_run(run_started, error=f"{type(ex).__name__}: {ex}")
                raise
            self.end_run(run_started)

        return self._deduplication_result

    def analyze_files_with_hashes(self, directories: List[Path]):
        """
        Computes the hash pipeline signatures of all new/changed files (the analysis phase of the hash pipeline).
        :param directories: source directories
        """
        matcher = self.hash_matcher
        files = matcher.find_files(directories)
        task = "Analyzing files (hashes)"
        started = False

        def progress(done: int, total: int):
            nonlocal started
            if not started:
                self._progress_manager.start(task, total, "Files", self.interactive)
                started = True
            self._progress_manager.inc()

        try:
            result = matcher.analyse(files, progress)
        finally:
            if started:
                self._progress_manager.clear()
        echo(f"Hash signatures: {result.analysed} analysed, {result.skipped_unchanged} unchanged, "
             f"{result.removed_missing} removed, {len(result.failed)} failed")
        self._hash_summary.update({"hashes_analysed": result.analysed, "hashes_failed": len(result.failed)})
        self._record_hash_failures(result.failed)

    def _record_hash_failures(self, failed: dict):
        """
        Records files the hash pipeline could not analyse, forgets earlier failures of files that work now.
        :param failed: path -> error message
        """
        if self._state is None:
            return
        try:
            for path in self._state.failure_paths(phase=HASH_ANALYSIS_FAILURE_PHASE):
                if path not in failed:
                    self._state.clear_failure(path)
            for path, error in failed.items():
                self._state.record_failure(path, error, phase=HASH_ANALYSIS_FAILURE_PHASE, run_id=self._run_id)
        except Exception as ex:
            LOGGER.exception(ex)

    def _run_hash_matcher(self, directories: List[Path], next_phase, skip_analyze_phase: bool, record_only: bool):
        """
        Runs the hash pipeline (analysis + matching) and records its groups. With record_only (backend "both")
        the groups are only recorded, otherwise they are decided by the prioritization rules like image-match groups
        (groups the pipeline does not allow to act on automatically are held for review).

        :param directories: source directories
        :param next_phase: starts the next phase of the run, returns its console label
        :param skip_analyze_phase: don't analyse new/changed files
        :param record_only: never act on the groups
        """
        phase_text = next_phase("Analyzing files (hashes)")
        if skip_analyze_phase:
            echo(phase_text + " - Skipping", color='yellow')
        else:
            echo(phase_text, color='cyan')
            self.analyze_files_with_hashes(directories)

        echo(next_phase("Finding duplicates (hashes)") + " ...", color='cyan')
        from py_image_dedup.matching.pipeline import STAGE_CLUSTER, STAGE_EMBEDDINGS, STAGE_HASHES
        stage_tasks = {
            STAGE_HASHES: ("Finding duplicates (hashes)", "Steps"),
            STAGE_EMBEDDINGS: ("Computing embeddings (crop detection)", "Files"),
            STAGE_CLUSTER: ("Grouping duplicates (hashes)", "Steps"),
        }
        current = {"stage": None, "done": 0}

        def progress(stage: str, done: int, total: int):
            if stage != current["stage"]:
                if current["stage"] is not None:
                    self._progress_manager.clear()
                task, unit = stage_tasks.get(stage, (stage, "Steps"))
                self._progress_manager.start(task, total, unit, self.interactive)
                current.update(stage=stage, done=0)
            if done > current["done"]:
                self._progress_manager.inc(done - current["done"])
                current["done"] = done

        try:
            groups = self.hash_matcher.match(directories, progress)
        finally:
            if current["stage"] is not None:
                self._progress_manager.clear()

        self._progress_manager.start("Recording duplicate groups (hashes)", len(groups), "Groups", self.interactive)
        try:
            for group in groups:
                try:
                    self._process_hash_group(group, record_only)
                except Exception as ex:
                    LOGGER.exception(ex)
                    echo(f"Error processing duplicate group {group.paths}: {ex}", color='red')
                    self._group_recording_failed = True
                finally:
                    self._progress_manager.inc()
        finally:
            self._progress_manager.clear()
        self._hash_summary.update({
            "hashes_groups": len(groups),
            "hashes_review_groups": sum(1 for g in groups if not g.auto_act_allowed),
            "hashes_record_only": record_only,
        })

    def _process_hash_group(self, group, record_only: bool):
        """
        Ranks the members of a hash pipeline group with the prioritization rules, records it in the state store
        and (unless record_only) marks the duplicates for the move/delete phase.

        :param group: a matching.models.DuplicateGroup
        :param record_only: never act on the group
        """
        from py_image_dedup.library.hash_matcher import member_match_info
        members = member_match_info(group)
        candidates = []
        for path in group.paths:
            if not Path(path).is_file():
                # deleted since the analysis
                continue
            try:
                metadata = self._metadata_reader._create_metadata_dict(path)
            except Exception as ex:
                LOGGER.warning(f"Could not read the metadata of {path}: {ex}")
                continue
            confidence = members[path]["match_confidence"]
            candidates.append({
                MetadataKey.PATH.value: path,
                # rules like smaller-distance: the less sure the match, the "farther" the copy
                MetadataKey.DISTANCE.value: round(1.0 - confidence, 4) if confidence is not None else 0.0,
                MetadataKey.SCORE.value: 0,
                MetadataKey.METADATA.value: metadata,
            })
        if len(candidates) < 2:
            return

        files_to_keep, duplicates = self._select_images_to_delete(candidates)
        match_details = {"confidence": round(group.confidence, 4), "review_reasons": list(group.review_reasons),
                         "members": members}
        if self._state is None:
            # without a state store (CLI) there is no review: only act on groups the pipeline allows
            if record_only or not group.auto_act_allowed:
                return
            self._save_duplicates_for_result(files_to_keep, duplicates)
            return
        decision = self._record_group(files_to_keep, duplicates, matcher=MATCHER_HASHES,
                                      match_details=match_details, record_only=record_only)
        if decision is None:
            return
        self._save_duplicates_for_result(*decision)

    def analyze_directories(self, directory_map: dict):
        """
        Analyzes all files, generates identifiers (if necessary) and stores them for later access
        """
        threads = self._config.ANALYSIS_THREADS.value

        # load truncated images too
        # TODO: this causes an infinite loop on some (truncated) images
        # ImageFile.LOAD_TRUNCATED_IMAGES = True

        for directory, file_count in directory_map.items():
            self._progress_manager.start(f"Analyzing files in '{directory}'", file_count, "Files", self.interactive)
            self.__walk_directory_files(
                root_directory=directory,
                threads=threads,
                command=lambda root_dir, file_dir, file_path: self.analyze_file(file_path))
            self._progress_manager.clear()

    def find_duplicates_in_directories(self, directory_map: dict):
        """
        Finds duplicates in the given directories
        :param directory_map: map of directory path -> file count
        """
        self.reset_result()

        for directory, file_count in directory_map.items():
            self._progress_manager.start(f"Finding duplicates in '{directory}' ...", file_count, "Files",
                                         self.interactive)
            self.__walk_directory_files(
                root_directory=directory,
                threads=1,  # there seems to be no performance advantage in using multiple threads here
                command=lambda root_dir, _, file_path: self.find_duplicates_of_file(
                    root_directories=self._config.SOURCE_DIRECTORIES.value,
                    root_directory=root_dir,
                    reference_file_path=file_path
                )
            )
            self._progress_manager.clear()

    def cleanup_database(self, directories: List[Path]):
        """
        Removes database entries of files that don't exist on disk.
        Note that this cleanup will only consider files within one
        of the root directories specified in constructor, as other file paths
        might have been added on other machines.
        :param directories: directories in this run
        """
        # TODO: This iterates through all db entries - even the ones we are ignoring.
        # The db query should be improved to speed this up
        if self._persistence is None:
            return

        count, entries = self._persistence.get_all()
        if count <= 0:
            return

        self._progress_manager.start(f"Cleanup database", count, "entries", self.interactive)
        for entry in entries:
            try:
                image_entry = entry
                metadata = image_entry.get(MetadataKey.METADATA.value, {})

                file_path = Path(image_entry[MetadataKey.PATH.value])
                self._progress_manager.set_postfix(self._truncate_middle(str(file_path)))

                if MetadataKey.DATAMODEL_VERSION.value not in metadata:
                    echo(f"Removing db entry with missing db model version number: {file_path}")
                    self._persistence.remove(str(file_path))
                    continue

                data_version = metadata.get(MetadataKey.DATAMODEL_VERSION.value, -1)
                if data_version != self._persistence.DATAMODEL_VERSION:
                    echo(f"Removing db entry with old db model version: {file_path}")
                    self._persistence.remove(str(file_path))
                    continue

                # filter by files in at least one of the specified root directories
                # this is necessary because the database might hold items for other paths already
                # and those are not interesting to us
                if not any(root_dir in file_path.parents for root_dir in directories):
                    continue

                if not file_path.exists():
                    echo(f"Removing db entry for missing file: {file_path}")
                    self._persistence.remove(str(file_path))
            except Exception as e:
                logging.exception(e)
                echo(f"Error while cleaning up database entry {entry}: {e}")
                try:
                    image_entry = entry
                    file_path = Path(image_entry[MetadataKey.PATH.value])
                    self._persistence.remove(str(file_path))
                except Exception as e:
                    logging.exception(e)
                    echo(f"Error removing db entry: {e}")
            finally:
                self._progress_manager.inc()
        self._progress_manager.clear()

    def _forget_failures_of_missing_files(self):
        """
        Removes recorded failures of files that don't exist anymore from the state store.
        """
        if self._state is None:
            return
        try:
            for path in self._state.failure_paths():
                if not Path(path).exists():
                    self._state.clear_failure(path)
        except Exception as ex:
            LOGGER.exception(ex)

    def _resolve_unseen_groups(self, matchers: List[str] | None = None):
        """
        Marks groups of earlier runs that this full run did not find again (f.ex. because files
        were removed by another tool) as resolved in the state store.
        :param matchers: only groups of these matchers (the ones that saw all files in this run), None: all
        """
        if self._state is None or self._run_id is None:
            return
        if self._group_recording_failed:
            # a group that could not be recorded would look like it is gone
            LOGGER.warning("Not resolving unseen groups, because recording some groups failed")
            return
        try:
            count = self._state.resolve_unseen_groups(self._run_id, matchers)
            if count > 0:
                echo(f"Marked {count} duplicate groups that were not found again as resolved")
        except Exception as ex:
            LOGGER.exception(ex)

    def _remove_empty_folders(self, directories: List[Path], recursive: bool):
        """
        Searches for empty folders and removes them
        :param directories: directories to scan
        """
        dry_run = self._config.DRY_RUN.value

        # remove empty folders
        for directory in directories:
            empty_folders = self._find_empty_folders(directory, recursive, dry_run)
            self._remove_folders(directory, empty_folders, dry_run)

    def _count_files(self, directories: List[Path]) -> dict:
        """
        Counts the amount of files to analyze (used in progress) and stores them in a map
        :return map "directory path" -> "directory file count"
        """
        directory_map = {}

        self._progress_manager.start(f"Counting files", len(directories), "Dirs", self.interactive)
        for directory in directories:
            self._progress_manager.set_postfix(self._truncate_middle(directory))

            file_count = get_files_count(
                directory,
                self._config.RECURSIVE.value,
                self._config.FILE_EXTENSION_FILTER.value,
                self._config.EXCLUSIONS.value
            )
            directory_map[directory] = file_count

            self._progress_manager.inc()
        self._progress_manager.clear()

        return directory_map

    def __walk_directory_files(self, root_directory: Path, command, threads: int):
        """
        Walks through the files of the given directory
        :param root_directory: the directory to start with
        :param command: the method to execute for every file found
        :return: file_path -> identifier
        """
        with ThreadPoolExecutor(max_workers=threads, thread_name_prefix="py-image-dedup-walker") as executor:
            for (root, dirs, files) in os.walk(str(root_directory)):
                # root is the place you're listing
                # dirs is a list of directories directly under root
                # files is a list of files directly under root
                root = Path(root)

                for file in files:
                    file_path = Path(root, file)

                    # skip file in exclusion
                    if any(list(map(lambda x: x.search(str(file_path.absolute())), self._config.EXCLUSIONS.value))):
                        continue

                    # skip file with unwanted file extension
                    if not file_has_extension(file_path, self._config.FILE_EXTENSION_FILTER.value):
                        continue

                    # skip if not existent (probably already deleted)
                    if not file_path.exists():
                        self._progress_manager.inc()
                        continue

                    try:
                        executor.submit(util.reraise_with_stack(command), root_directory, root, file_path)
                    except Exception as e:
                        click.echo(e, err=True)
                        sys.exit(1)

                if not self._config.RECURSIVE.value:
                    return

    @ANALYSIS_TIME.time()
    def analyze_file(self, file_path: Path):
        """
        Analyzes a single file
        :param file_path: the file path
        """
        self._progress_manager.set_postfix(self._truncate_middle(file_path))

        try:
            self._persistence.add(str(file_path))
            if self._state is not None:
                self._state.clear_failure(str(file_path))
        except Exception as e:
            logging.exception(e)
            echo(f"Error analyzing file '{file_path}': {e}")
            if self._state is not None:
                try:
                    self._state.record_failure(str(file_path), f"{type(e).__name__}: {e}", phase="analysis",
                                               run_id=self._run_id)
                except Exception as ex:
                    LOGGER.exception(ex)
        finally:
            self._progress_manager.inc()

    @FIND_DUPLICATES_TIME.time()
    def find_duplicates_of_file(self, root_directories: List[Path], root_directory: Path, reference_file_path: Path):
        """
        Finds duplicates and marks all but the best copy as "to-be-deleted".
        :param root_directories: valid root directories
        :param root_directory: root directory of reference_file_path
        :param reference_file_path: the file to check for duplicates
        """
        self._progress_manager.inc()
        self._progress_manager.set_postfix(self._truncate_middle(reference_file_path))

        # remember processed files to prevent processing files in multiple directions
        if reference_file_path in self._processed_files:
            # already found a better candidate for this file
            return

        duplicate_candidates = self._persistence.find_similar(str(reference_file_path))
        duplicate_candidates = self._without_missing_files(duplicate_candidates, root_directories)

        if self._config.SEARCH_ACROSS_ROOT_DIRS.value:
            # filter by files in at least one of the specified root directories
            # this is necessary because the database might hold items for other paths already
            # and those are not interesting to us
            duplicate_candidates = [
                candidate for candidate in duplicate_candidates if
                any(root_dir in Path(candidate[MetadataKey.PATH.value]).parents for root_dir in root_directories)
            ]
        else:
            # filter by files in the same root directory
            duplicate_candidates = [
                candidate for candidate in duplicate_candidates if
                root_directory in Path(candidate[MetadataKey.PATH.value]).parents
            ]

        if len(duplicate_candidates) <= 0:
            echo(f"No duplication candidates found in database for '{reference_file_path}'. "
                 "This is an indication that the file has not been analysed yet or "
                 "there was an issue analysing it.",
                 color='yellow')

        if len(duplicate_candidates) <= 1:
            for candidate in duplicate_candidates:
                candidate_path = Path(candidate[MetadataKey.PATH.value])

                if candidate_path != reference_file_path:
                    echo(f"Unexpected unique duplication candidate '{candidate_path}' for "
                         f"reference file '{reference_file_path}'", color='yellow')

                self._processed_files[candidate_path] = True

            # nothing to do here since the result is unique
            return

        # sort by quality criteria and redo the search to use the best candidate as the reference image
        sorted_duplicate_candidates = self._sort_by_quality_descending(duplicate_candidates)
        new_reference_file_path = sorted_duplicate_candidates[0][MetadataKey.PATH.value]
        duplicate_candidates = self._persistence.find_similar(new_reference_file_path)
        duplicate_candidates = self._without_missing_files(duplicate_candidates, root_directories)
        if len(duplicate_candidates) <= 1:
            # the other copies disappeared in the meantime
            for candidate in duplicate_candidates:
                self._processed_files[Path(candidate[MetadataKey.PATH.value])] = True
            return

        candidates_to_keep, candidates_to_delete = self._select_images_to_delete(duplicate_candidates)
        if self._state is not None:
            decision = self._record_group(candidates_to_keep, candidates_to_delete)
            if decision is None:
                # ignored by the user, waiting for review, or the state could not be recorded: don't touch it
                return
            candidates_to_keep, candidates_to_delete = decision
        self._save_duplicates_for_result(candidates_to_keep, candidates_to_delete)

    def _without_missing_files(self, candidates: List[dict], root_directories: List[Path]) -> List[dict]:
        """
        Removes candidates whose file does not exist (anymore). The database still holds files
        that were deleted after the database cleanup (f.ex. during a long running analysis),
        those must neither end up in a duplicate group nor be chosen as the file to keep.
        Their database entries are removed, if they are located in one of the root directories
        (other paths might have been added on other machines, see cleanup_database).

        :param candidates: duplicate candidates as returned by the persistence backend
        :param root_directories: valid root directories
        :return: candidates whose file exists
        """
        existing = []
        for candidate in candidates:
            candidate_path = Path(candidate[MetadataKey.PATH.value])
            if candidate_path.exists():
                existing.append(candidate)
                continue

            if self._persistence is not None and any(root_dir in candidate_path.parents
                                                     for root_dir in root_directories):
                echo(f"Removing db entry of missing file: {candidate_path}", color='yellow')
                try:
                    self._persistence.remove(str(candidate_path))
                except Exception as ex:
                    LOGGER.warning(f"Could not remove db entry of missing file '{candidate_path}': {ex}")
            else:
                LOGGER.debug(f"Ignoring duplicate candidate outside of root directories that does not exist: "
                             f"{candidate_path}")
        return existing

    def _record_group(self, files_to_keep: List[dict], duplicates: List[dict], matcher: str = MATCHER_NAME,
                      match_details: dict | None = None, record_only: bool = False) -> tuple | None:
        """
        Records a duplicate group and the decision of the prioritization rules in the state store,
        and applies decisions the user made for this group in the web UI.

        :param files_to_keep: files the prioritization rules decided to keep (best first)
        :param duplicates: files the prioritization rules decided to move/delete
        :param matcher: the matcher that found the group
        :param match_details: hash pipeline only: {"confidence", "review_reasons", "members": {path: info}}
                              (see hash_matcher.member_match_info). Groups with review reasons are held for review.
        :param record_only: only record the group, never act on it (and don't touch the bookkeeping of the
                            acting matcher)
        :return: (files to keep, duplicates) to use, or None if no file of this group must be touched
        """
        try:
            # the backend might return the same file more than once, a group contains every file only once
            ranked, seen = [], set()
            for candidate in self._sort_by_quality_descending(files_to_keep + duplicates):
                if candidate[MetadataKey.PATH.value] not in seen:
                    seen.add(candidate[MetadataKey.PATH.value])
                    ranked.append(candidate)
            if len(ranked) < 2:
                return None
            decided_by, decision_reason = self._explain_decision(ranked)
            keep_path_set = set(c[MetadataKey.PATH.value] for c in files_to_keep)
            has_duplicates = any(d[MetadataKey.PATH.value] not in keep_path_set for d in duplicates)
            needs_review = has_duplicates and (decided_by is None or decided_by in WEAK_PRIORITIZATION_RULES)
            review_reason = None
            if needs_review:
                review_reason = f"The best copy was only chosen by the weak tie-breaker rule '{decided_by}'" \
                    if decided_by is not None else "All prioritization rules rated the copies equal"
            edit_rule = next((r for r in DeduplicatorConfig.PRIORITIZATION_RULES.value
                              if r.get("name") == "prefer-edited"), None)
            if has_duplicates and not needs_review and edit_rule is not None:
                edited = [_edit_marker(c, edit_rule) is not None for c in ranked]
                if any(edited) and not all(edited):
                    needs_review = True
                    review_reason = "Contains an edited copy and its original: check that the edit should " \
                                    "replace the original (ignore the group to keep both)"
            match_review_reasons = list(match_details["review_reasons"]) if match_details is not None else []
            if has_duplicates and match_review_reasons:
                # variants (grayscale, colour-edited, crop), burst shots or a low match confidence
                needs_review = True
                review_reason = "; ".join(r for r in [*match_review_reasons, review_reason] if r)
            member_details = match_details["members"] if match_details is not None else {}

            keep_paths = [c[MetadataKey.PATH.value] for c in files_to_keep]
            best_path = files_to_keep[0][MetadataKey.PATH.value]
            max_mod_time_diff = self._config.MAX_FILE_MODIFICATION_TIME_DELTA.value
            candidates = []
            for candidate in ranked:
                path = candidate[MetadataKey.PATH.value]
                if path == best_path:
                    keep_reason = "best copy according to the prioritization rules"
                elif path in keep_paths and self._protected_pattern(path) is not None:
                    keep_reason = f"in a protected path ('{self._protected_pattern(path)}')"
                elif path in keep_paths:
                    keep_reason = f"file modification time differs more than {max_mod_time_diff} " \
                                  f"from the best copy, so it is not considered a duplicate"
                else:
                    keep_reason = None
                candidates.append(self._to_state_candidate(candidate, keep_reason, member_details.get(path)))

            group = self._state.record_group(
                run_id=self._run_id, matcher=matcher, candidates=candidates, keep_paths=keep_paths,
                decided_by=decided_by, decision_reason=decision_reason,
                needs_review=needs_review, review_reason=review_reason,
                match_confidence=match_details["confidence"] if match_details is not None else None,
                match_review_reasons=match_review_reasons if match_details is not None else None)
        except Exception as ex:
            LOGGER.exception(ex)
            echo(f"Error recording duplicate group in state database, not touching it: {ex}", color='red')
            self._group_recording_failed = True
            return None

        if record_only or not group.get("actionable", True):
            # f.ex. the hash pipeline with backend "both": recorded for comparison, never acted on
            return None

        for candidate in files_to_keep + duplicates:
            self._group_of_path[str(candidate[MetadataKey.PATH.value])] = group["id"]

        if group["status"] == GroupStatus.IGNORED:
            return None
        if group["decision_source"] == DecisionSource.USER:
            all_candidates = ranked
            keep = [c for c in all_candidates if c[MetadataKey.PATH.value] == group["keep_path"]]
            dont_keep = [c for c in all_candidates if c[MetadataKey.PATH.value] != group["keep_path"]]
            if len(keep) != 1:
                return None
            protected = [c for c in dont_keep if self._protected_pattern(c[MetadataKey.PATH.value]) is not None]
            return keep + protected, [c for c in dont_keep if c not in protected]
        if group.get("suggested_keep_path") and group["suggested_keep_path"] != group["keep_path"]:
            # another tool (f.ex. Immich) keeps a different file than the rules: wait for the user to decide
            return None
        if match_review_reasons:
            # the matcher does not allow automatic action (variants, burst shots, low confidence): wait for the user
            return None
        if group["needs_review"] and self._config.DEDUPLICATION_HOLD_AMBIGUOUS_GROUPS.value:
            return None
        return files_to_keep, duplicates

    @classmethod
    def _to_state_candidate(cls, candidate: dict, keep_reason: str | None, match_info: dict | None = None) -> dict:
        """
        Converts a candidate into the backend independent format of the state store.

        :param match_info: hash pipeline only: how the member was matched (see hash_matcher.member_match_info)
        """
        metadata = candidate.get(MetadataKey.METADATA.value, {})
        exif = metadata.get(MetadataKey.EXIF_DATA.value)
        stored_exif = None
        if isinstance(exif, dict):
            stored_exif = {
                str(k): v for k, v in exif.items()
                if not isinstance(v, (dict, list)) and len(str(v)) <= MAX_STORED_EXIF_VALUE_LENGTH
            }
        return {
            "path": candidate[MetadataKey.PATH.value],
            "keep_reason": keep_reason,
            "distance": candidate.get(MetadataKey.DISTANCE.value),
            "score": candidate.get(MetadataKey.SCORE.value),
            "file_size": metadata.get(MetadataKey.FILE_SIZE.value),
            "pixel_count": metadata.get(MetadataKey.PIXELCOUNT.value),
            "modified_at": metadata.get(MetadataKey.FILE_MODIFICATION_DATE.value),
            "exif_tag_count": len(exif) if isinstance(exif, dict) else None,
            "variant_kind": match_info.get("variant_kind") if match_info else None,
            "orientation": match_info.get("orientation") if match_info else None,
            "match_confidence": match_info.get("match_confidence") if match_info else None,
            "match_reason": match_info.get("match_reason") if match_info else None,
            "metadata": {
                "criteria": [{"rule": name, "value": display} for name, _, display in
                             cls._quality_criteria(candidate)],
                "exif": stored_exif,
                **({"match": {"notes": match_info.get("notes") or [], "partner": match_info.get("partner")}}
                   if match_info else {}),
            },
        }

    @classmethod
    def _explain_decision(cls, ranked_candidates: List[dict]) -> tuple:
        """
        Finds the prioritization rule that decided between the best and the second best candidate.

        :param ranked_candidates: candidates sorted by _sort_by_quality_descending()
        :return: (rule name or None, human readable reason)
        """
        if len(ranked_candidates) < 2:
            return None, "Only one candidate"
        best, runner_up = ranked_candidates[0], ranked_candidates[1]
        # compare exactly like the tuple comparison used for sorting
        for (name, best_value, best_display), (_, other_value, other_display) in zip(
            cls._quality_criteria(best), cls._quality_criteria(runner_up)):
            if best_value != other_value:
                runner_up_name = file.get_file_name(runner_up[MetadataKey.PATH.value])
                return name, f"Rule '{name}' decided: {best_display} (kept) vs. {other_display} " \
                             f"('{runner_up_name}', second best)"
        return None, "All prioritization rules rated the best two copies equal"

    def _save_duplicates_for_result(self, files_to_keep: List[dict], duplicates: List[dict]) -> None:
        """
        Saves the comparison result for the final summary

        :param files_to_keep: list of image that shall be kept
        :param duplicates: less good duplicates
        """
        self._deduplication_result.set_file_duplicates(files_to_keep, duplicates)

        for file_to_keep in files_to_keep:
            file_path = Path(file_to_keep[MetadataKey.PATH.value])
            self._deduplication_result.add_file_action(file_path, ActionEnum.NONE)

        if self._config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value is None:
            action = ActionEnum.DELETE
        else:
            action = ActionEnum.MOVE
        for duplicate in duplicates:
            file_path = Path(duplicate[MetadataKey.PATH.value])
            self._deduplication_result.add_file_action(file_path, action)

    def _select_images_to_delete(self, duplicate_candidates: [{}]) -> tuple:
        """
        Selects which image to keep and which to remove
        :return: tuple (image to keep, list of images to remove)
        """
        duplicate_candidates = self._sort_by_quality_descending(duplicate_candidates)

        # keep first and mark others for removal
        keep = [duplicate_candidates[0]]
        dont_keep = duplicate_candidates[1:]

        # move files that don't fit criteria to "keep" list
        max_mod_time_diff = self._config.MAX_FILE_MODIFICATION_TIME_DELTA.value
        if max_mod_time_diff is not None:
            # filter files that don't match max mod time diff criteria
            best_candidate = keep[0]
            best_match_mod_timestamp = best_candidate[MetadataKey.METADATA.value][
                MetadataKey.FILE_MODIFICATION_DATE.value]

            for c in dont_keep:
                c_timestamp = c[MetadataKey.METADATA.value][MetadataKey.FILE_MODIFICATION_DATE.value]
                timestamp_diff = abs(c_timestamp - best_match_mod_timestamp)
                difference = datetime.timedelta(seconds=timestamp_diff)
                if difference > max_mod_time_diff:
                    keep.append(c)
            dont_keep = list(filter(lambda x: x not in keep, dont_keep))

        # never touch files in protected paths
        protected = [c for c in dont_keep if self._protected_pattern(c[MetadataKey.PATH.value]) is not None]
        keep.extend(protected)
        dont_keep = [c for c in dont_keep if c not in protected]

        # remember that we have processed these files
        for candidate in duplicate_candidates:
            self._processed_files[candidate[MetadataKey.PATH.value]] = True

        return keep, dont_keep

    def _protected_pattern(self, path) -> str | None:
        """
        :return: the protected_paths pattern matching the given file path, None if it is not protected
        """
        for pattern in self._config.PROTECTED_PATHS.value or []:
            if pattern.search(str(path)):
                return pattern.pattern
        return None

    @classmethod
    def _sort_by_quality_descending(cls, duplicate_candidates) -> []:
        """
        Sorts images according to the desired priorities.
        The first item in the list will be the most preferred one of all found duplicates.

        :param duplicate_candidates: the images to analyze
        :return: duplicate candidates sorted by given criteria
        """

        def sort_criteria(candidate: dict) -> ():
            return tuple(value for _, value, _ in cls._quality_criteria(candidate))

        duplicate_candidates = sorted(duplicate_candidates, key=sort_criteria)

        return duplicate_candidates

    @staticmethod
    def _quality_criteria(candidate: dict) -> List[tuple]:
        """
        Evaluates the configured prioritization rules for a candidate.
        Sorting by the tuple of all sort values orders the best candidate first.

        :param candidate: the image to evaluate
        :return: list of (rule name, sort value, human readable value)
        """
        criteria = []
        metadata = candidate[MetadataKey.METADATA.value]

        for rule in DeduplicatorConfig.PRIORITIZATION_RULES.value:
            rule_name = rule.get("name")
            if rule_name == "more-exif-data":
                if MetadataKey.EXIF_DATA.value in metadata:
                    # more exif data is better
                    exif_count = len(metadata[MetadataKey.EXIF_DATA.value])
                    criteria.append((rule_name, exif_count * -1, f"{exif_count} EXIF tags"))
            elif rule_name == "less-exif-data":
                if MetadataKey.EXIF_DATA.value in metadata:
                    # more exif data is better
                    exif_count = len(metadata[MetadataKey.EXIF_DATA.value])
                    criteria.append((rule_name, exif_count * 1, f"{exif_count} EXIF tags"))
            elif rule_name == "bigger-file-size":
                # reverse, bigger is better
                file_size = metadata[MetadataKey.FILE_SIZE.value]
                criteria.append((rule_name, file_size * -1, f"{file_size} bytes"))
            elif rule_name == "smaller-file-size":
                # smaller is better
                file_size = metadata[MetadataKey.FILE_SIZE.value]
                criteria.append((rule_name, file_size * 1, f"{file_size} bytes"))
            elif rule_name == "newer-file-modification-date":
                # reverse, bigger (later time) is better
                mod_date = metadata[MetadataKey.FILE_MODIFICATION_DATE.value]
                criteria.append((rule_name, mod_date * -1, f"modified {_format_timestamp(mod_date)}"))
            elif rule_name == "older-file-modification-date":
                # smaller (earlier time) is better
                mod_date = metadata[MetadataKey.FILE_MODIFICATION_DATE.value]
                criteria.append((rule_name, mod_date * 1, f"modified {_format_timestamp(mod_date)}"))
            elif rule_name == "smaller-distance":
                # smaller distance is better
                distance = candidate[MetadataKey.DISTANCE.value]
                criteria.append((rule_name, distance, f"distance {round(distance, 4)}"))
            elif rule_name == "bigger-distance":
                # bigger distance is better
                distance = candidate[MetadataKey.DISTANCE.value]
                criteria.append((rule_name, distance * -1, f"distance {round(distance, 4)}"))
            # elif rule_name == "longer-path":
            # elif rule_name == "shorter-path":
            elif rule_name == "contains-copy-in-file-name":
                # if the filename contains "copy" it is less good
                contains_copy = "copy" in file.get_file_name(candidate[MetadataKey.PATH.value]).lower()
                criteria.append((rule_name, contains_copy, f"'copy' in file name: {contains_copy}"))
            elif rule_name == "doesnt-contain-copy-in-file-name":
                # if the filename contains "copy" it is better
                contains_copy = "copy" in file.get_file_name(candidate[MetadataKey.PATH.value]).lower()
                criteria.append((rule_name, not contains_copy, f"'copy' in file name: {contains_copy}"))
            elif rule_name == "longer-file-name":
                # longer filename is better (for "edited" versions)
                length = len(file.get_file_name(candidate[MetadataKey.PATH.value]))
                criteria.append((rule_name, length * -1, f"file name length {length}"))

            elif rule_name == "shorter-file-name":
                # shorter filename is better (for "edited" versions)
                length = len(file.get_file_name(candidate[MetadataKey.PATH.value]))
                criteria.append((rule_name, length * 1, f"file name length {length}"))

            elif rule_name == "longer-folder-path":
                # shorter folder path is better
                length = len(file.get_containing_folder(candidate[MetadataKey.PATH.value]))
                criteria.append((rule_name, length * -1, f"folder path length {length}"))
            elif rule_name == "shorter-folder-path":
                # shorter folder path is better
                length = len(file.get_containing_folder(candidate[MetadataKey.PATH.value]))
                criteria.append((rule_name, length, f"folder path length {length}"))
            elif rule_name == "higher-score":
                # reverse, bigger is better
                score = candidate[MetadataKey.SCORE.value]
                criteria.append((rule_name, score * -1, f"score {score}"))
            elif rule_name == "lower-score":
                # lower is better
                score = candidate[MetadataKey.SCORE.value]
                criteria.append((rule_name, score * 1, f"score {score}"))
            elif rule_name in ("preferred-paths", "avoided-paths"):
                # path patterns (regular expressions) in priority order: matching an earlier pattern is better.
                # preferred-paths: files matching no pattern come after all matching files,
                # avoided-paths: files matching no pattern come before all matching files
                patterns = rule.get("patterns") or []
                path = str(candidate[MetadataKey.PATH.value])
                index = next((i for i, pattern in enumerate(patterns) if _path_matches(pattern, path)), None)
                if index is None:
                    value = len(patterns) if rule_name == "preferred-paths" else -1
                    text = f"no {rule_name} pattern matches"
                else:
                    value = index
                    text = f"matches {rule_name} pattern '{patterns[index]}'"
                criteria.append((rule_name, value, text))
            elif rule_name == "prefer-edited":
                # intentionally edited copies are better than their original
                marker = _edit_marker(candidate, rule)
                criteria.append((rule_name, 0 if marker else 1, f"edited: {marker}" if marker else "not edited"))
            elif rule_name == "higher-pixel-count":
                # higher pixel count is better
                pixel_count = metadata[MetadataKey.PIXELCOUNT.value]
                criteria.append((rule_name, pixel_count * -1, f"{pixel_count} pixels"))
            elif rule_name == "lower-pixel-count":
                # lower pixel count is better
                pixel_count = metadata[MetadataKey.PIXELCOUNT.value]
                criteria.append((rule_name, pixel_count * 1, f"{pixel_count} pixels"))

        # just to assure the order in the result is the same
        # if all other criteria (above) are equal
        # and recurring runs will result in the same order
        # (although they shouldn't be compared twice to begin with)
        path = candidate[MetadataKey.PATH.value]
        criteria.append(("file-path", path, str(path)))

        return criteria

    def process_duplicates(self, phase_label: str = "Phase 5/6"):
        """
        Moves or removes duplicates based on the configuration
        :param phase_label: prefix of the console output
        """
        dry_run = self._config.DRY_RUN.value
        duplicate_target_directory = self._config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value
        if duplicate_target_directory:
            echo(f"{phase_label}: Moving duplicates ...", color='cyan')
            self._move_files_marked_as_delete(duplicate_target_directory, dry_run)
        else:
            echo(f"{phase_label}: Removing duplicates ...", color='cyan')
            self._remove_files_marked_as_delete(dry_run)

        if self._state is not None and not dry_run:
            acted_on = set(
                self._group_of_path[str(p)] for p in self._deduplication_result.get_removed_or_moved_files()
                if str(p) in self._group_of_path)
            try:
                self._state.mark_resolved(acted_on - self._failed_groups)
            except Exception as ex:
                LOGGER.exception(ex)

        self._trigger_immich_rescan()

    def _trigger_immich_rescan(self):
        """
        Queues a scan of the Immich external libraries (if enabled) when files were really moved or deleted
        since the last call. Runs in the background and never raises: Immich being unreachable must not
        affect deduplication.
        """
        changed_paths, self._changed_paths = self._changed_paths, []
        if not changed_paths or not self._config.IMMICH_RESCAN_AFTER_CHANGES.value:
            return
        url = self._config.IMMICH_URL.value
        api_key = self._config.IMMICH_API_KEY.value
        if not (url and api_key):
            LOGGER.warning("immich.rescan_after_changes is enabled, but immich.url or immich.api_key is missing")
            return
        try:
            client = ImmichClient(url, api_key, path_mappings=self._config.IMMICH_PATH_MAPPINGS.value, timeout=10)
        except Exception as ex:
            LOGGER.warning(f"Could not trigger an Immich library rescan: {ex}")
            return
        immich_paths = [p for p in (client.to_immich_path(p) for p in changed_paths) if p is not None]
        self._run_in_background(lambda: self._rescan_immich_libraries(client, immich_paths))

    @staticmethod
    def _rescan_immich_libraries(client: ImmichClient, immich_paths: List[str]):
        try:
            library_ids = client.rescan_libraries(immich_paths)
            LOGGER.info(f"Queued a scan of {len(library_ids)} Immich external library(ies)")
        except Exception as ex:
            LOGGER.warning(f"Could not trigger an Immich library rescan: {ex}")

    @staticmethod
    def _run_in_background(function):
        threading.Thread(target=function, name="immich-rescan").start()

    def execute_user_decisions(self) -> int:
        """
        Executes the decisions users made in the web UI (chosen keeper of a duplicate group).
        Duplicates are only ever moved to the duplicates_target_directory, never deleted,
        and nothing is touched when dry_run (or dry_run_user_decisions, if set) is enabled.

        :return: number of executed group decisions
        """
        if self._state is None:
            return 0
        with self.work_lock:
            groups = self._state.pending_executions()
            for group in groups:
                try:
                    self._execute_user_decision(group)
                except Exception as ex:
                    LOGGER.exception(ex)
                    self._state.record_action(path=group["keep_path"] or "", action="move",
                                              source=DecisionSource.USER, dry_run=self._config.user_decisions_dry_run,
                                              status=ActionStatus.FAILED, message=f"{type(ex).__name__}: {ex}",
                                              group_id=group["id"])
                    self._state.mark_executed(group["id"], resolved=False)
            self._trigger_immich_rescan()
            return len(groups)

    def _execute_user_decision(self, group: dict):
        if not self._state.is_actionable(group.get("matcher")):
            # record-only group (see StateStore.pending_executions, set_user_keeper refuses these)
            LOGGER.warning(f"Not executing the decision of record-only group {group['id']} ({group.get('matcher')})")
            self._state.mark_executed(group["id"], resolved=False)
            return
        dry_run = self._config.user_decisions_dry_run
        target_dir = self._config.DEDUPLICATOR_DUPLICATES_TARGET_DIRECTORY.value
        group_id = group["id"]
        keep_path = Path(group["keep_path"])
        duplicates = [Path(c["path"]) for c in group["candidates"] if c["path"] != group["keep_path"]]
        for path in duplicates:
            self._group_of_path[str(path)] = group_id
        self._failed_groups.discard(group_id)

        protected = [p for p in duplicates if self._protected_pattern(p) is not None]
        for path in protected:
            self._record_action(path, "move", ActionStatus.SKIPPED, dry_run=dry_run, source=DecisionSource.USER,
                                message=f"in a protected path ('{self._protected_pattern(path)}')")
        duplicates = [p for p in duplicates if p not in protected]

        reason = None
        if target_dir is None:
            reason = "duplicates_target_directory is not configured: decisions from the web UI only move files, " \
                     "they never delete them"
        elif not keep_path.exists():
            reason = f"the file to keep does not exist (anymore): {keep_path}, not touching the other copies"
        if reason is not None:
            for path in duplicates:
                self._record_action(path, "move", ActionStatus.SKIPPED, dry_run=dry_run,
                                    source=DecisionSource.USER, message=reason)
            self._state.mark_executed(group_id, resolved=False)
            return

        echo(f"Executing web UI decision for duplicate group {group_id}: keeping '{keep_path}'")
        self._progress_manager.start(f"Executing decision for group {group_id}", len(duplicates), "File", False)
        try:
            self._move_files(duplicates, target_dir, dry_run, source=DecisionSource.USER)
        finally:
            self._progress_manager.clear()
        self._state.mark_executed(group_id, resolved=not dry_run and group_id not in self._failed_groups)

    def execute_restore_requests(self) -> int:
        """
        Undoes moves requested in the web UI: moves a moved file back to its original location.
        A file is only restored if the original location is free (never overwrites anything), and nothing
        is touched when dry_run (or dry_run_user_decisions, if set) is enabled.
        The groups of a restored file are set to ignored, so the file is not moved again.

        :return: number of handled requests
        """
        if self._state is None:
            return 0
        with self.work_lock:
            requests = self._state.pending_restores()
            for move in requests:
                try:
                    self._execute_restore(move)
                except Exception as ex:
                    LOGGER.exception(ex)
                    self._record_restore(move, ActionStatus.FAILED, message=f"{type(ex).__name__}: {ex}")
                finally:
                    self._state.finish_restore(move["id"])
            self._trigger_immich_rescan()
            return len(requests)

    def _execute_restore(self, move: dict):
        """
        :param move: the move action to undo
        """
        dry_run = self._config.user_decisions_dry_run
        original = Path(move["path"])
        moved = Path(move["target"])

        reason = None
        if not move["undoable"]:
            reason = "the file was moved again or restored in the meantime"
        elif not moved.is_file():
            reason = f"the moved file does not exist (anymore): {moved}"
        elif original.exists() or original.is_symlink():
            reason = "the original location is not free, never overwriting a file"
        if reason is not None:
            self._record_restore(move, ActionStatus.SKIPPED, message=reason)
            return

        if dry_run:
            self._record_restore(move, ActionStatus.SIMULATED)
            return

        echo(f"Restoring '{moved}' to '{original}'")
        original.parent.mkdir(parents=True, exist_ok=True)
        _move_without_overwrite(moved, original)
        self._record_restore(move, ActionStatus.DONE)
        group_ids = [move["group_id"]] if move["group_id"] is not None else self._state.group_ids_of_path(original)
        for group_id in group_ids:
            self._state.mark_restored(group_id)

    def _record_restore(self, move: dict, status: str, message: str | None = None):
        """
        Writes the undo of a move to the action log: path is the original location, target the location
        the file was moved back from.
        """
        if status == ActionStatus.DONE and not self._config.user_decisions_dry_run:
            self._changed_paths.append(str(move["path"]))
        try:
            self._state.record_action(path=move["path"], action="restore", source=DecisionSource.USER,
                                      dry_run=self._config.user_decisions_dry_run, status=status,
                                      target=move["target"], message=message, group_id=move["group_id"])
        except Exception as ex:
            LOGGER.exception(ex)

    def _record_action(self, path: Path, action: str, status: str, dry_run: bool, source: str = DecisionSource.RULES,
                       target: Path | None = None, message: str | None = None):
        """
        Writes a file action to the action log of the state store (if any). Never raises.
        """
        if status == ActionStatus.DONE and not dry_run and action in ("move", "delete"):
            self._changed_paths.append(str(path))
        group_id = self._group_of_path.get(str(path))
        if status == ActionStatus.FAILED and group_id is not None:
            self._failed_groups.add(group_id)
        if self._state is None:
            return
        try:
            self._state.record_action(path=str(path), action=action, source=source, dry_run=dry_run, status=status,
                                      target=str(target) if target is not None else None, message=message,
                                      run_id=self._run_id, group_id=group_id)
        except Exception as ex:
            LOGGER.exception(ex)

    def _find_empty_folders(self, root_path: Path, recursive: bool, dry_run: bool) -> [str]:
        """
        Finds empty folders within the given root_path
        :param root_path: folder to search in
        """
        result = OrderedSet()

        # traverse bottom-up to remove folders that are empty due to file removal
        for root, directories, files in os.walk(str(root_path), topdown=False):
            # get absolute paths of all files and folders in the current root directory
            abs_file_paths = list(map(lambda x: os.path.abspath(os.path.join(root, x)), files))
            abs_folder_paths = list(map(lambda x: os.path.abspath(os.path.join(root, x)), directories))

            # find out which of those files were deleted by the deduplication process
            files_deleted = list(
                map(lambda x: Path(x), filter(
                    lambda x: Path(x) in self._deduplication_result.get_removed_or_moved_files(),
                    abs_file_paths)))
            files_deleted = list(set(files_deleted + list(
                filter(lambda x: x.parent == Path(root), self._deduplication_result.get_removed_or_moved_files()))))

            folders_deleted = list(filter(lambda x: x in result, abs_folder_paths))
            filtered_directories = list(filter(lambda x: x not in folders_deleted, abs_folder_paths))

            if dry_run:
                if len(files_deleted) > 0 and len(files_deleted) == len(files) and len(folders_deleted) == len(
                    directories):
                    result.append(root)
            else:
                if len(files_deleted) > 0 and len(files) <= 0 and len(directories) <= 0:
                    result.append(root)

            if not recursive:
                break

        return result

    def _remove_folders(self, root_path: Path, folders: [str], dry_run: bool):
        """
        Function to remove empty folders
        :param root_path:
        """
        echo(f"Removing empty folders ({len(folders)}) in: '{root_path}' ...")

        if len(folders) == 0:
            return

        self._progress_manager.start("Removing empty folders", len(folders), "Folder", self.interactive)
        for folder in folders:
            self._progress_manager.set_postfix(self._truncate_middle(folder))

            if not dry_run:
                os.rmdir(folder)
            self._record_action(folder, "remove_folder", ActionStatus.SIMULATED if dry_run else ActionStatus.DONE,
                                dry_run=dry_run)

            self._deduplication_result.add_removed_empty_folder(folder)
            self._progress_manager.inc()
        self._progress_manager.clear()

    def _remove_files_marked_as_delete(self, dry_run: bool):
        """
        Removes files that were marked to be deleted in previous deduplication step
        :param dry_run: set to true to simulate this action
        """
        items_to_remove = self._deduplication_result.get_file_with_action(ActionEnum.DELETE)
        marked_files_count = len(items_to_remove)
        if marked_files_count == 0:
            return

        self._progress_manager.start("Removing files", marked_files_count, "File", self.interactive)
        self._delete_files(items_to_remove, dry_run)
        self._progress_manager.clear()

    def _move_files_marked_as_delete(self, target_dir: Path, dry_run: bool):
        """
        Moves files that were marked to be deleted in previous deduplication step to the target directory
        :param target_dir: the directory to move duplicates to
        :param dry_run: set to true to simulate this action
        """
        items_to_move = self._deduplication_result.get_file_with_action(ActionEnum.MOVE)
        marked_files_count = len(items_to_move)
        if marked_files_count == 0:
            return

        self._progress_manager.start("Moving files", marked_files_count, "File", self.interactive)
        self._move_files(items_to_move, target_dir, dry_run)
        self._progress_manager.clear()

    def _delete_files(self, files_to_delete: [str], dry_run: bool):
        """
        Deletes files on disk
        :param files_to_delete: list of absolute file paths
        :param dry_run: set to true to simulate this action
        """
        for file_path in files_to_delete:
            self._progress_manager.set_postfix(self._truncate_middle(file_path))

            if dry_run:
                self._record_action(file_path, "delete", ActionStatus.SIMULATED, dry_run=dry_run)
            else:
                try:
                    # remove from file system
                    existed = os.path.exists(file_path)
                    if existed:
                        os.remove(file_path)

                    # remove from persistence
                    if self._persistence is not None:
                        self._persistence.remove(file_path)
                except Exception as ex:
                    self._record_action(file_path, "delete", ActionStatus.FAILED, dry_run=dry_run,
                                        message=f"{type(ex).__name__}: {ex}")
                    raise

                DUPLICATE_ACTION_DELETE_COUNT.inc()
                if existed:
                    self._record_action(file_path, "delete", ActionStatus.DONE, dry_run=dry_run)
                else:
                    self._record_action(file_path, "delete", ActionStatus.SKIPPED, dry_run=dry_run,
                                        message="file does not exist (anymore)")

            self._progress_manager.inc()

    def _move_files(self, files_to_move: List[Path], target_dir: Path, dry_run: bool,
                    source: str = DecisionSource.RULES):
        """
        Moves files on disk
        :param files_to_move: list of absolute file paths
        :param target_dir: directory to move files to
        :param source: who decided to move these files (for the action log)
        """
        for file_path in files_to_move:
            self._progress_manager.set_postfix(self._truncate_middle(file_path))

            target_file = Path(str(target_dir), *file_path.parts[1:])
            try:
                if dry_run:
                    self._record_action(file_path, "move", ActionStatus.SIMULATED, dry_run=dry_run, source=source,
                                        target=target_file)
                    continue

                # move file
                if not file_path.exists():
                    self._record_action(file_path, "move", ActionStatus.SKIPPED, dry_run=dry_run, source=source,
                                        target=target_file, message="file does not exist (anymore)")
                    continue

                message = None
                if target_file.exists():
                    if filecmp.cmp(file_path, target_file, shallow=False):
                        os.remove(file_path)
                        message = "an identical file already existed in the target directory, removed the duplicate"
                    else:
                        raise ValueError(f"Can't move duplicate file because the target already exists: {target_file}")
                else:
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(file_path, target_file)

                # remove from persistence (the hash pipeline forgets missing files on its next analysis)
                if self._persistence is not None:
                    self._persistence.remove(str(file_path))

                DUPLICATE_ACTION_MOVE_COUNT.inc()
                self._record_action(file_path, "move", ActionStatus.DONE, dry_run=dry_run, source=source,
                                    target=target_file, message=message)
            except Exception as ex:
                logging.exception(ex)
                # LOGGER.log(ex)
                self._record_action(file_path, "move", ActionStatus.FAILED, dry_run=dry_run, source=source,
                                    target=target_file, message=f"{type(ex).__name__}: {ex}")
            finally:
                self._progress_manager.inc()

    @staticmethod
    def _truncate_middle(text: any, max_length: int = 50):
        text = str(text)
        if len(text) <= max_length:
            # string is already short-enough, fill up with spaces
            return text + ((max_length - len(text)) * " ")
        # half of the size, minus the 3 .'s
        n_2 = int(max_length / 2) - 3
        # whatever's left
        n_1 = max_length - n_2 - 3
        return '{0}...{1}'.format(text[:n_1], text[-n_2:])

    def remove_empty_folders(self, phase_label: str = "Phase 6/6"):
        phase_6_text = f"{phase_label}: Removing empty folders"
        if not self._config.REMOVE_EMPTY_FOLDERS.value:
            echo(phase_6_text + " - Skipping", color='yellow')
        else:
            echo(phase_6_text, color='cyan')
            self._remove_empty_folders(self._config.SOURCE_DIRECTORIES.value, self._config.RECURSIVE.value)
