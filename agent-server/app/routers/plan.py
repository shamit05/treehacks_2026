# app/routers/plan.py
# Owner: Eng 3 (Agent Pipeline)
#
# POST /plan endpoint.
# Receives a goal + screenshot, returns a StepPlan JSON.
# Supports MOCK_MODE for demo reliability.
# SoM pipeline: server generates markers and draws them on the screenshot
# at actual pixel resolution (avoids Retina/AppKit coordinate issues).

import io
import json
import os

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from PIL import Image, ImageDraw, ImageFont

from app.schemas.step_plan import (
    CropRect,
    ImageSize,
    SoMMarker,
    SoMStepPlan,
    Step,
    StepPlan,
    TargetRect,
    TargetType,
)
from app.services.agent import AgentError, generate_plan, generate_som_plan, generate_som_refine
from app.services.mock import get_mock_plan

router = APIRouter()

MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024  # 20 MB

# SoM grid configuration
SOM_COLUMNS = 16
SOM_ROWS = 10

# Default half-size of the bbox drawn around a marker center (normalized).
_DEFAULT_MARKER_BBOX_HALF = 0.04


def _generate_markers_and_image(
    screenshot_bytes: bytes,
) -> tuple[list[SoMMarker], bytes]:
    """
    Generate a grid of numbered markers and draw them directly on the
    screenshot at its ACTUAL pixel resolution using Pillow.
    Marker size scales with image resolution so they're always readable.
    Returns (markers_list, marked_png_bytes).
    """
    img = Image.open(io.BytesIO(screenshot_bytes)).convert("RGBA")
    actual_w, actual_h = img.size

    # Scale marker size to image resolution.
    # Target: markers should be ~1/80th of image width.
    # On 3024px wide image: radius=38, font=24 — clearly readable.
    # On 1512px wide image: radius=19, font=12 — still readable.
    marker_radius = max(16, actual_w // 80)
    font_size = max(12, actual_w // 120)
    border_width = max(2, actual_w // 1000)

    print(f"[plan] image={actual_w}x{actual_h}, marker_radius={marker_radius}, font_size={font_size}")

    # Generate marker positions (normalized)
    markers: list[SoMMarker] = []
    norm_radius = marker_radius / max(actual_w, actual_h)

    for row in range(SOM_ROWS):
        for col in range(SOM_COLUMNS):
            cx = (col + 0.5) / SOM_COLUMNS
            cy = (row + 0.5) / SOM_ROWS
            markers.append(SoMMarker(
                id=len(markers),
                cx=round(cx, 6),
                cy=round(cy, 6),
                radius=round(norm_radius, 6),
            ))

    # Create a transparent overlay for the markers
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
    except Exception:
        try:
            font = ImageFont.truetype("/System/Library/Fonts/SFNSMono.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()

    for m in markers:
        px = m.cx * actual_w
        py = m.cy * actual_h
        r = marker_radius

        # White filled circle with red border
        draw.ellipse(
            [px - r, py - r, px + r, py + r],
            fill=(255, 255, 255, 220),
            outline=(220, 40, 40, 255),
            width=border_width,
        )

        # Marker ID text, centered in circle
        text = str(m.id)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text(
            (px - tw / 2, py - th / 2),
            text,
            fill=(0, 0, 0, 255),
            font=font,
        )

    # Composite overlay onto the screenshot
    result = Image.alpha_composite(img, overlay).convert("RGB")

    # Encode as PNG
    buf = io.BytesIO()
    result.save(buf, format="PNG")
    marked_bytes = buf.getvalue()

    print(f"[plan] drew {len(markers)} markers on {actual_w}x{actual_h} image -> {len(marked_bytes)} bytes")
    return markers, marked_bytes


def _som_plan_to_step_plan(
    som_plan: SoMStepPlan,
    markers: list[SoMMarker],
) -> StepPlan:
    """
    Convert a SoM plan (multiple marker IDs per step) to a standard StepPlan.
    All selected markers for a step are merged into a single bounding TargetRect
    that spans from the min to max marker positions.
    """
    marker_map = {m.id: m for m in markers}
    converted_steps: list[Step] = []

    for som_step in som_plan.steps:
        # Collect all valid marker positions for this step
        found_markers = []
        label = None
        avg_conf = 0.0
        for st in som_step.som_targets:
            marker = marker_map.get(st.marker_id)
            if marker is None:
                print(f"[plan] WARNING: marker_id={st.marker_id} not found in markers list, skipping")
                continue
            found_markers.append(marker)
            if st.label and not label:
                label = st.label
            if st.confidence is not None:
                avg_conf += st.confidence

        if not found_markers:
            converted_steps.append(Step(
                id=som_step.id,
                instruction=som_step.instruction,
                targets=[TargetRect(
                    type=TargetType.bbox_norm,
                    x=0.4, y=0.4, w=0.2, h=0.2,
                    confidence=0.1,
                    label="fallback — marker not found",
                )],
                advance=som_step.advance,
                safety=som_step.safety,
            ))
            continue

        avg_conf = avg_conf / len(found_markers) if found_markers else 0.0

        # Compute bounding box spanning all selected markers
        min_cx = min(m.cx for m in found_markers)
        max_cx = max(m.cx for m in found_markers)
        min_cy = min(m.cy for m in found_markers)
        max_cy = max(m.cy for m in found_markers)

        pad = _DEFAULT_MARKER_BBOX_HALF
        x = max(0.0, min_cx - pad)
        y = max(0.0, min_cy - pad)
        w = min(max_cx - min_cx + pad * 2, 1.0 - x)
        h = min(max_cy - min_cy + pad * 2, 1.0 - y)

        # Ensure minimum size
        w = max(w, 0.02)
        h = max(h, 0.02)

        targets = [TargetRect(
            type=TargetType.bbox_norm,
            x=x, y=y, w=w, h=h,
            confidence=avg_conf if avg_conf > 0 else None,
            label=label,
        )]

        converted_steps.append(Step(
            id=som_step.id,
            instruction=som_step.instruction,
            targets=targets,
            advance=som_step.advance,
            safety=som_step.safety,
        ))

    return StepPlan(
        version=som_plan.version,
        goal=som_plan.goal,
        image_size=som_plan.image_size,
        steps=converted_steps,
    )


# ---------------------------------------------------------------------------
# Two-pass SoM refinement: dense sub-grid on a zoomed crop
# ---------------------------------------------------------------------------

REFINE_SUB_COLS = 12     # sub-grid columns
REFINE_SUB_ROWS = 10     # sub-grid rows
REFINE_PADDING = 0.05    # padding around the coarse target region for the crop
# Padding added around the sub-marker-defined bbox (normalized to full image)
_REFINED_BBOX_PAD = 0.015


def _crop_and_draw_sub_markers(
    original_bytes: bytes,
    target_rect: TargetRect,
) -> tuple[CropRect, bytes, list[dict]]:
    """
    Crop a region around the coarse target (which may span multiple markers)
    from the original screenshot, then draw a dense 12x12 numbered sub-grid
    on the crop. All drawing at actual pixel resolution.
    Returns (crop_rect, marked_crop_png, sub_markers).
    """
    img = Image.open(io.BytesIO(original_bytes))
    actual_w, actual_h = img.size

    # Compute crop rect from the coarse target + padding, clamped to [0,1]
    pad = REFINE_PADDING
    cx = max(0.0, target_rect.x - pad)
    cy = max(0.0, target_rect.y - pad)
    cw = min(target_rect.w + pad * 2, 1.0 - cx)
    ch = min(target_rect.h + pad * 2, 1.0 - cy)

    # Ensure minimum crop size so the zoomed image has enough detail
    min_crop = 0.10
    if cw < min_crop:
        center = cx + cw / 2
        cx = max(0.0, center - min_crop / 2)
        cw = min(min_crop, 1.0 - cx)
    if ch < min_crop:
        center = cy + ch / 2
        cy = max(0.0, center - min_crop / 2)
        ch = min(min_crop, 1.0 - cy)

    crop_rect = CropRect(cx=cx, cy=cy, cw=cw, ch=ch)

    # Crop at actual pixel resolution
    left = int(cx * actual_w)
    top = int(cy * actual_h)
    right = int((cx + cw) * actual_w)
    bottom = int((cy + ch) * actual_h)
    cropped = img.crop((left, top, right, bottom))
    crop_w, crop_h = cropped.size

    # Sub-markers must be SMALL so they don't obscure the UI underneath.
    # Target: ~25% of the smallest cell dimension.
    cell_w = crop_w / REFINE_SUB_COLS
    cell_h = crop_h / REFINE_SUB_ROWS
    min_cell = min(cell_w, cell_h)
    sub_radius = max(8, int(min_cell * 0.25))
    sub_font_size = max(9, int(sub_radius * 0.9))  # min 9pt to avoid font errors
    border_w = max(1, sub_radius // 6)

    # Generate sub-markers on a dense 12x12 grid within the crop
    sub_markers = []
    for row in range(REFINE_SUB_ROWS):
        for col in range(REFINE_SUB_COLS):
            sub_cx = (col + 0.5) / REFINE_SUB_COLS
            sub_cy = (row + 0.5) / REFINE_SUB_ROWS
            sub_markers.append({
                "id": len(sub_markers),
                "cx_crop": sub_cx,
                "cy_crop": sub_cy,
                # Full-image normalized position
                "cx_full": cx + sub_cx * cw,
                "cy_full": cy + sub_cy * ch,
            })

    # Draw sub-markers on the crop
    cropped_rgba = cropped.convert("RGBA")
    overlay = Image.new("RGBA", cropped_rgba.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", sub_font_size)
    except Exception:
        font = ImageFont.load_default()

    for m in sub_markers:
        px = m["cx_crop"] * crop_w
        py = m["cy_crop"] * crop_h
        r = sub_radius
        # More opaque fill — readable markers, UI still slightly visible underneath
        draw.ellipse([px - r, py - r, px + r, py + r],
                     fill=(255, 255, 255, 180),
                     outline=(30, 120, 255, 230),
                     width=border_w)
        # Number text stays opaque for readability
        text = str(m["id"])
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text((px - tw / 2, py - th / 2), text, fill=(0, 0, 0, 230), font=font)

    result = Image.alpha_composite(cropped_rgba, overlay).convert("RGB")
    buf = io.BytesIO()
    result.save(buf, format="PNG")

    return crop_rect, buf.getvalue(), sub_markers


async def _refine_plan_two_pass(
    plan: StepPlan,
    original_screenshot_bytes: bytes,
    request_id: str,
) -> StepPlan:
    """
    Two-pass SoM refinement: for each step's coarse target, zoom into that
    region, draw a dense 12x12 sub-grid, ask the model to select all
    sub-markers covering the element. Bbox is computed from the bounding
    rectangle of all selected sub-markers. No raw coordinate guessing.
    """
    refined_steps: list[Step] = []

    for step in plan.steps:
        refined_targets: list[TargetRect] = []

        for target in step.targets:
            try:
                crop_rect, marked_crop, sub_markers = _crop_and_draw_sub_markers(
                    original_screenshot_bytes, target
                )

                print(f"[refine2] rid={request_id} step={step.id} crop=({crop_rect.cx:.3f},{crop_rect.cy:.3f},{crop_rect.cw:.3f},{crop_rect.ch:.3f}) {len(sub_markers)} sub-markers, {len(marked_crop)} bytes")

                # Save crop for debugging
                try:
                    with open("/tmp/overlayguide_refine_subcrop.png", "wb") as f:
                        f.write(marked_crop)
                except Exception:
                    pass

                # Ask model to select sub-markers covering the element
                result = await generate_som_refine(
                    instruction=step.instruction,
                    target_label=target.label or "",
                    crop_image_bytes=marked_crop,
                    request_id=request_id,
                )

                picked_ids = result.get("marker_ids", [])
                sub_map = {m["id"]: m for m in sub_markers}

                # Find all valid sub-markers
                found = [sub_map[mid] for mid in picked_ids if mid in sub_map]
                if not found:
                    print(f"[refine2] rid={request_id} step={step.id} no valid sub-markers from {picked_ids}, keeping coarse bbox")
                    refined_targets.append(target)
                    continue

                # Compute bounding box of all selected sub-markers (full-image coords)
                min_x = min(m["cx_full"] for m in found)
                max_x = max(m["cx_full"] for m in found)
                min_y = min(m["cy_full"] for m in found)
                max_y = max(m["cy_full"] for m in found)

                pad = _REFINED_BBOX_PAD
                rx = max(0.0, min_x - pad)
                ry = max(0.0, min_y - pad)
                rw = min(max_x - min_x + pad * 2, 1.0 - rx)
                rh = min(max_y - min_y + pad * 2, 1.0 - ry)

                # Ensure minimum size — at least ~40x30 pixels on a 1512x982 screen
                rw = max(rw, 0.025)
                rh = max(rh, 0.025)

                refined = TargetRect(
                    type=TargetType.bbox_norm,
                    x=rx, y=ry, w=rw, h=rh,
                    confidence=result.get("confidence"),
                    label=result.get("label"),
                )
                ids_str = ",".join(str(i) for i in picked_ids)
                print(f"[refine2] rid={request_id} step={step.id} sub-markers=[{ids_str}] ({len(found)} valid) -> bbox ({rx:.3f},{ry:.3f},{rw:.3f},{rh:.3f})")
                refined_targets.append(refined)

            except Exception as e:
                print(f"[refine2] rid={request_id} step={step.id} two-pass refine failed: {e}, keeping coarse bbox")
                refined_targets.append(target)

        refined_steps.append(Step(
            id=step.id,
            instruction=step.instruction,
            targets=refined_targets,
            advance=step.advance,
            safety=step.safety,
        ))

    return StepPlan(
        version=plan.version,
        goal=plan.goal,
        app_context=plan.app_context,
        image_size=plan.image_size,
        steps=refined_steps,
    )


@router.post("/plan", response_model=StepPlan)
async def create_plan(
    request: Request,
    goal: str = Form(...),
    image_size: str = Form(...),
    screenshot: UploadFile = File(...),
    learning_profile: str = Form(None),
    app_context: str = Form(None),
    session_summary: str = Form(None),
    markers_json: str = Form(None),
):
    """
    Generate a step-by-step guidance plan from a goal and screenshot.

    SoM pipeline (default when no markers_json):
      Server generates markers and draws them on the screenshot at actual
      pixel resolution, then asks the model to pick marker IDs.

    Legacy pipeline (when markers_json is explicitly set to "none"):
      Model outputs raw coordinates directly.
    """
    request_id = getattr(request.state, "request_id", "unknown")

    # Use SoM by default; only skip if markers_json is explicitly "none"
    use_som = markers_json != "none"
    print(f"[plan] rid={request_id} goal={goal!r} som={use_som}")

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

    # --- Generate plan via AI ---
    try:
        if use_som:
            # Server-side SoM: generate markers and draw them at actual pixel resolution
            markers, marked_screenshot = _generate_markers_and_image(screenshot_bytes)

            # Save marked screenshot for debugging
            try:
                with open("/tmp/overlayguide_server_marked.png", "wb") as f:
                    f.write(marked_screenshot)
            except Exception:
                pass

            som_plan = await generate_som_plan(
                goal=goal,
                image_size=parsed_size,
                screenshot_bytes=marked_screenshot,
                learning_profile=learning_profile,
                app_context=app_context,
                session_summary=session_summary,
                request_id=request_id,
            )

            # Log what the model picked
            marker_map = {m.id: m for m in markers}
            for step in som_plan.steps:
                for st in step.som_targets:
                    m = marker_map.get(st.marker_id)
                    if m:
                        print(f"[plan] rid={request_id} MARKER PICK: step={step.id} marker_id={st.marker_id} -> ({m.cx:.3f},{m.cy:.3f}) conf={st.confidence} label={st.label!r}")
                    else:
                        print(f"[plan] rid={request_id} MARKER PICK: step={step.id} marker_id={st.marker_id} -> NOT FOUND!")

            plan = _som_plan_to_step_plan(som_plan, markers)

            for step in plan.steps:
                for t in step.targets:
                    print(f"[plan] rid={request_id} MARKER TARGET: step={step.id} ({t.x:.3f},{t.y:.3f},{t.w:.3f},{t.h:.3f}) label={t.label!r}")

            # Two-pass SoM: dense sub-grid on zoomed crop, model picks sub-marker
            plan = await _refine_plan_two_pass(plan, screenshot_bytes, request_id)

            for step in plan.steps:
                for t in step.targets:
                    print(f"[plan] rid={request_id} REFINED TARGET: step={step.id} ({t.x:.3f},{t.y:.3f},{t.w:.3f},{t.h:.3f}) label={t.label!r}")
        else:
            # Legacy pipeline: model outputs raw coordinates
            plan = await generate_plan(
                goal=goal,
                image_size=parsed_size,
                screenshot_bytes=screenshot_bytes,
                learning_profile=learning_profile,
                app_context=app_context,
                session_summary=session_summary,
                request_id=request_id,
            )
    except AgentError as e:
        print(f"[plan] rid={request_id} agent error: {e}")
        raise HTTPException(status_code=502, detail=f"Agent failed: {e}")
    except Exception as e:
        print(f"[plan] rid={request_id} unexpected error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")

    print(f"[plan] rid={request_id} success, {len(plan.steps)} steps (som={use_som})")
    return plan
