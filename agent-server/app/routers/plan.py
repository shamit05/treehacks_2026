# app/routers/plan.py
# Owner: Eng 3 (Agent Server)
#
# POST /plan endpoint.
# Receives a goal + screenshot, returns a StepPlan JSON.
# Supports MOCK_MODE for demo reliability.

import os
import uuid

from fastapi import APIRouter, File, Form, Header, UploadFile

from app.schemas.step_plan import StepPlan
from app.services.agent import generate_plan
from app.services.mock import get_mock_plan

router = APIRouter()


@router.post("/plan", response_model=StepPlan)
async def create_plan(
    goal: str = Form(...),
    image_size: str = Form(...),
    screenshot: UploadFile = File(...),
    learning_profile: str = Form(None),
    app_context: str = Form(None),
    session_summary: str = Form(None),
    x_request_id: str | None = Header(None),
):
    """
    Generate a step-by-step guidance plan from a goal and screenshot.

    - **goal**: Natural language task description
    - **image_size**: JSON string like {"w": 1920, "h": 1080}
    - **screenshot**: PNG image of the current screen
    - **learning_profile**: Optional learning style preference
    - **app_context**: Optional JSON string with app_name, bundle_id, window_title
    - **session_summary**: Optional short summary of recent session events
    """
    request_id = x_request_id or str(uuid.uuid4())
    print(f"[plan] request_id={request_id} goal={goal!r}")

    # Check mock mode
    mock_mode = os.getenv("MOCK_MODE", "false").lower() == "true"
    if mock_mode:
        print(f"[plan] request_id={request_id} returning mock plan")
        return get_mock_plan(goal)

    # Read screenshot bytes
    screenshot_bytes = await screenshot.read()

    # Generate plan via AI model
    plan = await generate_plan(
        goal=goal,
        image_size_json=image_size,
        screenshot_bytes=screenshot_bytes,
        learning_profile=learning_profile,
        app_context=app_context,
        session_summary=session_summary,
        request_id=request_id,
    )

    return plan
