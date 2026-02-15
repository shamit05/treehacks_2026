# app/routers/next_step.py
# Owner: Eng 3 (Agent Pipeline)
#
# POST /next endpoint.
# Called after each step completes with a FRESH screenshot.
# Uses YOLO to detect elements, then asks the LLM to pick next targets.
# Single LLM call — fast path only.

import json
import os

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.schemas.step_plan import ImageSize, NextStepResponse, Step, TargetRect, TargetType
from app.services.agent import AgentError, generate_next_step
from app.services.mock import get_mock_next_step
from app.services.omniparser import detect_elements, draw_numbered_boxes, format_elements_context

router = APIRouter()


@router.post("/next", response_model=NextStepResponse)
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
    Uses YOLO element detection + single LLM call for speed.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    print(f"[next] rid={request_id} goal={goal!r} total={total_steps}")

    # --- Parse image_size ---
    try:
        size_dict = json.loads(image_size)
        parsed_size = ImageSize.model_validate(size_dict)
    except (json.JSONDecodeError, Exception) as e:
        raise HTTPException(status_code=422, detail=f"Invalid image_size JSON: {e}")

    # --- Parse completed_steps ---
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

    # --- Run YOLO to detect elements on fresh screenshot ---
    use_yolo = os.getenv("USE_OMNIPARSER", "true").lower() == "true"
    annotated_bytes = screenshot_bytes
    elements_context = ""

    if use_yolo:
        try:
            elements = detect_elements(screenshot_bytes)
            print(f"[next] rid={request_id} YOLO detected {len(elements)} elements")
            annotated_bytes = draw_numbered_boxes(screenshot_bytes, elements)
            elements_context = format_elements_context(elements)
        except Exception as e:
            print(f"[next] rid={request_id} YOLO failed: {e}, using raw screenshot")

    # --- Single LLM call: determine status + pick next elements ---
    try:
        result = await generate_next_step(
            goal=goal,
            image_size=parsed_size,
            screenshot_bytes=annotated_bytes,
            completed_steps=completed_steps,
            total_steps=total_steps,
            learning_profile=learning_profile,
            app_context=app_context,
            request_id=request_id,
            elements_context=elements_context,
        )
    except AgentError as e:
        print(f"[next] rid={request_id} agent error: {e}")
        raise HTTPException(status_code=502, detail=f"Agent failed: {e}")
    except Exception as e:
        print(f"[next] rid={request_id} unexpected error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")

    # --- If the LLM returned element_ids, map them to YOLO bboxes ---
    if use_yolo and elements_context and result.steps:
        elem_map = {e.id: e for e in elements}
        mapped_steps = []
        for step in result.steps:
            # Check if any target has element references (via label hack)
            # The LLM returns bbox_norm targets — use them directly
            mapped_steps.append(step)
        result = NextStepResponse(
            version=result.version,
            goal=result.goal,
            status=result.status,
            message=result.message,
            image_size=result.image_size,
            steps=mapped_steps,
        )

    print(f"[next] rid={request_id} success: status={result.status} steps={len(result.steps)} message={result.message!r}")
    return result
