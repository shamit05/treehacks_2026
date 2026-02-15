# app/routers/replan.py
# Owner: Eng 3 (Agent Pipeline)
#
# POST /replan endpoint.
# Called when the user is stuck on a step (e.g., 3+ click misses).
# Takes a fresh screenshot and produces a revised plan.

import json
import os

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.schemas.step_plan import ImageSize, StepPlan
from app.services.agent import AgentError, generate_replan
from app.services.mock import get_mock_plan
from app.services.search import get_stored_search_context

router = APIRouter()


@router.post("/replan", response_model=StepPlan)
async def create_replan(
    request: Request,
    goal: str = Form(...),
    image_size: str = Form(...),
    screenshot: UploadFile = File(...),
    current_step_id: str = Form(...),
    learning_profile: str = Form(None),
    app_context: str = Form(None),
    session_summary: str = Form(None),
):
    """
    Generate a revised step plan when the user is stuck on a step.

    - **goal**: Original task description
    - **image_size**: JSON string like {"w": 1920, "h": 1080}
    - **screenshot**: Fresh PNG screenshot of the current screen state
    - **current_step_id**: The step ID where the user got stuck (e.g., "s3")
    - **learning_profile**: Optional learning style preference
    - **app_context**: Optional JSON string with app_name, bundle_id, window_title
    - **session_summary**: Optional summary of recent session events
    """
    request_id = getattr(request.state, "request_id", "unknown")
    print(f"[replan] rid={request_id} goal={goal!r} stuck_at={current_step_id}")

    # --- Parse image_size ---
    try:
        size_dict = json.loads(image_size)
        parsed_size = ImageSize.model_validate(size_dict)
    except (json.JSONDecodeError, Exception) as e:
        raise HTTPException(status_code=422, detail=f"Invalid image_size JSON: {e}")

    # --- Mock mode ---
    mock_mode = os.getenv("MOCK_MODE", "false").lower() == "true"
    if mock_mode:
        print(f"[replan] rid={request_id} returning mock plan")
        return get_mock_plan(goal, parsed_size)

    # --- Read screenshot ---
    screenshot_bytes = await screenshot.read()
    if len(screenshot_bytes) == 0:
        raise HTTPException(status_code=422, detail="Screenshot file is empty")

    # --- Retrieve stored search context from /plan call ---
    search_context = get_stored_search_context(goal)
    if search_context:
        print(f"[replan] rid={request_id} using {len(search_context)} chars of stored search context")

    # --- Generate revised plan ---
    try:
        plan = await generate_replan(
            goal=goal,
            image_size=parsed_size,
            screenshot_bytes=screenshot_bytes,
            current_step_id=current_step_id,
            learning_profile=learning_profile,
            app_context=app_context,
            session_summary=session_summary,
            search_context=search_context or None,
            request_id=request_id,
        )
    except AgentError as e:
        print(f"[replan] rid={request_id} agent error: {e}")
        raise HTTPException(status_code=502, detail=f"Agent failed: {e}")
    except Exception as e:
        print(f"[replan] rid={request_id} unexpected error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")

    print(f"[replan] rid={request_id} success, {len(plan.steps)} steps")
    return plan
