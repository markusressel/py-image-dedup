FROM python:3.8-slim-buster

RUN apt-get update \
&& apt-get -y install sudo git python-skimage

WORKDIR /app

COPY . .

# RUN pip install --no-cache-dir numpy
RUN pip install --upgrade pip
RUN pip install pipenv
RUN pipenv install --system --deploy
RUN pip install .

ENV PUID=1000 PGID=1000

ENTRYPOINT [ "docker/entrypoint.sh", "py-image-dedup" ]
CMD [ "daemon" ]
