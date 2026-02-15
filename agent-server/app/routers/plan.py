# app/routers/plan.py
# Owner: Eng 3 (Agent Pipeline)
#
# POST /plan endpoint.
# Receives a goal + screenshot, returns a StepPlan JSON.
# Supports MOCK_MODE for demo reliability.
#
# Pipeline: Screenshot → YOLO full image → annotated screenshot → single Gemini call → StepPlan

import asyncio
import io
import json
import os
import time
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from PIL import Image, ImageDraw, ImageFont

from app.schemas.step_plan import (
    CropRect,
    ImageSize,
    OmniPlanResponse,
    SoMMarker,
    SoMStepPlan,
    Step,
    StepPlan,
    TargetRect,
    TargetType,
)
from app.services.agent import AgentError, is_native_genai_available, upload_images_to_gemini, verify_element
from app.services.debug import DebugSession
from app.services.mock import get_mock_plan
from app.services.search import search_for_goal, get_stored_search_context
from app.services.omniparser import (
    OmniElement,
    OmniParserResult,
    detect_elements,
    draw_numbered_boxes,
    format_elements_context,
    parse_screenshot as omniparser_parse,
    snap_to_nearest_element,
)

router = APIRouter()

MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024  # 20 MB


# ---------------------------------------------------------------------------
# Shared: convert Gemini step_data → (rx, ry, rw, rh) with YOLO snapping
# ---------------------------------------------------------------------------

from app.schemas.step_plan import Advance, AdvanceType

_ADVANCE_MAP = {
    "click_in_target": AdvanceType.click_in_target,
    "text_entered_or_next": AdvanceType.text_entered_or_next,
    "manual_next": AdvanceType.manual_next,
    "wait_for_ui_change": AdvanceType.wait_for_ui_change,
}


def _resolve_bbox(
    step_data: dict,
    elements: list,
    request_id: str = "",
    endpoint: str = "plan",
) -> tuple[float, float, float, float]:
    """
    Resolve a bounding box from Gemini's response, using YOLO elements for precision.

    Priority order (element_id-first with cross-validation):
      1. element_id → direct YOLO element lookup (Gemini visually matched the numbered box)
      2. Cross-validate: if both element_id and box_2d present, check agreement.
         If they disagree (centers > 0.08 apart), prefer box_2d snapping since Gemini
         may have misread the box number but correctly identified the spatial location.
      3. box_2d only → convert from 0-1000, snap to nearest YOLO element
      4. Fallback: center of screen

    Returns (x, y, w, h) in normalized [0,1] coords.
    """
    elem_map = {e.id: e for e in elements}
    step_id = step_data.get("id", "?")
    box_2d = step_data.get("box_2d")
    element_id = step_data.get("element_id")

    rx, ry, rw, rh = None, None, None, None

    # Parse box_2d into normalized coords for use in cross-validation
    raw_x, raw_y, raw_w, raw_h = None, None, None, None
    if box_2d and len(box_2d) == 4:
        ymin, xmin, ymax, xmax = box_2d
        raw_x = xmin / 1000.0
        raw_y = ymin / 1000.0
        raw_w = (xmax - xmin) / 1000.0
        raw_h = (ymax - ymin) / 1000.0
        print(f"[{endpoint}] rid={request_id} step={step_id} Gemini box_2d={box_2d} -> raw=({raw_x:.3f},{raw_y:.3f},{raw_w:.3f},{raw_h:.3f})")

    # --- Priority 1: element_id (direct visual match from numbered box) ---
    if element_id is not None and element_id in elem_map:
        elem = elem_map[element_id]
        eid_x, eid_y, eid_w, eid_h = elem.bbox_xywh
        eid_cx = eid_x + eid_w / 2
        eid_cy = eid_y + eid_h / 2
        print(f"[{endpoint}] rid={request_id} step={step_id} element_id={element_id} -> ({eid_x:.3f},{eid_y:.3f},{eid_w:.3f},{eid_h:.3f})")

        # --- Cross-validate with box_2d if both are present ---
        if raw_x is not None:
            box_cx = raw_x + raw_w / 2
            box_cy = raw_y + raw_h / 2
            center_dist = ((eid_cx - box_cx) ** 2 + (eid_cy - box_cy) ** 2) ** 0.5

            if center_dist <= 0.08:
                # AGREE: element_id and box_2d are close — trust element_id (pixel-perfect from YOLO)
                rx, ry, rw, rh = eid_x, eid_y, eid_w, eid_h
                print(f"[{endpoint}] rid={request_id} step={step_id} AGREE (dist={center_dist:.3f}): using element_id={element_id}")
            else:
                # DISAGREE: Gemini may have misread the box number.
                # Snap box_2d to nearest YOLO element — spatial location is more reliable
                # than a potentially misread number label.
                snap_x, snap_y, snap_w, snap_h, snap_id = snap_to_nearest_element(
                    raw_x, raw_y, raw_w, raw_h, elements
                )
                if snap_id is not None and snap_id != element_id:
                    # box_2d snapped to a DIFFERENT element — use it
                    print(f"[{endpoint}] rid={request_id} step={step_id} DISAGREE (dist={center_dist:.3f}): "
                          f"element_id={element_id} vs box_2d snap=elem[{snap_id}]. Using box_2d snap.")
                    rx, ry, rw, rh = snap_x, snap_y, snap_w, snap_h
                elif snap_id == element_id:
                    # box_2d snapped to the SAME element — extra confirmation, use it
                    print(f"[{endpoint}] rid={request_id} step={step_id} CONFIRMED: "
                          f"box_2d snap also chose elem[{snap_id}]. Using YOLO bbox.")
                    rx, ry, rw, rh = snap_x, snap_y, snap_w, snap_h
                else:
                    # box_2d didn't snap to anything — trust element_id despite distance
                    print(f"[{endpoint}] rid={request_id} step={step_id} DISAGREE but no snap: "
                          f"using element_id={element_id} (no better alternative)")
                    rx, ry, rw, rh = eid_x, eid_y, eid_w, eid_h
        else:
            # No box_2d, just use element_id directly
            rx, ry, rw, rh = eid_x, eid_y, eid_w, eid_h
            print(f"[{endpoint}] rid={request_id} step={step_id} using element_id={element_id} (no box_2d)")

    # --- Priority 2: box_2d only (no valid element_id) → snap to YOLO ---
    if rx is None and raw_x is not None:
        rx, ry, rw, rh, matched_id = snap_to_nearest_element(
            raw_x, raw_y, raw_w, raw_h, elements
        )
        if matched_id is not None:
            print(f"[{endpoint}] rid={request_id} step={step_id} SNAPPED box_2d to elem[{matched_id}]=({rx:.3f},{ry:.3f},{rw:.3f},{rh:.3f})")
        else:
            print(f"[{endpoint}] rid={request_id} step={step_id} no snap match, using raw Gemini coords")

    # --- Priority 3: fallback ---
    if rx is None:
        rx, ry, rw, rh = 0.4, 0.4, 0.2, 0.2
        print(f"[{endpoint}] rid={request_id} step={step_id} FALLBACK center-of-screen")

    # Clamp
    rx = max(0.0, min(rx, 1.0))
    ry = max(0.0, min(ry, 1.0))
    rw = max(0.02, min(rw, 1.0 - rx))
    rh = max(0.02, min(rh, 1.0 - ry))

    return rx, ry, rw, rh


