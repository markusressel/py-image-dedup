FROM python:3.8-slim-buster

ENV PYTHONUNBUFFERED=1
ENV POETRY_VERSION="1.1.4"
ENV PIP_DISABLE_PIP_VERSION_CHECK=on

RUN apt-get update \
&& apt-get -y install sudo git python-skimage

WORKDIR /app

COPY . .

COPY poetry.lock pyproject.toml ./
RUN apk add gcc musl-dev libffi-dev g++
RUN pip install "poetry==$POETRY_VERSION" \
 && POETRY_VIRTUALENVS_CREATE=false poetry install \
 && pip uninstall -y poetry

ENV PUID=1000 PGID=1000

ENTRYPOINT [ "docker/entrypoint.sh", "py-image-dedup" ]
CMD [ "daemon" ]
