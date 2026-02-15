# app/routers/refine.py
# Owner: Eng 3 (Agent Pipeline)
#
# POST /refine endpoint.
# Receives a crop image + context, returns a tight bounding box
# in crop-normalized coordinates for the target UI element.

import json
import os

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.schemas.step_plan import CropRect, RefineResponse, TargetRect, TargetType
from app.services.agent import AgentError, generate_refine

router = APIRouter()

MAX_CROP_BYTES = 10 * 1024 * 1024  # 10 MB


def _stitch_back(crop_bbox: RefineResponse, crop_rect: CropRect) -> TargetRect:
    """Convert crop-normalized bbox to full-image normalized bbox."""
    x = crop_rect.cx + crop_bbox.x * crop_rect.cw
    y = crop_rect.cy + crop_bbox.y * crop_rect.ch
    w = crop_bbox.w * crop_rect.cw
    h = crop_bbox.h * crop_rect.ch

    # Clamp to [0, 1]
    x = max(0.0, min(x, 1.0))
    y = max(0.0, min(y, 1.0))
    w = max(0.001, min(w, 1.0 - x))
    h = max(0.001, min(h, 1.0 - y))

    return TargetRect(
        type=TargetType.bbox_norm,
        x=x,
        y=y,
        w=w,
        h=h,
        confidence=crop_bbox.confidence,
        label=crop_bbox.label,
    )


@router.post("/refine", response_model=TargetRect)
async def refine_target(
    request: Request,
    instruction: str = Form(...),
    target_label: str = Form(""),
    crop_rect: str = Form(...),
    crop_image: UploadFile = File(...),
):
    """
    Refine a target bounding box by analyzing a cropped screenshot region.

    - **instruction**: What the user should do (e.g., "Click the + button")
    - **target_label**: Label of the target UI element
    - **crop_rect**: JSON string {"cx", "cy", "cw", "ch"} — crop in full-image normalized coords
    - **crop_image**: PNG image of the cropped region

    Returns a TargetRect in full-image normalized coordinates (stitched back).
    """
    request_id = getattr(request.state, "request_id", "unknown")
    print(f"[refine] rid={request_id} instruction={instruction!r}")

    # --- Parse crop_rect ---
    try:
        crop_dict = json.loads(crop_rect)
        parsed_crop = CropRect.model_validate(crop_dict)
    except (json.JSONDecodeError, Exception) as e:
        raise HTTPException(status_code=422, detail=f"Invalid crop_rect JSON: {e}")

    # --- Mock mode ---
    mock_mode = os.getenv("MOCK_MODE", "false").lower() == "true"
    if mock_mode:
        # Return center of crop as the target
        return TargetRect(
            type=TargetType.bbox_norm,
            x=parsed_crop.cx + 0.3 * parsed_crop.cw,
            y=parsed_crop.cy + 0.3 * parsed_crop.ch,
            w=0.4 * parsed_crop.cw,
            h=0.4 * parsed_crop.ch,
            confidence=0.85,
            label=target_label or "mock target",
        )

    # --- Read crop image ---
    crop_bytes = await crop_image.read()
    if len(crop_bytes) == 0:
        raise HTTPException(status_code=422, detail="Crop image is empty")
    if len(crop_bytes) > MAX_CROP_BYTES:
        raise HTTPException(status_code=413, detail="Crop image exceeds 10 MB limit")

    print(f"[refine] rid={request_id} crop_image={len(crop_bytes)} bytes, crop_rect=({parsed_crop.cx:.3f},{parsed_crop.cy:.3f},{parsed_crop.cw:.3f},{parsed_crop.ch:.3f})")

    # --- Generate refined bbox via AI ---
    try:
        crop_bbox = await generate_refine(
            instruction=instruction,
            target_label=target_label,
            crop_image_bytes=crop_bytes,
            request_id=request_id,
        )
    except AgentError as e:
        print(f"[refine] rid={request_id} agent error: {e}")
        raise HTTPException(status_code=502, detail=f"Refine agent failed: {e}")
    except Exception as e:
        print(f"[refine] rid={request_id} unexpected error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")

    # --- Stitch back to full-image coords ---
    stitched = _stitch_back(crop_bbox, parsed_crop)
    print(f"[refine] rid={request_id} crop_bbox=({crop_bbox.x:.3f},{crop_bbox.y:.3f},{crop_bbox.w:.3f},{crop_bbox.h:.3f}) -> stitched=({stitched.x:.3f},{stitched.y:.3f},{stitched.w:.3f},{stitched.h:.3f})")
    return stitched
