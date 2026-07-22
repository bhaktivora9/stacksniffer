from fastapi import HTTPException, Path

import backend.services.storage_service as storage_service
from backend.services.repo_key import is_repo_key


async def resolve_repo_key(id: str = Path(...)) -> str:
    # Accept a raw repo_key too - useful for curl/debugging and for
    # seed_corpus.py, which knows keys but not request ids.
    if is_repo_key(id):
        return id
    rk = await storage_service.request_to_repo_key(id)
    if not rk:
        raise HTTPException(404, f"unknown request id: {id}")
    return rk
