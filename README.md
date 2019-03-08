# py-image-dedup [![Build Status](https://travis-ci.org/markusressel/py-image-dedup.svg?branch=master)](https://travis-ci.org/markusressel/py-image-dedup) [![PyPI version](https://badge.fury.io/py/py-image-dedup.svg)](https://badge.fury.io/py/py-image-dedup)

**py-image-dedup** is a tool to scan through a library of photos, find duplicates and remove them
in a prioritized way.

It is build upon [Image-Match](https://github.com/ascribe/image-match) a very popular library to compute
a pHash for an image and store the result in an ElasticSearch backend for very high scalability.

[![asciicast](https://asciinema.org/a/3WbBxMXnZyT1QnuTP9fm37wkS.svg)](https://asciinema.org/a/3WbBxMXnZyT1QnuTP9fm37wkS)

# How it works

### Phase 1 - Database cleanup

In the first phase the elasticsearch backend is checked against the 
current filesystem state. Files that no longer exist are removed from
the database to speed up queries made in a later phase.

### Phase 2 - Counting files

Although not necessary for the deduplication process it is very convenient
to have some kind of progress indication while the deduplication process
is at work. To do this available files must be counted beforehand.

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
for this file will **not** be recalculated. Because of this supsequent
runs will be much faster. There still has to happen some file access though 
so it is probably limited by that. 
 
### Phase 4 - Finding duplicates

Every file is now processed again - but only by means of querying the 
database backend for similar images (within the given `max_dist`).
If there are images found that match the similarity criteria they are considered
duplicate candidates. All candidates are then ordered by the following
criteria (in this exact order):

1. file size (bigger is better)
1. file modification time (newer is better)
1. EXIF data (more exif data is better)
1. pixel count (more is better)
1. distance (lower is better)
1. filename contains "copy" (False is better)
1. filename length (longer is better) - (for "edited" versions)
1. parent folder path length (shorter is better)
1. score (higher is better)

The first candidate in the resulting list is considered to be the best
available version of all candidates.
 
### Phase 5 - Removing duplicates

All but the best version of duplicate candidates identified in the previous
phase are now deleted from the file system (if you did not specify `--dry-run` of course).  
 
### Phase 6 - Removing empty folders

In the last phase empty folders are deleted.

# How to use

## Setup elasticsearch backend

### Elasticsearch version

This library requires elasticsearch version 5 or later. Sadly the
[Image-Match](https://github.com/ascribe/image-match) library 
specifies version 2 for no apparent reason, so you have to remove this
requirement from it's requirements.

Because of this **py-image-dedup** might exit with an **error on first install**.

To fix this find the installed files of the image-match library, f.ex.

```
../venv/lib/python3.6/site-packages/image_match-1.1.2-py3.6.egg-info/requires.txt    
```

and remove the second line
```
elasticsearch<2.4,>=2.3
```

from the file.  
After that **py-image-dedup** should install and run as expected.

### Set up the index

Since this library is based on [Image-Match](https://github.com/ascribe/image-match) 
you need a running elasticsearch instance for efficient storing and 
querying of image signatures.

**py-image-dedup** uses a single index called `images` that you can create using the following command:

```shell
curl -X PUT "192.168.2.24:9200/images?pretty" -H "Content-Type: application/json" -d "
{
  \"mappings\": {
    \"image\": {
      \"properties\": {
        \"path\": {
          \"type\": \"keyword\",
          \"ignore_above\": 256
        }
      }
    }
  }
}
```

## Configuration

**py-image-dedup** offers customization options to make sure it can 
detect the best image with the highest probability possible.

| Name | Description | Default |
|------|-------------|---------|
| threads | Number of threads to use for image analysis | `2` |
| recursive | Toggle to analyse given directories recursively | `False` |
| search_across_dirs | Toggle to allow duplicate results across given directories | `False` |
| file_extensions | Comma separated list of file extensions to analyse | `"png,jpg,jpeg"` |
| max_dist | Maximum distance of image signatures to consider. This is a value in the range [0..1] | `0.1` |

## Command line usage

**py-image-dedup** can be used from the command line like this:

```shell
py-image-dedup deduplicate --help
```

Have a look at the help output to see how you can customize it.

## Dry run

To analyze images and get an overview of what images would be deleted 
be sure to make a dry run first.

```shell
py-image-dedup -d "/home/mydir" --dry-run
```

## FreeBSD

If you want to run this on a FreeBSD host make sure you have an up
to date release that is able to install ports.

Since [Image-Match](https://github.com/ascribe/image-match) does a lot of
math it relies on `numpy` and `scipy`. To get those working on FreeBSD
you have to install them as a port:

```
pkg install pkgconf
pkg install py36-numpy
pkg install py27-scipy
```

For `.png` support you also need to install
```
pkg install png
```

I still ran into issues after installing all these and just threw those
two in the mix and it finally worked:
```
pkg install freetype
pkg install py27-matplotlib  # this has a LOT of dependencies
```

### Encoding issues

When using the pythin library `click` on FreeBSD you might run into
encoding issues. To mitigate this change your locale from `ANSII` to `UTF-8`
if possible.

This can be achieved f.ex. by creating a file `~/.login_conf` with the following content:
```
me:\
	:charset=ISO-8859-1:\
	:lang=de_DE.UTF-8:
```


# Contributing

GitHub is for social coding: if you want to write code, I encourage contributions through pull requests from forks
of this repository. Create GitHub tickets for bugs and new features and comment on the ones that you are interested in.

# License

```
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
