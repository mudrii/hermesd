FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS base

LABEL maintainer="Nous Research"
LABEL description="TUI monitoring dashboard for Hermes AI agent"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 hermesd \
    && useradd --uid 1000 --gid hermesd --create-home hermesd

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN pip install --no-cache-dir pip==26.2.1 uv==0.12.10 \
    && uv sync --locked --no-dev --no-install-project

COPY hermesd/ hermesd/
RUN uv sync --locked --no-dev \
    && chmod -R a+rX hermesd \
    && rm -rf /root/.cache /root/.cache/uv

USER hermesd

# Mount ~/.hermes as a volume at runtime:
#   docker run -it -v ~/.hermes:/home/hermesd/.hermes:ro hermesd
ENV HERMES_HOME=/home/hermesd/.hermes
ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["hermesd"]
