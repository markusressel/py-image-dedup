FROM python:3.6-slim-buster

RUN apt-get update \
&& apt-get -y install python-skimage git

WORKDIR /app

RUN pip install --no-cache-dir numpy
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN pip install --no-cache-dir .

CMD [ "py-image-dedup", "daemon" ]
