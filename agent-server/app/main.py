# app/main.py
# Owner: Eng 3 (Agent Server)
#
# FastAPI application entry point.
# Registers routers and configures CORS for the mac-client.

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import plan

app = FastAPI(
    title="OverlayGuide Agent Server",
    description="AI agent that generates step-by-step UI guidance plans from screenshots",
    version="0.1.0",
)

# CORS — allow the mac client to connect from localhost
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(plan.router)


@app.get("/health")
async def health():
    """Health check endpoint. Returns mock status if MOCK_MODE is enabled."""
    mock_mode = os.getenv("MOCK_MODE", "false").lower() == "true"
    return {
        "status": "ok",
        "mock_mode": mock_mode,
    }
