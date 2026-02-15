# app/routers/next_step.py
# Owner: Eng 3 (Agent Pipeline)
#
# POST /next endpoint.
# Called after each step completes with a FRESH screenshot.
# Returns the next 1-2 steps based on the current screen state.

import json
import os

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.schemas.step_plan import ImageSize, StepPlan
from app.services.agent import AgentError, generate_next_step
from app.services.mock import get_mock_next_step
from app.services.search import get_stored_search_context

router = APIRouter()


@router.post("/next", response_model=StepPlan)
async def next_step(
    request: Request,
    goal: str = Form(...),
    image_size: str = Form(...),
    screenshot: UploadFile = File(...),
    completed_steps: str = Form(...),
    total_steps: int = Form(...),
    learning_profile: str = Form(None),
    app_context: str = Form(None),
):
    """
    Get the next 1-2 steps based on a fresh screenshot after completing a step.

    - **goal**: Original task description
    - **image_size**: JSON string like {"w": 1920, "h": 1080}
    - **screenshot**: Fresh PNG screenshot of the CURRENT screen
    - **completed_steps**: JSON array of completed steps, e.g. [{"id":"s1","instruction":"Click File"}]
    - **total_steps**: Total steps in the original plan (for progress context)
    - **learning_profile**: Optional learning style preference
    - **app_context**: Optional JSON string with app_name, bundle_id, window_title
    """
    request_id = getattr(request.state, "request_id", "unknown")
    print(f"[next] rid={request_id} goal={goal!r} total={total_steps}")

    # --- Parse image_size ---
    try:
        size_dict = json.loads(image_size)
        parsed_size = ImageSize.model_validate(size_dict)
    except (json.JSONDecodeError, Exception) as e:
        raise HTTPException(status_code=422, detail=f"Invalid image_size JSON: {e}")

    # --- Parse completed_steps to figure out step number ---
    try:
        completed_list = json.loads(completed_steps)
        if not isinstance(completed_list, list):
            raise ValueError("completed_steps must be a JSON array")
        num_completed = len(completed_list)
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"Invalid completed_steps JSON: {e}")

    print(f"[next] rid={request_id} completed={num_completed}/{total_steps}")

    # --- Mock mode ---
    mock_mode = os.getenv("MOCK_MODE", "false").lower() == "true"
    if mock_mode:
        print(f"[next] rid={request_id} returning mock next step")
        return get_mock_next_step(goal, parsed_size, next_step_number=num_completed + 1)

    # --- Read screenshot ---
    screenshot_bytes = await screenshot.read()
    if len(screenshot_bytes) == 0:
        raise HTTPException(status_code=422, detail="Screenshot file is empty")

    # --- Retrieve stored search context from /plan call ---
    search_context = get_stored_search_context(goal)
    if search_context:
        print(f"[next] rid={request_id} using {len(search_context)} chars of stored search context")

    # --- Generate next step(s) via AI ---
    try:
        plan = await generate_next_step(
            goal=goal,
            image_size=parsed_size,
            screenshot_bytes=screenshot_bytes,
            completed_steps=completed_steps,
            total_steps=total_steps,
            learning_profile=learning_profile,
            app_context=app_context,
            search_context=search_context or None,
            request_id=request_id,
        )
    except AgentError as e:
        print(f"[next] rid={request_id} agent error: {e}")
        raise HTTPException(status_code=502, detail=f"Agent failed: {e}")
    except Exception as e:
        print(f"[next] rid={request_id} unexpected error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")

    print(f"[next] rid={request_id} success, {len(plan.steps)} steps returned")
    return plan
