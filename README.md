# py-image-dedup [![Build Status](https://img.shields.io/endpoint.svg?url=https%3A%2F%2Factions-badge.atrox.dev%2Fmarkusressel%2Fpy-image-dedup%2Fbadge%3Fref%3Dmaster&style=flat)](https://actions-badge.atrox.dev/markusressel/py-image-dedup/goto?ref=master) [![Code Climate](https://codeclimate.com/github/markusressel/py-image-dedup.svg)](https://codeclimate.com/github/markusressel/py-image-dedup) [![PyPI version](https://badge.fury.io/py/py-image-dedup.svg)](https://badge.fury.io/py/py-image-dedup)

**py-image-dedup** is a tool to sort out or remove duplicates within a photo library. 
Unlike most other solutions, **py-image-dedup** 
intentionally uses an approximate image comparison to also detect 
duplicates of images that slightly differ in resolution, color or other minor details.

It is build upon [Image-Match](https://github.com/ascribe/image-match) a very popular library to compute
a pHash for an image and store the result in an ElasticSearch backend for very high scalability.

[![asciicast](https://asciinema.org/a/3WbBxMXnZyT1QnuTP9fm37wkS.svg)](https://asciinema.org/a/3WbBxMXnZyT1QnuTP9fm37wkS)

# How it works

### Phase 1 - Database cleanup

In the first phase the elasticsearch backend is checked against the 
current filesystem state, cleaning up database entries of files that 
no longer exist. This will speed up queries made later on.

### Phase 2 - Counting files

Although not necessary for the deduplication process it is very convenient
to have some kind of progress indication while the deduplication process
is at work. To be able to provide that, available files must be counted beforehand.

### Phase 3 - Analysing files

In this phase every image file is analysed. This means generating a signature (pHash)
to quickly compare it to other images and adding other metadata of the image
to the elasticsearch backend that is used in the next phase.

This phase is quite CPU intensive and the first run take take quite
some time. Using as much threads as feasible (using the `-t` parameter) 
is advised to get the best performance.

Since we might already have a previous version of this file in the database 
before analysing a given file the file modification time is compared to the
given one. If the database content seems to be still correct the signature
for this file will **not** be recalculated. Because of this, subsequent
runs will be much faster. There still has to happen some file access though,
so it is probably limited by that.

### Phase 4 - Finding duplicates

Every file is now processed again - but only by means of querying the
database backend for similar images (within the given `max_dist`).
If there are images found that match the similarity criteria they are considered
duplicate candidates. All candidates are then ordered according to the `prioritization_rules`,
which you can specify yourself in the configuration, see [Configuration](#Configuration).

If you do not specify `prioritization_rules` yourself, the following order will
be used:

1. pixel count (more is better)
1. EXIF data (more exif data is better)
1. file size (bigger is better)
1. file modification time (newer is better)
1. distance (lower is better)
1. filename contains "copy" (False is better)
1. filename length (longer is better) - (for "edited" versions)
1. parent folder path length (shorter is better)
1. score (higher is better)

The first candidate in the resulting list is considered to be the best
available version of all candidates.

Two rules take a list of `patterns` (regular expressions, matched anywhere in the file path)
in priority order, f.ex. to prefer the original from the camera folder over copies sent via a messenger:

```yaml
prioritization_rules:
  - name: "higher-pixel-count"
  - name: "more-exif-data"
  - name: "bigger-file-size"
  # files in an earlier matching folder are better, files matching no pattern come last
  - name: "preferred-paths"
    patterns: ["/Familie/", "/Camera/"]
  # files matching no pattern are better, then in pattern order
  - name: "avoided-paths"
    patterns: ["/Immich/", "/Telegram ", "/WhatsApp "]
  # copies are usually younger than the original
  - name: "older-file-modification-date"
```

`prefer-edited` ranks intentionally edited copies before their original. A copy counts as edited if its file name
(without extension) matches one of `file_name_patterns` (default: `_edit`, `-edited`, `bearbeitet`, ...) or its
EXIF `Software` tag matches one of `software_patterns` (default: Lightroom, Photoshop, Snapseed, GIMP, darktable, ...).
As some edits are only made for fun, a group with an edited copy and its original is always marked for review
(with `hold_ambiguous_groups: true` nothing is moved until you decide; ignore the group to keep both).

Files matching one of `deduplication.protected_paths` (regular expressions, f.ex. curated albums) are never moved
or deleted, neither by the rules nor by decisions from the web UI. If the best copy of a group is somewhere else,
both are kept.
 
### Phase 5 - Moving/Deleting duplicates

All but the best version of duplicate candidates identified in the previous
phase are now deleted from the file system (if you didn't specify `--dry-run` of course).

If `duplicates_target_directory` is set, the specified folder will be used as
a root directory to move duplicates to, instead of deleting them, replicating their original 
folder structure.
 
### Phase 6 - Removing empty folders (Optional)

In the last phase, folders that are empty due to the deduplication 
process are deleted, cleaning up the directory structure (if turned on in configuration).

# How to use

## Install

Install **py-image-dedup** using pip:

```shell
pip3 install py-image-dedup
```

## Configuration

**py-image-dedup** uses [container-app-conf](https://github.com/markusressel/container-app-conf)
to provide configuration via a YAML file as well as ENV variables which
generates a reference config on startup. Have a look at the
[documentation about it](https://github.com/markusressel/container-app-conf#generate-reference-config).

See [py_image_dedup_reference.yaml](/py_image_dedup_reference.yaml)
for an example in this repo.

## Setup elasticsearch backend

Since this library is based on [Image-Match](https://github.com/ascribe/image-match) 
you need a running elasticsearch instance for efficient storing and 
querying of image signatures.

### Elasticsearch version

This library requires elasticsearch version 5 or later. Sadly the
[Image-Match](https://github.com/ascribe/image-match) library still 
specifies version 2, so [a fork of the original project](https://github.com/markusressel/image-match)
 is used instead. This fork is maintained by me, and any contributions
 are very much appreciated.

### Set up the index

**py-image-dedup** uses a single index (called `images` by default).
When configured, this index will be created automatically for you. 

## Command line usage

**py-image-dedup** can be used from the command line like this:

```shell
py-image-dedup deduplicate --help
```

Have a look at the help output to see how you can customize it.

### Daemon

**CAUTION!** This feature is still very much a work in progress. 
**Always** have a backup of your data! 

**py-image-dedup** has a built in daemon that allows you to continuously
monitor your source directories and deduplicate them on the fly.

When running the daemon (and enabled in configuration) a prometheus reporter
is used to allow you to gather some statistical insights.

```shell
py-image-dedup daemon
```

### Web UI

The daemon serves a web UI (and the JSON API it uses, under `/api`, documented at `/api/docs`)
on `web.port` (default `8080`, env `PY_IMAGE_DEDUP_WEB_PORT`). It shows:

* what the daemon is doing right now (phase, progress, queue size) and past runs
* duplicate groups with thumbnails, which copy is kept and which prioritization rule decided it.
  Groups that were only decided by a weak tie-breaker rule (e.g. file name length) are marked for review.
* the action log: every file that was (or, in a dry run, WOULD have been) moved or deleted
* the effective configuration with hints, e.g. files that failed analysis

For every duplicate group you can choose a different copy to keep, or ignore the group so the daemon
never touches it. The daemon executes these decisions within a few seconds (after a running deduplication
finished). Decisions from the web UI only ever **move** duplicates to `duplicates_target_directory`, they
never delete files, so they require `duplicates_target_directory` to be set. `dry_run` applies to them too:
while it is enabled, nothing on disk is touched and the action log shows what would have happened.
Set `dry_run_user_decisions: false` to execute your decisions while the prioritization rules stay in dry run.
The group list and the banner show whether your decisions are executed or only simulated.

The group list supports bulk actions: select groups (or all groups matching the current filter) and
**accept the rule decision** (the daemon executes the decision of the prioritization rules like a decision of
yours, requires `duplicates_target_directory`), **ignore** or **reset** them (`POST /api/groups/bulk`).
Resolved groups are never changed, accepting skips ignored groups and groups you already decided.

Moves can be undone from the action log or the group detail page (`POST /api/actions/{id}/undo`,
`POST /api/groups/{id}/undo`): the daemon moves the file back to its original location, but only if nothing
exists there (it never overwrites a file), logs it as a `restore` action and respects the dry run setting for
your decisions. The group of a restored file is set to **ignored**, so neither the rules nor a pending decision
move it again. Reset the group to let the rules act on it again.

Decisions made in another tool can be imported with `POST /api/decisions/import-discarded`
(`{"discarded_paths": [...], "apply": false}`, see `/api/docs`). Only open groups decided by the rules where
all but one member are in `discarded_paths` are changed, other groups (decided by you, ignored, resolved) are
left alone:

* **agrees with the rules**: the remaining file is the one the prioritization rules keep. It becomes your
  decision and the daemon moves the other files.
* **disagrees with the rules**: nothing is moved. The group goes to review (also with
  `hold_ambiguous_groups: false`) with the remaining file as suggestion ("Immich keeps ...") that you can
  accept with one click on the group page. Disagreements are worth a look: either the other tool picked a
  worse copy (f.ex. a messenger copy over the camera original) or your rules need tuning. The groups list can
  be filtered by groups with a suggestion.

Without `"apply": true` it only reports what would be decided, including a list of the disagreeing groups with
resolution, file size and EXIF tag count of both files and the rule that decided the group. Importing again
is safe: it only refreshes the suggestions.

### Immich

If your library is also an external library in [Immich](https://immich.app), you can use Immich's duplicate
review and let py-image-dedup move the copies you trashed there: configure `immich.url`, `immich.api_key`
(an API key with the `asset.read` permission of the user owning the library) and, if Immich sees the files
under another path, `immich.path_mappings`. The dashboard then shows an "Immich" card to preview and apply the
import of the Immich trash (`POST /api/integrations/immich/import-trash`, same rules as the import above:
only groups where Immich agrees with your prioritization rules are executed). Offline assets (files missing on
disk) are skipped. Exclude `duplicates_target_directory` from the Immich library, so moved files are not imported
again.

The group detail page links every file Immich knows to its asset in Immich ("open in Immich", looked up via
`GET /api/groups/{id}/immich`). Local paths are translated with the inverse of `immich.path_mappings`.
Set `immich.public_url` if the browser reaches Immich under another URL than `immich.url` (f.ex. a cluster
internal service URL). Finding assets by path uses the structured search filter of Immich v3.2.0 and newer,
older versions fall back to the deprecated `originalPath` search parameter.

With `immich.rescan_after_changes: true`, py-image-dedup queues a scan of the Immich external libraries
containing the affected files (of all external libraries, if none matches) after it really moved or deleted
files: at most once per deduplication run and once per batch of executed web UI decisions, never in dry run.
The scan runs in the background; if Immich is unreachable, only a warning is logged. Library scans are admin
endpoints in Immich: the API key must belong to an Immich admin and needs the `library.read` and `library.update`
permissions in addition to `asset.read`.

With `deduplication.hold_ambiguous_groups` enabled, groups that need a review are not acted on automatically.

The state shown in the web UI (runs, groups, decisions, action log) is stored in a small SQLite database
at `state.database_path` (env `PY_IMAGE_DEDUP_STATE_DATABASE_PATH`); generated thumbnails are cached next to it.
Prometheus metrics are also available at `/metrics` on the web port.

`GET /api/health` returns `{"status": "ok"}` and can be used for liveness/readiness probes.

**CAUTION!** The web UI has no authentication. It listens on `127.0.0.1` by default
(`PY_IMAGE_DEDUP_WEB_HOST`, the Docker image uses `0.0.0.0`). Only expose it to trusted networks.

## Dry run

To analyze images and get an overview of what images would be deleted 
be sure to make a dry run first.

```shell
py-image-dedup deduplicate --dry-run
```


## FreeBSD

If you want to run this on a FreeBSD host make sure you have an up
to date release that is able to install ports.

Since [Image-Match](https://github.com/ascribe/image-match) does a lot of
math it relies on `numpy` and `scipy`. To get those working on FreeBSD
you have to install them as a port:

```shell
pkg install pkgconf
pkg install py38-numpy
pkg install py27-scipy
```

For `.png` support you also need to install
```shell
pkg install png
```

I still ran into issues after installing all these and just threw those
two in the mix and it finally worked:
```shell
pkg install freetype
pkg install py27-matplotlib  # this has a LOT of dependencies
```

### Encoding issues

When using the python library `click` on FreeBSD you might run into
encoding issues. To mitigate this change your locale from `ANSII` to `UTF-8`
if possible.

This can be achieved f.ex. by creating a file `~/.login_conf` with the following content:
```text
me:\
	:charset=ISO-8859-1:\
	:lang=de_DE.UTF-8:
```

## Docker

To run **py-image-dedup** using docker you can use the [markusressel/py-image-dedup](https://hub.docker.com/r/markusressel/py-image-dedup) 
image from DockerHub (`linux/amd64` and `linux/arm64`):

```
docker run -t \
    -p 8000:8000 \
    -p 8080:8080 \
    -v /where/the/original/photolibrary/is/located:/data/in \
    -v /where/duplicates/should/be/moved/to:/data/out \
    -v /where/the/state/should/be/stored:/data/state \
    -e PY_IMAGE_DEDUP_DRY_RUN=False \
    -e PY_IMAGE_DEDUP_ANALYSIS_SOURCE_DIRECTORIES=/data/in/ \
    -e PY_IMAGE_DEDUP_ANALYSIS_RECURSIVE=True \
    -e PY_IMAGE_DEDUP_ANALYSIS_ACROSS_DIRS=True \
    -e PY_IMAGE_DEDUP_ANALYSIS_FILE_EXTENSIONS=.png,.jpg,.jpeg \
    -e PY_IMAGE_DEDUP_ANALYSIS_THREADS=8 \
    -e PY_IMAGE_DEDUP_ANALYSIS_USE_EXIF_DATA=True \
    -e PY_IMAGE_DEDUP_DEDUPLICATION_DUPLICATES_TARGET_DIRECTORY=/data/out/ \
    -e PY_IMAGE_DEDUP_ELASTICSEARCH_AUTO_CREATE_INDEX=True \
    -e PY_IMAGE_DEDUP_ELASTICSEARCH_HOST=elasticsearch \
    -e PY_IMAGE_DEDUP_ELASTICSEARCH_PORT=9200 \
    -e PY_IMAGE_DEDUP_ELASTICSEARCH_INDEX=images \
    -e PY_IMAGE_DEDUP_ELASTICSEARCH_AUTO_CREATE_INDEX=True \
    -e PY_IMAGE_DEDUP_ELASTICSEARCH_MAX_DISTANCE=0.1 \
    -e PY_IMAGE_DEDUP_ELASTICSEARCH_CONNECTION_TIMEOUT=2m \
    -e PY_IMAGE_DEDUP_REMOVE_EMPTY_FOLDERS=False \
    -e PY_IMAGE_DEDUP_STATS_ENABLED=True \
    -e PY_IMAGE_DEDUP_STATS_PORT=8000 \
    -e PY_IMAGE_DEDUP_WEB_PORT=8080 \
    markusressel/py-image-dedup:latest
```

The image exposes port `8000` (Prometheus metrics) and `8080` (web UI). It stores the state database at
`/data/state/state.db` (`PY_IMAGE_DEDUP_STATE_DATABASE_PATH`), mount a volume writable by `PUID`/`PGID` there
to keep the history across container restarts.

Since an elasticsearch instance is required too, you can 
also use the `docker-compose.yml` file included in this repo which will
set up a single-node elasticsearch cluster too:

```shell script
docker compose up
```

### UID and GID

To run **py-image-dedup** inside the container using a specific user id 
and group id you can use the env variables `PUID=1000` and `PGID=1000`.

# Contributing

GitHub is for social coding: if you want to write code, I encourage contributions through pull requests from forks
of this repository. Create GitHub tickets for bugs and new features and comment on the ones that you are interested in.

# License

```text
py-image-dedup by Markus Ressel
Copyright (C) 2018  Markus Ressel

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
```
