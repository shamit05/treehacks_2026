# app/main.py
# Owner: Eng 3 (Agent Pipeline)
#
# FastAPI application entry point.
# Registers routers, loads env, configures CORS, and sets up error handling.

import os
import time
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

load_dotenv()  # load .env before anything else

from app.routers import next_step, plan, refine, replan  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown events."""
    mock_mode = os.getenv("MOCK_MODE", "false").lower() == "true"
    model = os.getenv("OPENAI_MODEL", "gpt-4o")
    gemini_key = bool(os.getenv("GEMINI_API_KEY"))
    openai_key = bool(os.getenv("OPENAI_API_KEY"))
    openrouter_key = bool(os.getenv("OPENROUTER_API_KEY"))
    provider = "Gemini" if gemini_key else ("OpenAI" if openai_key else ("OpenRouter" if openrouter_key else "NONE"))
    has_key = gemini_key or openai_key or openrouter_key
    print(f"[server] The Cookbook Agent Server starting")
    print(f"[server]   MOCK_MODE={mock_mode}")
    print(f"[server]   PROVIDER={provider}")
    print(f"[server]   MODEL={model}")
    if not mock_mode and not has_key:
        print("[server]   WARNING: No API key set and mock mode is off. /plan will fail.")
    yield
    print("[server] Shutting down.")


app = FastAPI(
    title="The Cookbook Agent Server",
    description="AI agent that generates step-by-step UI guidance plans from screenshots",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow the mac client to connect from localhost
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request ID + timing middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = request_id
    start = time.time()

    response = await call_next(request)

    elapsed_ms = round((time.time() - start) * 1000)
    response.headers["X-Request-ID"] = request_id
    print(f"[server] {request.method} {request.url.path} -> {response.status_code} ({elapsed_ms}ms) rid={request_id}")
    return response


# ---------------------------------------------------------------------------
# Global exception handler — return JSON, never HTML stacktraces
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    print(f"[server] Unhandled error rid={request_id}: {type(exc).__name__}: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "message": str(exc),
            "request_id": request_id,
        },
    )


# Register routers
app.include_router(plan.router)
app.include_router(refine.router)
app.include_router(next_step.router)
app.include_router(replan.router)
app.include_router(refine.router)


@app.get("/health")
async def health():
    """Health check endpoint. Returns mock status if MOCK_MODE is enabled."""
    mock_mode = os.getenv("MOCK_MODE", "false").lower() == "true"
    return {
        "status": "ok",
        "mock_mode": mock_mode,
        "model": os.getenv("OPENAI_MODEL", "gpt-4o"),
    }
