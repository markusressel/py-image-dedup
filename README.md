# py-image-dedup [![Build Status](https://travis-ci.org/markusressel/py-image-dedup.svg?branch=master)](https://travis-ci.org/markusressel/gopass-chrome-importer) [![PyPI version](https://badge.fury.io/py/py-image-dedup.svg)](https://badge.fury.io/py/py-image-dedup)

**py-image-dedup** is a tool to scan through a library of photos, find duplicates and remove them
in a prioritized way.

It is build upon [Image-Match](https://github.com/ascribe/image-match) a very popular library to compute
a pHash for an image and store the result in an ElasticSearch backend for very high scalability.

[![asciicast](https://asciinema.org/a/3WbBxMXnZyT1QnuTP9fm37wkS.svg)](https://asciinema.org/a/3WbBxMXnZyT1QnuTP9fm37wkS)

# How to use

## Setup elasticsearch backend

### Elasticsearch version

This library requires elasticsearch version 5 or later. Sadly the
[Image-Match](https://github.com/ascribe/image-match) library 
specifies version 2 for no apparent reason, so you have to remove this
requirement from it's requirements.

Because of this **py-image-dedup** will exit with an **error on first install**.

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

**py-image-dedup** uses a single index called "images" that you can create using the following command:

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
| threads | Number of threads to use for image analysis | 2 |
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