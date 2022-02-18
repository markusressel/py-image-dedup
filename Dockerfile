# dont use alpine for python builds: https://pythonspeed.com/articles/alpine-docker-python/
FROM python:3.10-slim-buster

ENV PYTHONUNBUFFERED=1
ENV POETRY_VERSION="1.1.13"
ENV PIP_DISABLE_PIP_VERSION_CHECK=on

RUN apt-get update \
&& apt-get -y install sudo git python-skimage

WORKDIR /app

COPY . .

COPY poetry.lock pyproject.toml ./

RUN apt-get update && \
    apt-get install -y libatlas-base-dev gfortran && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip install "poetry==$POETRY_VERSION" \
 && POETRY_VIRTUALENVS_CREATE=false poetry install --no-dev \
 && pip uninstall -y poetry

ENV PUID=1000 PGID=1000

ENTRYPOINT [ "docker/entrypoint.sh", "py-image-dedup" ]
CMD [ "daemon" ]
