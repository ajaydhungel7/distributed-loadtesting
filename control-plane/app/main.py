from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.routes.tests import router as jobs_router

app = FastAPI(title="Distributed Queue Autoscaling Platform", version="2.0.0")

app.include_router(jobs_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": exc.errors()})
