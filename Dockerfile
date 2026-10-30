FROM python:3.12-slim-bookworm

ARG PYPI_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_INDEX_URL=${PYPI_INDEX_URL}

WORKDIR /app

RUN groupadd --gid 10001 love-moments \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin love-moments

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install --requirement requirements.txt

# Release identity changes on every deploy. Keep it after dependency layers so
# a new commit does not invalidate the slow package-download cache.
ARG APP_RELEASE_SHA=unknown
ENV APP_RELEASE_SHA=${APP_RELEASE_SHA}

COPY main.py ./
COPY sw.js manifest.webmanifest ./
COPY love_moments ./love_moments
COPY scripts ./scripts
COPY static ./static

USER 10001:10001

EXPOSE 52013

CMD ["python", "main.py"]
