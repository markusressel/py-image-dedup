# build the web UI (Vue single page app), only its static files end up in the final image
FROM node:24-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# dont use alpine for python builds: https://pythonspeed.com/articles/alpine-docker-python/
FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1
ENV POETRY_VERSION="2.3.2"
ENV PIP_DISABLE_PIP_VERSION_CHECK=on
ENV POETRY_VIRTUALENVS_CREATE=false

# sudo is used by docker/entrypoint.sh to drop privileges to PUID/PGID,
# which usually do not exist in /etc/passwd inside the container
RUN apt-get update \
 && apt-get install -y --no-install-recommends sudo \
 && apt-get clean && rm -rf /var/lib/apt/lists/* \
 && echo "Defaults runas_allow_unknown_id" > /etc/sudoers.d/runas-allow-unknown-id \
 && chmod 0440 /etc/sudoers.d/runas-allow-unknown-id

WORKDIR /app

# install dependencies first, so this layer is cached as long as the lock file does not change
COPY poetry.lock pyproject.toml ./
# poetry lives in a throwaway venv, so neither poetry nor its own dependencies end up in the image
RUN python -m venv /opt/poetry \
 && /opt/poetry/bin/pip install --no-cache-dir "poetry==$POETRY_VERSION" \
 && /opt/poetry/bin/poetry install --without dev --no-root --no-interaction \
 && rm -rf /opt/poetry /root/.cache

# install the project itself (provides the py-image-dedup script)
COPY . .
RUN pip install --no-cache-dir --no-deps .

COPY --from=web /web/dist /app/web/dist
ENV PY_IMAGE_DEDUP_WEB_STATIC_DIRECTORY=/app/web/dist

# the state database (web UI history) lives in /data/state, mount a volume there to keep it.
# The directory is world writable because the process runs as an arbitrary PUID/PGID.
RUN mkdir -p /data/state && chmod 0777 /data/state

ENV PUID=1000 PGID=1000
ENV PY_IMAGE_DEDUP_STATE_DATABASE_PATH=/data/state/state.db
ENV PY_IMAGE_DEDUP_WEB_HOST=0.0.0.0

# 8000: prometheus metrics, 8080: web UI + API
EXPOSE 8000 8080

ENTRYPOINT [ "docker/entrypoint.sh", "py-image-dedup" ]
CMD [ "daemon" ]
