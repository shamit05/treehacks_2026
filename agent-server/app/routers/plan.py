# app/routers/plan.py
# Owner: Eng 3 (Agent Pipeline)
#
# POST /plan endpoint.
# Receives a goal + screenshot, returns a StepPlan JSON.
# Supports MOCK_MODE for demo reliability.

import asyncio
import json
import os

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.schemas.step_plan import ImageSize, StepPlan
from app.services.agent import AgentError, generate_plan
from app.services.mock import get_mock_plan
from app.services.search import search_for_goal

router = APIRouter()

MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024  # 20 MB


@router.post("/plan", response_model=StepPlan)
async def create_plan(
    request: Request,
    goal: str = Form(...),
    image_size: str = Form(...),
    screenshot: UploadFile = File(...),
    learning_profile: str = Form(None),
    app_context: str = Form(None),
    session_summary: str = Form(None),
    skip_search: bool = Form(False),
):
    """
    Generate a step-by-step guidance plan from a goal and screenshot.

    - **goal**: Natural language task description
    - **image_size**: JSON string like {"w": 1920, "h": 1080}
    - **screenshot**: PNG image of the current screen
    - **learning_profile**: Optional learning style preference
    - **app_context**: Optional JSON string with app_name, bundle_id, window_title
    - **session_summary**: Optional short summary of recent session events
    - **skip_search**: Optional flag to disable web search (default: false)
    """
    request_id = getattr(request.state, "request_id", "unknown")
    print(f"[plan] rid={request_id} goal={goal!r}")

    # --- Parse and validate image_size ---
    try:
        size_dict = json.loads(image_size)
        parsed_size = ImageSize.model_validate(size_dict)
    except (json.JSONDecodeError, Exception) as e:
        raise HTTPException(status_code=422, detail=f"Invalid image_size JSON: {e}")

    # --- Mock mode ---
    mock_mode = os.getenv("MOCK_MODE", "false").lower() == "true"
    if mock_mode:
        print(f"[plan] rid={request_id} returning mock plan")
        return get_mock_plan(goal, parsed_size)

    # --- Read and validate screenshot ---
    screenshot_bytes = await screenshot.read()
    if len(screenshot_bytes) == 0:
        raise HTTPException(status_code=422, detail="Screenshot file is empty")
    if len(screenshot_bytes) > MAX_SCREENSHOT_BYTES:
        raise HTTPException(status_code=413, detail="Screenshot exceeds 20 MB limit")

    print(f"[plan] rid={request_id} screenshot={len(screenshot_bytes)} bytes, size={parsed_size.w}x{parsed_size.h}")

    # --- Run search and plan generation CONCURRENTLY ---
    # Search results are stored internally for /next and /replan calls.
    # Plan generation proceeds without blocking on search, saving ~12-15s.
    # Pass skip_search=true to disable web search entirely.

    if skip_search:
        print(f"[plan] rid={request_id} search skipped (skip_search=true)")

    async def _safe_search() -> str:
        """Wrapper so search failures don't cancel the gather."""
        if skip_search:
            return ""
        try:
            return await search_for_goal(
                goal=goal,
                screenshot_bytes=screenshot_bytes,
                app_context=app_context,
                request_id=request_id,
            )
        except Exception as e:
            print(f"[plan] rid={request_id} search failed (non-fatal): {type(e).__name__}: {e}")
            return ""

    async def _generate() -> StepPlan:
        return await generate_plan(
            goal=goal,
            image_size=parsed_size,
            screenshot_bytes=screenshot_bytes,
            learning_profile=learning_profile,
            app_context=app_context,
            session_summary=session_summary,
            search_context="",  # plan starts without search; /next will use stored results
            request_id=request_id,
        )

    try:
        search_context, plan = await asyncio.gather(_safe_search(), _generate())
    except AgentError as e:
        print(f"[plan] rid={request_id} agent error: {e}")
        raise HTTPException(status_code=502, detail=f"Agent failed: {e}")
    except Exception as e:
        print(f"[plan] rid={request_id} unexpected error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")

    print(f"[plan] rid={request_id} success, {len(plan.steps)} steps")
    return plan
