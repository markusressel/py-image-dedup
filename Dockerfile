FROM python:3.8-slim-buster

RUN apt-get update \
&& apt-get -y install python-skimage git

WORKDIR /app

COPY . .

# RUN pip install --no-cache-dir numpy
RUN pip install --upgrade pip
RUN pip install pipenv
RUN pipenv install --system --deploy

CMD [ "py-image-dedup", "daemon" ]
