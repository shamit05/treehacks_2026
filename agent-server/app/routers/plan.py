# app/routers/plan.py
# Owner: Eng 3 (Agent Pipeline)
#
# POST /plan endpoint.
# Receives a goal + screenshot, returns a StepPlan JSON.
# Supports MOCK_MODE for demo reliability.
#
# Pipeline options (controlled by USE_OMNIPARSER env var):
#   OmniParser (default): screenshot → OmniParser → element list → LLM → StepPlan
#   SoM (legacy):         screenshot → grid markers → LLM → two-pass refine → StepPlan

import io
import json
import os

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
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
from app.services.agent import (
    AgentError,
    generate_omniparser_plan,
    generate_omniparser_refine,
    generate_plan,
    generate_som_plan,
    generate_som_refine,
)
from app.services.mock import get_mock_plan
from app.services.omniparser import (
    OmniElement,
    OmniParserResult,
    detect_elements,
    draw_numbered_boxes,
    format_elements_context,
    parse_screenshot as omniparser_parse,
)

router = APIRouter()

MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024  # 20 MB

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

                # Clamp to [0,1]
                rx = max(0.0, full_x1)
                ry = max(0.0, full_y1)
                rw = min(full_x2 - full_x1, 1.0 - rx)
                rh = min(full_y2 - full_y1, 1.0 - ry)

                # Ensure minimum size
                rw = max(rw, 0.02)
                rh = max(rh, 0.02)

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

    Hybrid pipeline (default, USE_OMNIPARSER=true):
      Screenshot → SOM coarse grid (8x5) → LLM picks region
      → crop → OmniParser YOLO (precise elements) → LLM picks element → StepPlan

    SoM-only pipeline (legacy, USE_OMNIPARSER=false):
      Screenshot → SOM grid (8x5) → LLM → two-pass sub-grid refinement → StepPlan
    """
    request_id = getattr(request.state, "request_id", "unknown")

    use_omniparser = os.getenv("USE_OMNIPARSER", "true").lower() == "true"
    # Legacy SoM fallback: only when OmniParser is disabled and markers_json != "none"
    use_som = not use_omniparser and markers_json != "none"

    pipeline = "hybrid" if use_omniparser else ("som" if use_som else "legacy")
    print(f"[plan] rid={request_id} goal={goal!r} pipeline={pipeline}")

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
    # Max 2 LLM calls:
    #   Call 1: YOLO full screen → LLM generates plan + picks elements
    #   Call 2 (only if step 1 has low confidence): crop → YOLO → LLM refines step 1
    # Steps beyond step 1 always use the fast-path bbox — they'll be
    # re-planned via /next when the user completes each step anyway.
    _REFINE_CONFIDENCE = 0.5

    try:
        if use_omniparser:
            # ===== Call 1: YOLO full screen → LLM picks elements =====
            print(f"[plan] rid={request_id} YOLO on full screen...")
            full_elements = detect_elements(screenshot_bytes)
            print(f"[plan] rid={request_id} YOLO detected {len(full_elements)} elements")

            annotated_full = draw_numbered_boxes(screenshot_bytes, full_elements)
            try:
                with open("/tmp/overlayguide_fast_annotated.png", "wb") as f:
                    f.write(annotated_full)
            except Exception:
                pass

            elements_ctx = format_elements_context(full_elements)
            omni_plan = await generate_omniparser_plan(
                goal=goal,
                image_size=parsed_size,
                annotated_screenshot_bytes=annotated_full,
                elements_context=elements_ctx,
                learning_profile=learning_profile,
                app_context=app_context,
                session_summary=session_summary,
                request_id=request_id,
            )

            plan = _omni_plan_to_step_plan(omni_plan, full_elements)

            for i, (omni_step, step) in enumerate(zip(omni_plan.steps, plan.steps)):
                conf = omni_step.confidence or 0.0
                for t in step.targets:
                    print(f"[plan] rid={request_id} step={step.id} ({t.x:.3f},{t.y:.3f},{t.w:.3f},{t.h:.3f}) conf={conf:.2f} label={t.label!r}")

            # ===== Call 2 (optional): refine ONLY step 1 if low confidence =====
            if plan.steps and omni_plan.steps:
                first_conf = omni_plan.steps[0].confidence or 0.0
                first_step = plan.steps[0]
                first_target = first_step.targets[0] if first_step.targets else None

                if first_conf < _REFINE_CONFIDENCE and first_target and first_target.x is not None:
                    print(f"[plan] rid={request_id} step 1 conf={first_conf:.2f} < {_REFINE_CONFIDENCE} — refining via crop...")
                    try:
                        crop_rect, crop_bytes = _crop_region(screenshot_bytes, first_target)
                        print(f"[plan] rid={request_id} crop=({crop_rect.cx:.3f},{crop_rect.cy:.3f},{crop_rect.cw:.3f},{crop_rect.ch:.3f})")

                        crop_elements = detect_elements(crop_bytes)
                        print(f"[plan] rid={request_id} YOLO detected {len(crop_elements)} elements in crop")

                        if crop_elements:
                            annotated_crop = draw_numbered_boxes(crop_bytes, crop_elements)
                            try:
                                with open("/tmp/overlayguide_refine_crop.png", "wb") as f:
                                    f.write(annotated_crop)
                            except Exception:
                                pass

                            crop_ctx = format_elements_context(crop_elements)
                            result = await generate_omniparser_refine(
                                instruction=first_step.instruction,
                                target_label=first_target.label or "",
                                crop_image_bytes=annotated_crop,
                                elements_context=crop_ctx,
                                request_id=request_id,
                            )

                            picked_ids = result.get("element_ids", [])
                            elem_map = {e.id: e for e in crop_elements}
                            found = [elem_map[eid] for eid in picked_ids if eid in elem_map]

                            if found:
                                all_x1 = [e.bbox_xyxy[0] for e in found]
                                all_y1 = [e.bbox_xyxy[1] for e in found]
                                all_x2 = [e.bbox_xyxy[2] for e in found]
                                all_y2 = [e.bbox_xyxy[3] for e in found]

                                rx = max(0.0, crop_rect.cx + min(all_x1) * crop_rect.cw)
                                ry = max(0.0, crop_rect.cy + min(all_y1) * crop_rect.ch)
                                rw = max((max(all_x2) - min(all_x1)) * crop_rect.cw, 0.02)
                                rh = max((max(all_y2) - min(all_y1)) * crop_rect.ch, 0.02)
                                rw = min(rw, 1.0 - rx)
                                rh = min(rh, 1.0 - ry)

                                refined_step = Step(
                                    id=first_step.id,
                                    instruction=first_step.instruction,
                                    targets=[TargetRect(
                                        type=TargetType.bbox_norm,
                                        x=rx, y=ry, w=rw, h=rh,
                                        confidence=result.get("confidence"),
                                        label=result.get("label"),
                                    )],
                                    advance=first_step.advance,
                                    safety=first_step.safety,
                                )
                                # Replace step 1 with refined version
                                plan = StepPlan(
                                    version=plan.version,
                                    goal=plan.goal,
                                    image_size=plan.image_size,
                                    steps=[refined_step] + list(plan.steps[1:]),
                                )
                                print(f"[plan] rid={request_id} step 1 REFINED ({rx:.3f},{ry:.3f},{rw:.3f},{rh:.3f}) label={result.get('label')!r}")
                    except Exception as e:
                        print(f"[plan] rid={request_id} step 1 refine failed: {e}, keeping fast-path bbox")
                else:
                    print(f"[plan] rid={request_id} step 1 conf={first_conf:.2f} — no refine needed")

            for step in plan.steps:
                for t in step.targets:
                    print(f"[plan] rid={request_id} FINAL: step={step.id} ({t.x:.3f},{t.y:.3f},{t.w:.3f},{t.h:.3f}) label={t.label!r}")

        elif use_som:
            # ----- SoM pipeline (legacy) -----
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

            # Two-pass SoM: dense sub-grid on zoomed crop
            plan = await _refine_plan_two_pass(plan, screenshot_bytes, request_id)

            for step in plan.steps:
                for t in step.targets:
                    print(f"[plan] rid={request_id} REFINED TARGET: step={step.id} ({t.x:.3f},{t.y:.3f},{t.w:.3f},{t.h:.3f}) label={t.label!r}")
        else:
            # ----- Legacy pipeline: model outputs raw coordinates -----
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

    print(f"[plan] rid={request_id} success, {len(plan.steps)} steps (pipeline={pipeline})")
    return plan
