import json
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from backend.models.schemas import ChatRequest
from backend.services import chat_service, storage_service

router = APIRouter(prefix="/api/chat", tags=["chat"])

chat_sessions: dict[str, dict] = {}


@router.post("/")
async def chat(request: ChatRequest):
    analysis = await storage_service.get_analysis(request.analysis_id)
    if not analysis:
        raise HTTPException(404, "Analysis not found. Analyze a repo first.")

    session_id = request.session_id or str(uuid4())
    if session_id not in chat_sessions:
        chat_sessions[session_id] = {
            "session_id": session_id,
            "analysis_id": request.analysis_id,
            "messages": [],
            "created_at": datetime.utcnow().isoformat(),
        }

    session = chat_sessions[session_id]
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in session["messages"]
    ]

    full_response: list[str] = []

    async def generate():
        async for chunk in chat_service.stream_chat(analysis, history, request.message):
            full_response.append(chunk)
            yield f"data: {json.dumps({'chunk': chunk, 'session_id': session_id})}\n\n"

        session["messages"].append({
            "role": "user",
            "content": request.message,
            "timestamp": datetime.utcnow().isoformat(),
        })
        session["messages"].append({
            "role": "assistant",
            "content": "".join(full_response),
            "timestamp": datetime.utcnow().isoformat(),
        })
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/session/{session_id}")
async def get_session(session_id: str):
    session = chat_sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    return session


@router.delete("/session/{session_id}")
async def clear_session(session_id: str):
    chat_sessions.pop(session_id, None)
    return {"cleared": True}
