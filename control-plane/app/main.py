from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.routes.tests import router as tests_router

app = FastAPI(title="Load Test Control Plane", version="1.0.0")

app.include_router(tests_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": exc.errors()})
