# app/routers/next_step.py
# Owner: Eng 3 (Agent Pipeline)
#
# POST /next endpoint.
# Called after each step completes with a FRESH screenshot.
# Pipeline: Screenshot → YOLO → annotated image → single Gemini call → NextStepResponse

import json
import os

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.schemas.step_plan import (
    Advance,
    AdvanceType,
    ImageSize,
    NextStepResponse,
    Step,
    TargetRect,
    TargetType,
)
from app.services.agent import AgentError, generate_gemini_next
from app.services.debug import DebugSession
from app.services.mock import get_mock_next_step
from app.services.omniparser import detect_elements, draw_numbered_boxes, format_elements_context, snap_to_nearest_element
from app.services.search import get_stored_search_context
from app.routers.plan import _resolve_bbox, _ADVANCE_MAP

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
    Get the next step(s) based on a fresh screenshot after completing a step.
    Pipeline: Screenshot → YOLO → Gemini one-shot → NextStepResponse
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
        return get_mock_next_step(goal, parsed_size, next_step_number=num_completed + 1)

    # --- Read screenshot ---
    screenshot_bytes = await screenshot.read()
    if len(screenshot_bytes) == 0:
        raise HTTPException(status_code=422, detail="Screenshot file is empty")

    # --- Build completed steps summary for the prompt ---
    steps_summary_lines = []
    for i, step_data in enumerate(completed_list):
        if isinstance(step_data, dict):
            instr = step_data.get("instruction", "")
            steps_summary_lines.append(f"  {i + 1}. {instr}")
        else:
            steps_summary_lines.append(f"  {i + 1}. (completed)")
    completed_summary = "\n".join(steps_summary_lines) if steps_summary_lines else "none yet"

    # --- Retrieve stored search context from /plan call ---
    search_context = get_stored_search_context(goal)
    if search_context:
        print(f"[next] rid={request_id} using {len(search_context)} chars of stored search context")

    # --- Debug session ---
    dbg = DebugSession(request_id, goal=goal, endpoint="next")

    try:
        # --- YOLO on fresh screenshot ---
        elements = detect_elements(screenshot_bytes)
        print(f"[next] rid={request_id} YOLO detected {len(elements)} elements")

        elements.sort(key=lambda e: (e.bbox_xyxy[1], e.bbox_xyxy[0]))
        for i, e in enumerate(elements):
            e.id = i

        annotated_bytes = draw_numbered_boxes(screenshot_bytes, elements)
        elements_ctx = format_elements_context(elements)

        dbg.save_image("screenshot", screenshot_bytes)
        dbg.save_image("yolo_annotated", annotated_bytes, f"{len(elements)} elements")
        dbg.save_text("yolo_elements", elements_ctx)
        dbg.save_text("completed_steps_summary", completed_summary)
        if search_context:
            dbg.save_text("search_context", search_context)

        # --- Single Gemini call ---
        result = await generate_gemini_next(
            goal=goal,
            annotated_screenshot_bytes=annotated_bytes,
            raw_screenshot_bytes=screenshot_bytes,
            elements_context=elements_ctx,
            completed_steps_summary=completed_summary,
            num_completed=num_completed,
            total_steps=total_steps,
            request_id=request_id,
            search_context=search_context,
        )

        # Save prompt + raw response
        debug_meta = result.pop("_debug", {})
        if debug_meta:
            dbg.save_prompt_and_response(
                "gemini_next",
                prompt=debug_meta.get("prompt", ""),
                response=debug_meta.get("raw_response", ""),
                model=debug_meta.get("model", ""),
            )
        dbg.save_json("gemini_parsed_output", result)

        status = result.get("status", "done")
        message = result.get("message")

        # --- Convert steps if status is "continue" (with YOLO snapping) ---
        converted_steps: list[Step] = []
        if status == "continue":
            for step_data in result.get("steps", []):
                step_id = step_data.get("id", f"s{len(converted_steps) + 1}")
                instruction = step_data.get("instruction", "")
                label = step_data.get("label")
                confidence = step_data.get("confidence", 0.5)
                advance_type = step_data.get("advance", "click_in_target")
                reasoning = step_data.get("reasoning", "")

                if reasoning:
                    print(f"[next] rid={request_id} step={step_id} REASONING: {reasoning}")
                print(f"[next] rid={request_id} step={step_id} element_id={step_data.get('element_id')} box_2d={step_data.get('box_2d')} label={label!r}")

                rx, ry, rw, rh = _resolve_bbox(step_data, elements, request_id, "next")

                dbg.save_step_resolution(
                    step_id=step_id,
                    step_data=step_data,
                    resolved_bbox=(rx, ry, rw, rh),
                )

                converted_steps.append(Step(
                    id=step_id,
                    instruction=instruction,
                    targets=[TargetRect(
                        type=TargetType.bbox_norm,
                        x=rx, y=ry, w=rw, h=rh,
                        confidence=confidence,
                        label=label,
                    )],
                    advance=Advance(type=_ADVANCE_MAP.get(advance_type, AdvanceType.click_in_target)),
                ))

        response = NextStepResponse(
            version="v1",
            goal=goal,
            status=status,
            message=message,
            image_size=parsed_size,
            steps=converted_steps,
        )

        print(f"[next] rid={request_id} status={status} steps={len(converted_steps)} message={message!r}")
        dbg.finalize(response.model_dump())
        return response

    except AgentError as e:
        print(f"[next] rid={request_id} agent error: {e}")
        raise HTTPException(status_code=502, detail=f"Agent failed: {e}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[next] rid={request_id} unexpected error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")
