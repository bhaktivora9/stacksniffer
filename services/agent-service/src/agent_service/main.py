from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="StackSniffer Agent Service")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("agent_service.main:app", host="0.0.0.0", port=8084)