# ---------------------------------------------------------------------------
# Iterative verification: crop + YOLO + re-ask to confirm element
# ---------------------------------------------------------------------------
# Verification crop parameters
_VERIFY_CROP_PAD = 0.08  # padding around target bbox for crop
_VERIFY_MIN_CROP = 0.15  # minimum crop dimension
_VERIFY_CONFIDENCE_THRESHOLD = 0.6  # verify steps below this confidence


async def _verify_and_correct_step(
    step_data: dict,
    resolved_x: float,
    resolved_y: float,
    resolved_w: float,
    resolved_h: float,
    original_screenshot_bytes: bytes,
    elements: list,
    request_id: str = "",
) -> tuple[float, float, float, float]:
    """
    Verify a resolved bbox by cropping, running YOLO on the crop,
    and asking the LLM to confirm the element.

    If the LLM says the element is wrong, returns the corrected bbox.
    Otherwise returns the original bbox.
    """
    instruction = step_data.get("instruction", "")
    element_id = step_data.get("element_id")
    label = step_data.get("label", "")
    step_id = step_data.get("id", "?")

    # Compute crop region around the resolved bbox
    pad = _VERIFY_CROP_PAD
    cx = max(0.0, resolved_x - pad)
    cy = max(0.0, resolved_y - pad)
    cw = min(resolved_w + pad * 2, 1.0 - cx)
    ch = min(resolved_h + pad * 2, 1.0 - cy)

    # Ensure minimum crop size
    if cw < _VERIFY_MIN_CROP:
        center = resolved_x + resolved_w / 2
        cx = max(0.0, center - _VERIFY_MIN_CROP / 2)
        cw = min(_VERIFY_MIN_CROP, 1.0 - cx)
    if ch < _VERIFY_MIN_CROP:
        center = resolved_y + resolved_h / 2
        cy = max(0.0, center - _VERIFY_MIN_CROP / 2)
        ch = min(_VERIFY_MIN_CROP, 1.0 - cy)

    # Crop the screenshot
    img = Image.open(io.BytesIO(original_screenshot_bytes))
    actual_w, actual_h = img.size
    left = int(cx * actual_w)
    top = int(cy * actual_h)
    right = int((cx + cw) * actual_w)
    bottom = int((cy + ch) * actual_h)
    cropped = img.crop((left, top, right, bottom)).convert("RGB")

    crop_buf = io.BytesIO()
    cropped.save(crop_buf, format="PNG")
    raw_crop_bytes = crop_buf.getvalue()

    # Run YOLO on the crop for precise local detection
    crop_elements = detect_elements(raw_crop_bytes)
    crop_elements.sort(key=lambda e: (e.bbox_xyxy[1], e.bbox_xyxy[0]))
    for i, e in enumerate(crop_elements):
        e.id = i

    if not crop_elements:
        print(f"[verify] rid={request_id} step={step_id} no elements in crop, keeping original bbox")
        return resolved_x, resolved_y, resolved_w, resolved_h

    # Draw numbered boxes on the crop
    annotated_crop = draw_numbered_boxes(raw_crop_bytes, crop_elements)
    crop_ctx = format_elements_context(crop_elements)

    # Save debug images
    try:
        with open(f"/tmp/og_verify_{step_id}_crop.png", "wb") as f:
            f.write(annotated_crop)
        with open(f"/tmp/og_verify_{step_id}_raw.png", "wb") as f:
            f.write(raw_crop_bytes)
    except Exception:
        pass

    # Find which crop element corresponds to our original resolved bbox
    # Map the resolved bbox into crop-relative coordinates
    rel_x = (resolved_x - cx) / cw
    rel_y = (resolved_y - cy) / ch
    rel_w = resolved_w / cw
    rel_h = resolved_h / ch

    # Find the crop element closest to our resolved bbox center
    rel_cx = rel_x + rel_w / 2
    rel_cy = rel_y + rel_h / 2
    best_crop_elem = None
    best_dist = float("inf")
    for ce in crop_elements:
        ce_x, ce_y, ce_w, ce_h = ce.bbox_xywh
        ce_cx = ce_x + ce_w / 2
        ce_cy = ce_y + ce_h / 2
        dist = ((rel_cx - ce_cx) ** 2 + (rel_cy - ce_cy) ** 2) ** 0.5
        if dist < best_dist:
            best_dist = dist
            best_crop_elem = ce

    crop_element_id = best_crop_elem.id if best_crop_elem else 0

    print(f"[verify] rid={request_id} step={step_id} crop=({cx:.3f},{cy:.3f},{cw:.3f},{ch:.3f}) "
          f"{len(crop_elements)} elements, checking crop-elem[{crop_element_id}]")

    # Ask the LLM to verify
    try:
        result = await verify_element(
            instruction=instruction,
            element_id=crop_element_id,
            label=label,
            annotated_crop_bytes=annotated_crop,
            raw_crop_bytes=raw_crop_bytes,
            elements_context=crop_ctx,
            request_id=request_id,
        )
    except Exception as e:
        print(f"[verify] rid={request_id} step={step_id} verification LLM call failed: {e}, keeping original")
        return resolved_x, resolved_y, resolved_w, resolved_h

    if result.get("correct", True):
        print(f"[verify] rid={request_id} step={step_id} CONFIRMED correct")
        return resolved_x, resolved_y, resolved_w, resolved_h

    # Element was WRONG — try to correct
    correct_crop_id = result.get("correct_element_id")
    crop_elem_map = {e.id: e for e in crop_elements}

    if correct_crop_id is not None and correct_crop_id in crop_elem_map:
        # Map corrected crop-relative bbox back to full-image coords
        ce = crop_elem_map[correct_crop_id]
        ce_x, ce_y, ce_w, ce_h = ce.bbox_xywh
        full_x = cx + ce_x * cw
        full_y = cy + ce_y * ch
        full_w = ce_w * cw
        full_h = ce_h * ch
        print(f"[verify] rid={request_id} step={step_id} CORRECTED to crop-elem[{correct_crop_id}] "
              f"-> full ({full_x:.3f},{full_y:.3f},{full_w:.3f},{full_h:.3f}) "
              f"reasoning: {result.get('reasoning', '')[:80]}")
        return full_x, full_y, full_w, full_h

    # Try box_2d from verification response
    verify_box = result.get("box_2d")
    if verify_box and len(verify_box) == 4:
        ymin, xmin, ymax, xmax = verify_box
        crop_rx = xmin / 1000.0
        crop_ry = ymin / 1000.0
        crop_rw = (xmax - xmin) / 1000.0
        crop_rh = (ymax - ymin) / 1000.0
        # Snap to nearest crop element
        snap_x, snap_y, snap_w, snap_h, snap_id = snap_to_nearest_element(
            crop_rx, crop_ry, crop_rw, crop_rh, crop_elements
        )
        # Convert back to full-image coords
        full_x = cx + snap_x * cw
        full_y = cy + snap_y * ch
        full_w = snap_w * cw
        full_h = snap_h * ch
        print(f"[verify] rid={request_id} step={step_id} CORRECTED via verify box_2d "
              f"-> full ({full_x:.3f},{full_y:.3f},{full_w:.3f},{full_h:.3f})")
        return full_x, full_y, full_w, full_h

    print(f"[verify] rid={request_id} step={step_id} verification said wrong but no correction, keeping original")
    return resolved_x, resolved_y, resolved_w, resolved_h


