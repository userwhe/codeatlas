FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src
RUN uv sync --frozen --no-dev

# Fixture repositories and pull requests for fake mode (the load test); fake mode stays refused in
# production. The rest of tests/ is not copied.
COPY tests/fixtures/repos ./tests/fixtures/repos
COPY tests/fixtures/pull-requests ./tests/fixtures/pull-requests

# A fixed UID: render-config gives the containers' secret files to UID 1000.
RUN useradd --uid 1000 --create-home app

# The commit SHA, reported by /version. Set after the dependency layers to keep them cached.
ARG CODEATLAS_RELEASE=development
ENV CODEATLAS_RELEASE=$CODEATLAS_RELEASE

USER app

EXPOSE 8000
CMD ["uvicorn", "codeatlas.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
