import logging
from os import getenv
from typing import Final

DEFAULT_ALLOWED_ORIGINS = ["http://localhost:5173", "http://localhost:3000"]
_ENV_MISSING: Final = object()


def allowed_origins(configured: str | None | object = _ENV_MISSING) -> list[str]:
    value = getenv("ALLOWED_ORIGINS") if configured is _ENV_MISSING else configured
    if value is None:
        return DEFAULT_ALLOWED_ORIGINS

    origins = [origin.strip().rstrip("/") for origin in str(value).split(",") if origin.strip()]
    explicit_origins = [origin for origin in origins if origin != "*"]
    if len(explicit_origins) != len(origins):
        logging.getLogger(__name__).warning(
            "Ignoring wildcard CORS origin because credentialed requests require explicit origins"
        )
    if not explicit_origins:
        raise ValueError(
            "ALLOWED_ORIGINS must include at least one explicit origin when CORS credentials are enabled"
        )
    return explicit_origins
