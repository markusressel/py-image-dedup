FROM python:3.6-slim-buster

RUN apt-get update \
&& apt-get -y install python-skimage git

# we use a forked version of image-match to support both EL v6 and v7
WORKDIR /tmp
RUN git clone https://github.com/markusressel/image-match.git
WORKDIR /tmp/image-match
RUN pip install --no-cache-dir .

# now we install py-image-dedup
WORKDIR /app

RUN pip install --no-cache-dir numpy
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN pip install --no-cache-dir .

CMD [ "py-image-dedup", "daemon" ]