# ---------------------------------------------------------------------------
# Session cache: pre-processed YOLO results keyed by session_id.
# Populated by /start, consumed by /plan-stream.
# Entries expire after 120s to avoid memory leaks.
# ---------------------------------------------------------------------------
_session_cache: dict[str, dict] = {}
_SESSION_TTL = 120  # seconds


def _prune_sessions():
    """Remove expired sessions from the cache."""
    now = time.time()
    expired = [sid for sid, v in _session_cache.items() if now - v["ts"] > _SESSION_TTL]
    for sid in expired:
        del _session_cache[sid]

# SoM grid configuration — coarse grid for hybrid pipeline.
# 6x4 = 24 markers — big, easy to read. LLM selects MULTIPLE markers
# to cover the region of interest, which naturally creates a bigger crop.
SOM_COLUMNS = 6
SOM_ROWS = 4

# Default half-size of the bbox drawn around a marker center (normalized).
# For 6x4 grid: cells are 16.7% x 25%, so half-cell = 8.3% x 12.5%.
# When multiple markers are selected, the bbox spans all of them.
_DEFAULT_MARKER_BBOX_HALF = 0.08


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
    # With the 6x4 grid: very big, impossible to misread.
    # On 3024px wide image: radius=68, font=46 — huge and clear.
    # On 1512px wide image: radius=34, font=23 — very readable.
    marker_radius = max(28, actual_w // 45)
    font_size = max(18, actual_w // 65)
    border_width = max(3, actual_w // 500)

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


# ---------------------------------------------------------------------------
# OmniParser pipeline: convert OmniPlanResponse → StepPlan using element bboxes
# ---------------------------------------------------------------------------


def _omni_plan_to_step_plan(
    omni_plan: OmniPlanResponse,
    elements: list[OmniElement],
) -> StepPlan:
    """
    Convert an OmniParser plan (element IDs per step) to a standard StepPlan.
    Each element_id maps to a bounding box from OmniParser's detection.
    If a step references multiple elements, they are merged into a single bbox.
    """
    elem_map = {e.id: e for e in elements}
    converted_steps: list[Step] = []

    for omni_step in omni_plan.steps:
        # Collect bboxes for all referenced elements
        found_elements: list[OmniElement] = []
        for eid in omni_step.element_ids:
            elem = elem_map.get(eid)
            if elem is None:
                print(f"[omni] WARNING: element_id={eid} not found in OmniParser results, skipping")
                continue
            found_elements.append(elem)

        if not found_elements:
            # Fallback: center of screen
            converted_steps.append(Step(
                id=omni_step.id,
                instruction=omni_step.instruction,
                targets=[TargetRect(
                    type=TargetType.bbox_norm,
                    x=0.4, y=0.4, w=0.2, h=0.2,
                    confidence=0.1,
                    label="fallback — element not found",
                )],
                advance=omni_step.advance,
                safety=omni_step.safety,
            ))
            continue

        # Compute bounding box spanning all referenced elements
        all_x1 = [e.bbox_xyxy[0] for e in found_elements]
        all_y1 = [e.bbox_xyxy[1] for e in found_elements]
        all_x2 = [e.bbox_xyxy[2] for e in found_elements]
        all_y2 = [e.bbox_xyxy[3] for e in found_elements]

        merged_x1 = max(0.0, min(all_x1))
        merged_y1 = max(0.0, min(all_y1))
        merged_x2 = min(1.0, max(all_x2))
        merged_y2 = min(1.0, max(all_y2))

        w = max(merged_x2 - merged_x1, 0.02)
        h = max(merged_y2 - merged_y1, 0.02)

        # Clamp to ensure x+w <= 1 and y+h <= 1
        x = min(merged_x1, 1.0 - w)
        y = min(merged_y1, 1.0 - h)

        # Use the first element's content as label
        label = found_elements[0].content if found_elements else None

        targets = [TargetRect(
            type=TargetType.bbox_norm,
            x=x, y=y, w=w, h=h,
            confidence=omni_step.confidence,
            label=label,
        )]

        converted_steps.append(Step(
            id=omni_step.id,
            instruction=omni_step.instruction,
            targets=targets,
            advance=omni_step.advance,
            safety=omni_step.safety,
        ))

    return StepPlan(
        version=omni_plan.version,
        goal=omni_plan.goal,
        image_size=omni_plan.image_size,
        steps=converted_steps,
    )


# ---------------------------------------------------------------------------
# Two-pass SoM refinement (legacy): dense sub-grid on a zoomed crop
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Hybrid SOM + OmniParser refinement
# ---------------------------------------------------------------------------

# Generous padding around the coarse SOM target when cropping for OmniParser.
# Bigger crop = captures the actual element even if SOM is off by one marker.
_OMNI_REFINE_PADDING = 0.10

# Minimum crop size so YOLO has enough context to detect elements.
_OMNI_MIN_CROP = 0.20

# If the target center is within this distance of a screen edge,
# snap the crop to that edge (don't leave a tiny gap).
_EDGE_SNAP_THRESHOLD = 0.15


def _crop_region(
    original_bytes: bytes,
    target_rect: TargetRect,
) -> tuple[CropRect, bytes]:
    """
    Crop a region around the coarse target from the original screenshot.
    Edge-aware: if the target is near a screen edge, the crop extends
    all the way to that edge so we don't miss elements at the boundary.
    Returns (crop_rect, cropped_png_bytes). No markers or boxes drawn.
    """
    img = Image.open(io.BytesIO(original_bytes))
    actual_w, actual_h = img.size

    pad = _OMNI_REFINE_PADDING
    cx = target_rect.x - pad
    cy = target_rect.y - pad
    cw = target_rect.w + pad * 2
    ch = target_rect.h + pad * 2

    # Ensure minimum crop size (centered on the target)
    if cw < _OMNI_MIN_CROP:
        center = target_rect.x + target_rect.w / 2
        cx = center - _OMNI_MIN_CROP / 2
        cw = _OMNI_MIN_CROP
    if ch < _OMNI_MIN_CROP:
        center = target_rect.y + target_rect.h / 2
        cy = center - _OMNI_MIN_CROP / 2
        ch = _OMNI_MIN_CROP

    # Edge snapping: if the crop is near a screen edge, extend to that edge.
    # This prevents missing elements at the very edge of the screen
    # (e.g. Apple menu at x=0, Dock at y=0.95, menu bar at y=0).
    target_cx = target_rect.x + target_rect.w / 2
    target_cy = target_rect.y + target_rect.h / 2

    # Snap to left edge
    if target_cx < _EDGE_SNAP_THRESHOLD:
        cw = cw + cx  # extend leftward
        cx = 0.0
    # Snap to top edge
    if target_cy < _EDGE_SNAP_THRESHOLD:
        ch = ch + cy  # extend upward
        cy = 0.0
    # Snap to right edge
    if target_cx > (1.0 - _EDGE_SNAP_THRESHOLD):
        cw = 1.0 - cx
    # Snap to bottom edge
    if target_cy > (1.0 - _EDGE_SNAP_THRESHOLD):
        ch = 1.0 - cy

    # Clamp to screen bounds
    cx = max(0.0, cx)
    cy = max(0.0, cy)
    cw = min(cw, 1.0 - cx)
    ch = min(ch, 1.0 - cy)

    crop_rect = CropRect(cx=cx, cy=cy, cw=cw, ch=ch)

    left = int(cx * actual_w)
    top = int(cy * actual_h)
    right = int((cx + cw) * actual_w)
    bottom = int((cy + ch) * actual_h)
    cropped = img.crop((left, top, right, bottom)).convert("RGB")

    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    return crop_rect, buf.getvalue()


async def _refine_with_omniparser(
    plan: StepPlan,
    original_screenshot_bytes: bytes,
    request_id: str,
) -> StepPlan:
    """
    Hybrid SOM + OmniParser refinement.

    For each step's coarse SOM target:
      1. Crop the region from the original screenshot (generous padding).
      2. Run YOLO on the crop to detect precise UI elements.
      3. Draw numbered boxes on the crop.
      4. Ask the LLM to pick which element is the target.
      5. Map the crop-relative bbox back to full-image coordinates.
    """
    refined_steps: list[Step] = []

    for step in plan.steps:
        refined_targets: list[TargetRect] = []

        for target in step.targets:
            try:
                # 1. Crop the region
                crop_rect, crop_bytes = _crop_region(
                    original_screenshot_bytes, target
                )
                print(f"[hybrid] rid={request_id} step={step.id} crop=({crop_rect.cx:.3f},{crop_rect.cy:.3f},{crop_rect.cw:.3f},{crop_rect.ch:.3f})")

                # 2. Run YOLO on the crop
                elements = detect_elements(crop_bytes)
                print(f"[hybrid] rid={request_id} step={step.id} YOLO detected {len(elements)} elements in crop")

                if not elements:
                    print(f"[hybrid] rid={request_id} step={step.id} no elements in crop, keeping coarse bbox")
                    refined_targets.append(target)
                    continue

                # 3. Draw numbered boxes on the crop
                annotated_crop = draw_numbered_boxes(crop_bytes, elements)

                # Save crop for debugging
                try:
                    with open(f"/tmp/overlayguide_hybrid_crop_{step.id}.png", "wb") as f:
                        f.write(annotated_crop)
                except Exception:
                    pass

                # 4. Ask LLM to pick the element
                elements_ctx = format_elements_context(elements)
                result = await generate_omniparser_refine(
                    instruction=step.instruction,
                    target_label=target.label or "",
                    crop_image_bytes=annotated_crop,
                    elements_context=elements_ctx,
                    request_id=request_id,
                    raw_crop_bytes=crop_bytes,
                )

                picked_ids = result.get("element_ids", [])
                elem_map = {e.id: e for e in elements}

                # Find all valid elements
                found = [elem_map[eid] for eid in picked_ids if eid in elem_map]
                if not found:
                    print(f"[hybrid] rid={request_id} step={step.id} no valid elements from {picked_ids}, keeping coarse bbox")
                    refined_targets.append(target)
                    continue

                # 5. Compute bbox from selected elements and map to full-image coords
                # Element bboxes are crop-relative [0,1] → convert to full-image [0,1]
                all_x1 = [e.bbox_xyxy[0] for e in found]
                all_y1 = [e.bbox_xyxy[1] for e in found]
                all_x2 = [e.bbox_xyxy[2] for e in found]
                all_y2 = [e.bbox_xyxy[3] for e in found]

                # Crop-relative → full-image
                full_x1 = crop_rect.cx + min(all_x1) * crop_rect.cw
                full_y1 = crop_rect.cy + min(all_y1) * crop_rect.ch
                full_x2 = crop_rect.cx + max(all_x2) * crop_rect.cw
                full_y2 = crop_rect.cy + max(all_y2) * crop_rect.ch

                # Guard against inverted coordinates (bad YOLO detection)
                if full_x2 <= full_x1 or full_y2 <= full_y1:
                    print(f"[hybrid] rid={request_id} step={step.id} inverted bbox, keeping coarse")
                    refined_targets.append(target)
                    continue

                # Clamp origin to [0,1]
                rx = max(0.0, min(full_x1, 1.0))
                ry = max(0.0, min(full_y1, 1.0))

                # Compute width/height from the actual bbox extent
                rw = max(full_x2 - full_x1, 0.02)
                rh = max(full_y2 - full_y1, 0.02)

                # Clamp so bbox stays within [0,1]
                rw = min(rw, 1.0 - rx)
                rh = min(rh, 1.0 - ry)

                refined = TargetRect(
                    type=TargetType.bbox_norm,
                    x=rx, y=ry, w=rw, h=rh,
                    confidence=result.get("confidence"),
                    label=result.get("label"),
                )
                ids_str = ",".join(str(i) for i in picked_ids)
                print(f"[hybrid] rid={request_id} step={step.id} elements=[{ids_str}] ({len(found)} valid) -> bbox ({rx:.3f},{ry:.3f},{rw:.3f},{rh:.3f}) label={result.get('label')!r}")
                refined_targets.append(refined)

            except Exception as e:
                print(f"[hybrid] rid={request_id} step={step.id} OmniParser refine failed: {e}, keeping coarse bbox")
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


@router.post("/start")
async def start_session(
    request: Request,
    image_size: str = Form(...),
    screenshot: UploadFile = File(...),
):
    """
    Pre-process a screenshot while the user is typing their goal.
    Runs YOLO detection + annotated image generation eagerly.
    Returns a session_id that /plan-stream can reference to skip YOLO.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    _prune_sessions()

    try:
        size_dict = json.loads(image_size)
        parsed_size = ImageSize.model_validate(size_dict)
    except (json.JSONDecodeError, Exception) as e:
        raise HTTPException(status_code=422, detail=f"Invalid image_size JSON: {e}")

    screenshot_bytes = await screenshot.read()
    if len(screenshot_bytes) == 0:
        raise HTTPException(status_code=422, detail="Screenshot file is empty")
    if len(screenshot_bytes) > MAX_SCREENSHOT_BYTES:
        raise HTTPException(status_code=413, detail="Screenshot exceeds 20 MB limit")

    session_id = str(uuid4())
    print(f"[start] rid={request_id} sid={session_id} running YOLO on {len(screenshot_bytes)} bytes")

    # Run YOLO detection (the slow part we want to pre-compute)
    elements = detect_elements(screenshot_bytes)
    elements.sort(key=lambda e: (e.bbox_xyxy[1], e.bbox_xyxy[0]))
    for i, e in enumerate(elements):
        e.id = i

    annotated_bytes = draw_numbered_boxes(screenshot_bytes, elements)
    elements_ctx = format_elements_context(elements)

    # Upload images to Gemini File API so /plan-stream can skip base64 re-encoding
    gemini_annotated_file = None
    gemini_raw_file = None
    if is_native_genai_available():
        try:
            gemini_annotated_file, gemini_raw_file = upload_images_to_gemini(
                annotated_bytes, screenshot_bytes
            )
            print(f"[start] rid={request_id} sid={session_id} images uploaded to Gemini")
        except Exception as e:
            print(f"[start] rid={request_id} sid={session_id} Gemini upload failed (non-fatal): {e}")

    _session_cache[session_id] = {
        "elements": elements,
        "annotated_bytes": annotated_bytes,
        "elements_ctx": elements_ctx,
        "screenshot_bytes": screenshot_bytes,
        "image_size": parsed_size,
        "gemini_annotated_file": gemini_annotated_file,
        "gemini_raw_file": gemini_raw_file,
        "ts": time.time(),
    }

    print(f"[start] rid={request_id} sid={session_id} cached {len(elements)} elements, gemini_files={'yes' if gemini_annotated_file else 'no'}")
    return {"session_id": session_id, "element_count": len(elements)}


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
    skip_search: bool = Form(False),
):
    """
    Generate a step-by-step guidance plan from a goal and screenshot.

    Pipeline: Screenshot → YOLO full image → annotated screenshot → single Gemini call → StepPlan
    One YOLO pass + one LLM call. No SOM grid, no cropping, no refinement.
    Web search runs concurrently to enrich the LLM prompt with relevant context.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    print(f"[plan] rid={request_id} goal={goal!r} pipeline=gemini-oneshot skip_search={skip_search}")

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

    # --- Debug session: saves all outputs for post-hoc analysis ---
    dbg = DebugSession(request_id, goal=goal, endpoint="plan")

    try:
        # ===== STEP 0: Kick off web search concurrently (non-blocking) =====
        search_task = None
        if not skip_search:
            async def _safe_search() -> str:
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
            search_task = asyncio.create_task(_safe_search())
        else:
            print(f"[plan] rid={request_id} search skipped (skip_search=true)")

        # ===== STEP 1: Save original screenshot =====
        dbg.save_image("original_screenshot", screenshot_bytes,
                       f"{parsed_size.w}x{parsed_size.h}")

        # ===== STEP 2: YOLO on full screenshot =====
        elements = detect_elements(screenshot_bytes)
        print(f"[plan] rid={request_id} YOLO detected {len(elements)} elements on full screenshot")

        # Sort by position (top-to-bottom, left-to-right) and renumber
        elements.sort(key=lambda e: (e.bbox_xyxy[1], e.bbox_xyxy[0]))
        for i, e in enumerate(elements):
            e.id = i

        # ===== STEP 3: Draw numbered boxes on screenshot =====
        annotated_bytes = draw_numbered_boxes(screenshot_bytes, elements)
        dbg.save_image("yolo_annotated", annotated_bytes,
                       f"{len(elements)} elements")

        elements_ctx = format_elements_context(elements)
        dbg.save_text("yolo_elements", elements_ctx,
                      f"{len(elements)} elements")

        # ===== STEP 3.5: Collect search results if available =====
        search_context = ""
        if search_task is not None:
            search_context = await search_task
            if search_context:
                print(f"[plan] rid={request_id} search returned {len(search_context)} chars of context")
                dbg.save_text("search_context", search_context)
            else:
                dbg.save_text("search_context", "(no results or search disabled)")

        # ===== STEP 4: Single Gemini call — one-shot plan =====
        from app.services.agent import generate_gemini_plan
        result = await generate_gemini_plan(
            goal=goal,
            annotated_screenshot_bytes=annotated_bytes,
            raw_screenshot_bytes=screenshot_bytes,
            elements_context=elements_ctx,
            request_id=request_id,
            search_context=search_context,
        )

        # Save prompt + raw response from Gemini
        debug_meta = result.pop("_debug", {})
        if debug_meta:
            dbg.save_prompt_and_response(
                "gemini_plan",
                prompt=debug_meta.get("prompt", ""),
                response=debug_meta.get("raw_response", ""),
                model=debug_meta.get("model", ""),
            )

        # Save the parsed Gemini output
        dbg.save_json("gemini_parsed_output", result)

        # ===== STEP 5: Convert Gemini response → StepPlan with YOLO snapping =====
        converted_steps: list[Step] = []

        for step_data in result.get("steps", []):
            step_id = step_data.get("id", f"s{len(converted_steps) + 1}")
            instruction = step_data.get("instruction", "")
            label = step_data.get("label")
            confidence = step_data.get("confidence", 0.5)
            advance_type = step_data.get("advance", "click_in_target")
            reasoning = step_data.get("reasoning", "")

            # Log Gemini's reasoning for debugging element selection
            if reasoning:
                print(f"[plan] rid={request_id} step={step_id} REASONING: {reasoning}")
            print(f"[plan] rid={request_id} step={step_id} element_id={step_data.get('element_id')} box_2d={step_data.get('box_2d')} label={label!r}")

            rx, ry, rw, rh = _resolve_bbox(step_data, elements, request_id, "plan")

            # ===== STEP 5.5: Iterative verification for important steps =====
            step_idx = len(converted_steps)
            needs_verify = (
                step_idx == 0
                or confidence < _VERIFY_CONFIDENCE_THRESHOLD
            )
            verification_result = None
            if needs_verify:
                print(f"[plan] rid={request_id} step={step_id} running verification pass "
                      f"(idx={step_idx}, conf={confidence})")
                try:
                    vx, vy, vw, vh = await _verify_and_correct_step(
                        step_data=step_data,
                        resolved_x=rx,
                        resolved_y=ry,
                        resolved_w=rw,
                        resolved_h=rh,
                        original_screenshot_bytes=screenshot_bytes,
                        elements=elements,
                        request_id=request_id,
                    )
                    if (vx, vy, vw, vh) != (rx, ry, rw, rh):
                        print(f"[plan] rid={request_id} step={step_id} VERIFICATION CORRECTED: "
                              f"({rx:.3f},{ry:.3f},{rw:.3f},{rh:.3f}) -> ({vx:.3f},{vy:.3f},{vw:.3f},{vh:.3f})")
                        verification_result = {"corrected": True, "before": (rx,ry,rw,rh), "after": (vx,vy,vw,vh)}
                        rx, ry, rw, rh = vx, vy, vw, vh
                    else:
                        print(f"[plan] rid={request_id} step={step_id} VERIFICATION CONFIRMED original bbox")
                        verification_result = {"corrected": False}
                except Exception as ve:
                    print(f"[plan] rid={request_id} step={step_id} verification failed (non-fatal): {ve}")
                    verification_result = {"error": str(ve)}

            # Save per-step resolution trace
            dbg.save_step_resolution(
                step_id=step_id,
                step_data=step_data,
                resolved_bbox=(rx, ry, rw, rh),
                verification_result=verification_result,
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

        plan = StepPlan(
            version="v1",
            goal=goal,
            image_size=parsed_size,
            steps=converted_steps,
        )

        # ===== FINAL: draw final bbox on original screenshot =====
        _save_bbox_debug(screenshot_bytes, plan,
                         lambda d: dbg.save_image("FINAL_overlay", d), color=(0, 220, 50))

        for step in plan.steps:
            for t in step.targets:
                print(f"[plan] rid={request_id} FINAL: step={step.id} ({t.x:.3f},{t.y:.3f},{t.w:.3f},{t.h:.3f}) label={t.label!r}")

        # Finalize debug session
        dbg.finalize(plan.model_dump())

    except AgentError as e:
        print(f"[plan] rid={request_id} agent error: {e}")
        raise HTTPException(status_code=502, detail=f"Agent failed: {e}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[plan] rid={request_id} unexpected error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")

    print(f"[plan] rid={request_id} success, {len(plan.steps)} steps (pipeline=gemini-oneshot)")
    return plan


# ---------------------------------------------------------------------------
# Streaming endpoint: /plan-stream (SSE)
# ---------------------------------------------------------------------------


@router.post("/plan-stream")
async def create_plan_stream(
    request: Request,
    goal: str = Form(...),
    image_size: str = Form(...),
    screenshot: UploadFile = File(None),
    learning_profile: str = Form(None),
    app_context: str = Form(None),
    session_summary: str = Form(None),
    markers_json: str = Form(None),
    session_id: str = Form(None),
    skip_search: bool = Form(False),
):
    """
    Streaming version of /plan. Returns newline-delimited JSON (NDJSON):
      Line 1: {"type":"instruction","text":"Click on..."} — as soon as instruction is available
      Line 2: {"type":"plan","data":{...full StepPlan...}} — when complete

    If session_id is provided (from /start), uses cached YOLO results — skips detection.
    Web search runs concurrently to enrich the LLM prompt with relevant context.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    print(f"[plan-stream] rid={request_id} goal={goal!r} session_id={session_id!r} skip_search={skip_search}")

    # Try to use cached session from /start
    cached = _session_cache.pop(session_id, None) if session_id else None

    if cached and time.time() - cached["ts"] < _SESSION_TTL:
        screenshot_bytes = cached["screenshot_bytes"]
        parsed_size = cached["image_size"]
        prefetched_elements = cached["elements"]
        prefetched_annotated = cached["annotated_bytes"]
        prefetched_ctx = cached["elements_ctx"]
        gemini_annotated_file = cached.get("gemini_annotated_file")
        gemini_raw_file = cached.get("gemini_raw_file")
        # Consume the uploaded file to avoid resource leaks
        if screenshot is not None:
            await screenshot.read()
        print(f"[plan-stream] rid={request_id} using cached session {session_id} ({len(prefetched_elements)} elements, gemini_files={'yes' if gemini_annotated_file else 'no'})")
    else:
        prefetched_elements = None
        prefetched_annotated = None
        prefetched_ctx = None
        gemini_annotated_file = None
        gemini_raw_file = None
        try:
            size_dict = json.loads(image_size)
            parsed_size = ImageSize.model_validate(size_dict)
        except (json.JSONDecodeError, Exception) as e:
            raise HTTPException(status_code=422, detail=f"Invalid image_size JSON: {e}")
        if screenshot is None:
            raise HTTPException(status_code=422, detail="No screenshot and no valid session_id")
        screenshot_bytes = await screenshot.read()
        if len(screenshot_bytes) == 0:
            raise HTTPException(status_code=422, detail="Screenshot file is empty")

    # Kick off search concurrently before entering the event stream
    search_task = None
    if not skip_search:
        async def _safe_search() -> str:
            try:
                return await search_for_goal(
                    goal=goal,
                    screenshot_bytes=screenshot_bytes,
                    app_context=app_context,
                    request_id=request_id,
                )
            except Exception as e:
                print(f"[plan-stream] rid={request_id} search failed (non-fatal): {type(e).__name__}: {e}")
                return ""
        search_task = asyncio.create_task(_safe_search())

    async def event_stream():
        try:
            # Use prefetched YOLO results if available, else run fresh
            if prefetched_elements is not None:
                elements = prefetched_elements
                annotated_bytes = prefetched_annotated
                elements_ctx = prefetched_ctx
            else:
                elements = detect_elements(screenshot_bytes)
                elements.sort(key=lambda e: (e.bbox_xyxy[1], e.bbox_xyxy[0]))
                for i, e in enumerate(elements):
                    e.id = i
                annotated_bytes = draw_numbered_boxes(screenshot_bytes, elements)
                elements_ctx = format_elements_context(elements)

            elem_map = {e.id: e for e in elements}

            # Collect search results if available
            search_context = ""
            if search_task is not None:
                search_context = await search_task
                if search_context:
                    print(f"[plan-stream] rid={request_id} search returned {len(search_context)} chars of context")

            # Choose streaming path: native Gemini files (fast) or OpenAI-compat (fallback)
            if gemini_annotated_file is not None and gemini_raw_file is not None:
                from app.services.agent import generate_gemini_plan_with_files_stream
                stream_gen = generate_gemini_plan_with_files_stream(
                    goal=goal,
                    annotated_file=gemini_annotated_file,
                    raw_file=gemini_raw_file,
                    elements_context=elements_ctx,
                    request_id=request_id,
                    search_context=search_context,
                )
                print(f"[plan-stream] rid={request_id} using native Gemini files path (fast)")
            else:
                from app.services.agent import generate_gemini_plan_stream
                stream_gen = generate_gemini_plan_stream(
                    goal=goal,
                    annotated_screenshot_bytes=annotated_bytes,
                    raw_screenshot_bytes=screenshot_bytes,
                    elements_context=elements_ctx,
                    request_id=request_id,
                    search_context=search_context,
                )
                print(f"[plan-stream] rid={request_id} using OpenAI-compat path (fallback)")

            full_result = None
            async for event in stream_gen:
                if event["type"] == "instruction":
                    yield json.dumps({"type": "instruction", "text": event["text"]}) + "\n"

                elif event["type"] == "plan":
                    full_result = event["data"]

            if full_result is None:
                yield json.dumps({"type": "error", "message": "No plan generated"}) + "\n"
                return

            # Convert Gemini result → StepPlan with YOLO snapping
            converted_steps = []
            for step_data in full_result.get("steps", []):
                step_id = step_data.get("id", f"s{len(converted_steps) + 1}")
                instruction = step_data.get("instruction", "")
                label = step_data.get("label")
                confidence = step_data.get("confidence", 0.5)
                advance_type = step_data.get("advance", "click_in_target")
                reasoning = step_data.get("reasoning", "")

                # Log Gemini's reasoning for debugging element selection
                if reasoning:
                    print(f"[plan-stream] rid={request_id} step={step_id} REASONING: {reasoning}")
                print(f"[plan-stream] rid={request_id} step={step_id} element_id={step_data.get('element_id')} box_2d={step_data.get('box_2d')} label={label!r}")

                rx, ry, rw, rh = _resolve_bbox(step_data, elements, request_id, "plan-stream")

                converted_steps.append(Step(
                    id=step_id,
                    instruction=instruction,
                    targets=[TargetRect(type=TargetType.bbox_norm, x=rx, y=ry, w=rw, h=rh, confidence=confidence, label=label)],
                    advance=Advance(type=_ADVANCE_MAP.get(advance_type, AdvanceType.click_in_target)),
                ))

            plan = StepPlan(version="v1", goal=goal, image_size=parsed_size, steps=converted_steps)
            plan_json = plan.model_dump()
            yield json.dumps({"type": "plan", "data": plan_json}) + "\n"

            print(f"[plan-stream] rid={request_id} done, {len(plan.steps)} steps")

        except Exception as e:
            import traceback
            traceback.print_exc()
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


def _save_bbox_debug(
    screenshot_bytes: bytes,
    plan: StepPlan,
    save_fn,
    color: tuple[int, int, int] = (0, 200, 50),
) -> None:
    """Draw all target bounding boxes on the original screenshot and pass bytes to save_fn."""
    try:
        img = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")
        actual_w, actual_h = img.size
        draw = ImageDraw.Draw(img)

        font_size = max(18, actual_w // 90)
        border_width = max(3, actual_w // 400)

        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
        except Exception:
            font = ImageFont.load_default()

        for step_idx, step in enumerate(plan.steps):
            for t in step.targets:
                if t.x is None or t.y is None or t.w is None or t.h is None:
                    continue
                x1 = int(t.x * actual_w)
                y1 = int(t.y * actual_h)
                x2 = int((t.x + t.w) * actual_w)
                y2 = int((t.y + t.h) * actual_h)

                draw.rectangle([x1, y1, x2, y2], outline=color, width=border_width)

                label = f"Step {step_idx + 1}: {t.label or step.instruction[:40]}"
                lbbox = draw.textbbox((0, 0), label, font=font)
                lw = lbbox[2] - lbbox[0] + 12
                lh = lbbox[3] - lbbox[1] + 8
                lx = max(0, x1)
                ly = max(0, y1 - lh - 2)
                draw.rectangle([lx, ly, lx + lw, ly + lh], fill=color)
                draw.text((lx + 6, ly + 4), label, fill=(255, 255, 255), font=font)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        save_fn(buf.getvalue())
    except Exception as e:
        print(f"[plan] _save_bbox_debug failed: {e}")
