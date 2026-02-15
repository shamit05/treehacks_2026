#!/usr/bin/env python3
"""
Accuracy test for the SoM two-pass pipeline.

Takes a screenshot, runs the full pipeline offline (no server needed),
saves debug images at every step, and draws the final bbox on the
original screenshot so you can visually check accuracy.

Usage:
    cd agent-server
    source .venv/bin/activate
    python test_accuracy.py
    python test_accuracy.py "Click the File menu"
    
Then open /tmp/som_test/ in Finder to inspect all debug images.
"""

import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from PIL import Image, ImageDraw, ImageFont

# Import pipeline functions
from app.routers.plan import (
    _generate_markers_and_image,
    _som_plan_to_step_plan,
    _crop_and_draw_sub_markers,
    _REFINED_BBOX_PAD,
    SOM_COLUMNS,
    SOM_ROWS,
    REFINE_SUB_COLS,
    REFINE_SUB_ROWS,
)
from app.schemas.step_plan import ImageSize, TargetRect
from app.services.agent import generate_som_plan, generate_som_refine

OUT_DIR = Path("/tmp/som_test")
DEFAULT_GOAL = "Click the File menu to save the file"


def take_screenshot() -> tuple[bytes, int, int]:
    """Capture screenshot, return (png_bytes, actual_w, actual_h)."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp = f.name
    subprocess.run(["screencapture", "-x", "-C", "-m", tmp], capture_output=True, timeout=10)
    png = Path(tmp).read_bytes()
    Path(tmp).unlink()
    img = Image.open(io.BytesIO(png))
    return png, img.width, img.height


def draw_bbox_on_image(img_bytes: bytes, targets: list[dict], label: str) -> bytes:
    """Draw colored bboxes on an image. targets = [{x,y,w,h,label,color}]."""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 20)
    except Exception:
        font = ImageFont.load_default()
    
    w, h = img.size
    for t in targets:
        x1 = int(t["x"] * w)
        y1 = int(t["y"] * h)
        x2 = int((t["x"] + t["w"]) * w)
        y2 = int((t["y"] + t["h"]) * h)
        color = t.get("color", (0, 255, 0))
        draw.rectangle([x1, y1, x2, y2], outline=color, width=3)
        lbl = t.get("label", "")
        if lbl:
            draw.text((x1, max(0, y1 - 22)), lbl, fill=color, font=font)
    
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def run_test(goal: str):
    OUT_DIR.mkdir(exist_ok=True)
    
    model = os.getenv("OPENAI_MODEL", "gpt-4o")
    print(f"\n{'='*70}")
    print(f"  SoM Accuracy Test")
    print(f"  Model: {model}")
    print(f"  Goal: {goal}")
    print(f"  Grid: {SOM_COLUMNS}x{SOM_ROWS} coarse, {REFINE_SUB_COLS}x{REFINE_SUB_ROWS} fine")
    print(f"  Output: {OUT_DIR}/")
    print(f"{'='*70}\n")

    # 1. Take screenshot
    print("[1/5] Taking screenshot...")
    t0 = time.time()
    png_bytes, actual_w, actual_h = take_screenshot()
    print(f"  {actual_w}x{actual_h}, {len(png_bytes):,} bytes ({time.time()-t0:.1f}s)")
    (OUT_DIR / "01_raw_screenshot.png").write_bytes(png_bytes)

    # Get logical size (for image_size param)
    # On Retina, logical = actual / 2
    logical_w = actual_w // 2 if actual_w > 2000 else actual_w
    logical_h = actual_h // 2 if actual_h > 2000 else actual_h
    image_size = ImageSize(w=logical_w, h=logical_h)
    print(f"  Logical: {logical_w}x{logical_h}, Actual: {actual_w}x{actual_h}")

    # 2. Generate coarse markers
    print("\n[2/5] Generating coarse markers...")
    t0 = time.time()
    markers, marked_bytes = _generate_markers_and_image(png_bytes)
    print(f"  {len(markers)} markers, {len(marked_bytes):,} bytes ({time.time()-t0:.1f}s)")
    (OUT_DIR / "02_coarse_markers.png").write_bytes(marked_bytes)

    # 3. Pass 1: model picks coarse markers
    print(f"\n[3/5] Pass 1: asking {model} to pick coarse markers...")
    t0 = time.time()
    som_plan = await generate_som_plan(
        goal=goal,
        image_size=image_size,
        screenshot_bytes=marked_bytes,
        request_id="test-accuracy",
    )
    elapsed1 = time.time() - t0
    print(f"  {len(som_plan.steps)} steps ({elapsed1:.1f}s)")
    
    marker_map = {m.id: m for m in markers}
    for step in som_plan.steps:
        ids = [st.marker_id for st in step.som_targets]
        positions = [(f"{marker_map[i].cx:.3f},{marker_map[i].cy:.3f}" if i in marker_map else "?") for i in ids]
        print(f"  Step {step.id}: markers={ids} pos=[{', '.join(positions)}] label={step.som_targets[0].label!r}")

    # Convert to coarse plan
    coarse_plan = _som_plan_to_step_plan(som_plan, markers)
    
    # Draw coarse targets on screenshot
    coarse_targets = []
    for step in coarse_plan.steps:
        for t in step.targets:
            coarse_targets.append({"x": t.x, "y": t.y, "w": t.w, "h": t.h,
                                   "label": f"{step.id}: {t.label}", "color": (255, 0, 0)})
    coarse_vis = draw_bbox_on_image(png_bytes, coarse_targets, "coarse")
    (OUT_DIR / "03_coarse_targets.png").write_bytes(coarse_vis)

    # 4. Pass 2: refine each target
    print(f"\n[4/5] Pass 2: refining with {REFINE_SUB_COLS}x{REFINE_SUB_ROWS} sub-grid...")
    refined_targets_all = []
    
    for si, step in enumerate(coarse_plan.steps):
        for ti, target in enumerate(step.targets):
            t0 = time.time()
            crop_rect, marked_crop, sub_markers = _crop_and_draw_sub_markers(png_bytes, target)
            
            crop_name = f"04_crop_s{si}_t{ti}.png"
            (OUT_DIR / crop_name).write_bytes(marked_crop)
            
            img_crop = Image.open(io.BytesIO(marked_crop))
            cell_w = img_crop.width / REFINE_SUB_COLS
            cell_h = img_crop.height / REFINE_SUB_ROWS
            min_cell = min(cell_w, cell_h)
            marker_r = max(8, int(min_cell * 0.25))
            
            print(f"  Step {step.id}: crop=({crop_rect.cx:.3f},{crop_rect.cy:.3f},{crop_rect.cw:.3f},{crop_rect.ch:.3f})")
            print(f"    Crop pixels: {img_crop.width}x{img_crop.height}, cell: {cell_w:.0f}x{cell_h:.0f}, marker_r: {marker_r}")
            
            result = await generate_som_refine(
                instruction=step.instruction,
                target_label=target.label or "",
                crop_image_bytes=marked_crop,
                request_id="test-accuracy",
            )
            elapsed2 = time.time() - t0
            
            picked_ids = result.get("marker_ids", [])
            sub_map = {m["id"]: m for m in sub_markers}
            found = [sub_map[mid] for mid in picked_ids if mid in sub_map]
            
            if not found:
                print(f"    NO VALID sub-markers from {picked_ids}, keeping coarse ({elapsed2:.1f}s)")
                refined_targets_all.append({
                    "x": target.x, "y": target.y, "w": target.w, "h": target.h,
                    "label": f"{step.id}: {target.label} (coarse)", "color": (255, 165, 0)
                })
                continue
            
            min_x = min(m["cx_full"] for m in found)
            max_x = max(m["cx_full"] for m in found)
            min_y = min(m["cy_full"] for m in found)
            max_y = max(m["cy_full"] for m in found)
            
            pad = _REFINED_BBOX_PAD
            rx = max(0.0, min_x - pad)
            ry = max(0.0, min_y - pad)
            rw = min(max_x - min_x + pad * 2, 1.0 - rx)
            rh = min(max_y - min_y + pad * 2, 1.0 - ry)
            rw = max(rw, 0.035)
            rh = max(rh, 0.035)
            
            print(f"    Picked sub-markers: {picked_ids} ({len(found)} valid)")
            print(f"    Refined bbox: ({rx:.3f},{ry:.3f},{rw:.3f},{rh:.3f}) = {int(rx*actual_w)}..{int((rx+rw)*actual_w)}x, {int(ry*actual_h)}..{int((ry+rh)*actual_h)}y pixels ({elapsed2:.1f}s)")
            
            refined_targets_all.append({
                "x": rx, "y": ry, "w": rw, "h": rh,
                "label": f"{step.id}: {result.get('label', '')} [{','.join(str(i) for i in picked_ids)}]",
                "color": (0, 255, 0)
            })

    # 5. Draw final result
    print(f"\n[5/5] Drawing final result...")
    # Both coarse (red) and refined (green) on the same image
    all_targets = coarse_targets + refined_targets_all
    final_vis = draw_bbox_on_image(png_bytes, all_targets, "final")
    (OUT_DIR / "05_final_result.png").write_bytes(final_vis)
    
    # Also just refined
    refined_vis = draw_bbox_on_image(png_bytes, refined_targets_all, "refined")
    (OUT_DIR / "05_refined_only.png").write_bytes(refined_vis)
    
    print(f"\n{'='*70}")
    print(f"  Done! Open /tmp/som_test/ in Finder to inspect:")
    print(f"    01_raw_screenshot.png   - original screenshot")
    print(f"    02_coarse_markers.png   - screenshot with coarse grid")
    print(f"    03_coarse_targets.png   - red boxes from pass 1")
    print(f"    04_crop_s*_t*.png       - zoomed crops with sub-grid")
    print(f"    05_final_result.png     - red (coarse) + green (refined)")
    print(f"    05_refined_only.png     - just the green refined boxes")
    print(f"{'='*70}")


if __name__ == "__main__":
    goal = DEFAULT_GOAL
    if sys.argv[1:]:
        goal = " ".join(sys.argv[1:])
    asyncio.run(run_test(goal))
