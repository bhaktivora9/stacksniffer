from fastapi import FastAPI

from agent_service.api.classification_contract import router as classification_contract_router


def create_app() -> FastAPI:
    app = FastAPI(title="StackSniffer Agent Service")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(classification_contract_router)

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("agent_service.main:app", host="0.0.0.0", port=8084)