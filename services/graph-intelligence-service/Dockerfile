FROM python:3.13-slim AS build

WORKDIR /workspace

COPY pyproject.toml ./
COPY src ./src
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.13-slim

WORKDIR /app

COPY --from=build /wheels /tmp/wheels
RUN python -m pip install --no-cache-dir /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels

RUN useradd --create-home --shell /usr/sbin/nologin appuser
USER appuser

EXPOSE 8085

ENTRYPOINT ["uvicorn", "graph_intelligence_service.main:app", "--host", "0.0.0.0", "--port", "8085"]