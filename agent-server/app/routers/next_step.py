# app/routers/next_step.py
# Owner: Eng 3 (Agent Pipeline)
#
# POST /next endpoint.
# Called after each step completes with a FRESH screenshot.
# Two-pass zoom pipeline: Pass 1 (locate on raw) → crop → YOLO → Pass 2 (identify on zoomed crop)

import io
import json
import os

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from PIL import Image

from app.schemas.step_plan import (
    Advance,
    AdvanceType,
    ImageSize,
    NextStepResponse,
    Step,
    TargetRect,
    TargetType,
)
from app.services.agent import AgentError, generate_locate_next, generate_identify_element
from app.services.debug import DebugSession
from app.services.mock import get_mock_next_step
from app.services.omniparser import detect_elements, draw_numbered_boxes, format_elements_context, snap_to_nearest_element
from app.services.search import get_stored_search_context
from app.routers.plan import _ADVANCE_MAP

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
    Two-pass zoom pipeline: Pass 1 (locate) → crop → YOLO → Pass 2 (identify)
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

    # --- Build completed steps summary ---
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
        dbg.save_image("screenshot", screenshot_bytes)
        dbg.save_text("completed_steps_summary", completed_summary)
        if search_context:
            dbg.save_text("search_context", search_context)

        # ===== PASS 1: Spatial localization on RAW screenshot =====
        locate_result = await generate_locate_next(
            goal=goal,
            raw_screenshot_bytes=screenshot_bytes,
            completed_steps_summary=completed_summary,
            num_completed=num_completed,
            total_steps=total_steps,
            request_id=request_id,
            search_context=search_context,
        )

        locate_debug = locate_result.pop("_debug", {})
        if locate_debug:
            dbg.save_prompt_and_response(
                "pass1_locate_next",
                prompt=locate_debug.get("prompt", ""),
                response=locate_debug.get("raw_response", ""),
                model=locate_debug.get("model", ""),
            )
        dbg.save_json("pass1_locate_output", locate_result)

        status = locate_result.get("status", "done")
        message = locate_result.get("message")
        raw_steps = locate_result.get("steps", [])

        print(f"[next] rid={request_id} Pass 1: status={status} steps={len(raw_steps)} message={message!r}")

        # ===== PASS 2: For each step, crop → YOLO → identify =====
        converted_steps: list[Step] = []
        if status == "continue" and raw_steps:
            img = Image.open(io.BytesIO(screenshot_bytes))
            actual_w, actual_h = img.size

            for step_data in raw_steps:
                step_id = step_data.get("id", f"s{len(converted_steps) + 1}")
                instruction = step_data.get("instruction", "")
                label = step_data.get("label", "")
                confidence = step_data.get("confidence", 0.5)
                advance_type = step_data.get("advance", "click_in_target")
                box_2d = step_data.get("box_2d", [])

                print(f"[next] rid={request_id} step={step_id} Pass 1 box_2d={box_2d} label={label!r}")

                if not box_2d or len(box_2d) != 4:
                    print(f"[next] rid={request_id} step={step_id} invalid box_2d, fallback")
                    converted_steps.append(Step(
                        id=step_id, instruction=instruction,
                        targets=[TargetRect(type=TargetType.bbox_norm, x=0.4, y=0.4, w=0.2, h=0.2,
                                            confidence=0.1, label=label)],
                        advance=Advance(type=_ADVANCE_MAP.get(advance_type, AdvanceType.click_in_target)),
                    ))
                    continue

                ymin, xmin, ymax, xmax = box_2d
                loc_x = xmin / 1000.0
                loc_y = ymin / 1000.0
                loc_w = (xmax - xmin) / 1000.0
                loc_h = (ymax - ymin) / 1000.0

                # Crop generous region around Pass 1 box_2d
                pad = 0.06
                crop_x = max(0.0, loc_x - pad)
                crop_y = max(0.0, loc_y - pad)
                crop_w = min(loc_w + pad * 2, 1.0 - crop_x)
                crop_h = min(loc_h + pad * 2, 1.0 - crop_y)
                min_crop = 0.10
                if crop_w < min_crop:
                    center = loc_x + loc_w / 2
                    crop_x = max(0.0, center - min_crop / 2)
                    crop_w = min(min_crop, 1.0 - crop_x)
                if crop_h < min_crop:
                    center = loc_y + loc_h / 2
                    crop_y = max(0.0, center - min_crop / 2)
                    crop_h = min(min_crop, 1.0 - crop_y)

                left = int(crop_x * actual_w)
                top = int(crop_y * actual_h)
                right = int((crop_x + crop_w) * actual_w)
                bottom = int((crop_y + crop_h) * actual_h)
                cropped = img.crop((left, top, right, bottom)).convert("RGB")
                crop_buf = io.BytesIO()
                cropped.save(crop_buf, format="PNG")
                raw_crop_bytes = crop_buf.getvalue()

                # YOLO on crop
                crop_elements = detect_elements(raw_crop_bytes)
                crop_elements.sort(key=lambda e: (e.bbox_xyxy[1], e.bbox_xyxy[0]))
                for i, e in enumerate(crop_elements):
                    e.id = i

                print(f"[next] rid={request_id} step={step_id} YOLO on crop: {len(crop_elements)} elements")

                if not crop_elements:
                    rx, ry, rw, rh = loc_x, loc_y, loc_w, loc_h
                else:
                    annotated_crop = draw_numbered_boxes(raw_crop_bytes, crop_elements)
                    crop_ctx = format_elements_context(crop_elements)

                    dbg.save_image(f"pass2_{step_id}_crop_annotated", annotated_crop)
                    dbg.save_image(f"pass2_{step_id}_crop_raw", raw_crop_bytes)

                    identify_result = await generate_identify_element(
                        instruction=instruction,
                        label=label,
                        annotated_crop_bytes=annotated_crop,
                        raw_crop_bytes=raw_crop_bytes,
                        elements_context=crop_ctx,
                        request_id=request_id,
                    )

                    id_debug = identify_result.pop("_debug", {})
                    if id_debug:
                        dbg.save_prompt_and_response(
                            f"pass2_{step_id}_identify",
                            prompt=id_debug.get("prompt", ""),
                            response=id_debug.get("raw_response", ""),
                            model=id_debug.get("model", ""),
                        )

                    picked_id = identify_result.get("element_id")
                    crop_elem_map = {e.id: e for e in crop_elements}

                    if picked_id is not None and picked_id in crop_elem_map:
                        ce = crop_elem_map[picked_id]
                        ce_x, ce_y, ce_w, ce_h = ce.bbox_xywh
                        rx = crop_x + ce_x * crop_w
                        ry = crop_y + ce_y * crop_h
                        rw = ce_w * crop_w
                        rh = ce_h * crop_h
                        confidence = identify_result.get("confidence", confidence)
                        print(f"[next] rid={request_id} step={step_id} Pass 2 picked elem[{picked_id}] "
                              f"-> full ({rx:.3f},{ry:.3f},{rw:.3f},{rh:.3f})")
                    else:
                        rel_x = (loc_x - crop_x) / crop_w
                        rel_y = (loc_y - crop_y) / crop_h
                        rel_w = loc_w / crop_w
                        rel_h = loc_h / crop_h
                        snap_x, snap_y, snap_w, snap_h, snap_id = snap_to_nearest_element(
                            rel_x, rel_y, rel_w, rel_h, crop_elements
                        )
                        rx = crop_x + snap_x * crop_w
                        ry = crop_y + snap_y * crop_h
                        rw = snap_w * crop_w
                        rh = snap_h * crop_h
                        print(f"[next] rid={request_id} step={step_id} Pass 2 fallback snap elem[{snap_id}]")

                # Clamp
                rx = max(0.0, min(rx, 1.0))
                ry = max(0.0, min(ry, 1.0))
                rw = max(0.02, min(rw, 1.0 - rx))
                rh = max(0.02, min(rh, 1.0 - ry))

                dbg.save_step_resolution(step_id=step_id, step_data=step_data,
                                         resolved_bbox=(rx, ry, rw, rh))

                converted_steps.append(Step(
                    id=step_id, instruction=instruction,
                    targets=[TargetRect(type=TargetType.bbox_norm, x=rx, y=ry, w=rw, h=rh,
                                        confidence=confidence, label=label)],
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
