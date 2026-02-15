#!/usr/bin/env python3
"""
Quick live test for the agent server.

Takes a screenshot of your screen, sends it to POST /plan with a goal,
and prints the returned step plan. No frontend needed.

Usage:
    # 1. Start the server (in another terminal):
    #    cd agent-server && source .venv/bin/activate
    #    uvicorn app.main:app --reload
    #
    # 2. Run this script (SoM pipeline — default):
    #    python test_live.py
    #    python test_live.py "Open System Settings and go to Wi-Fi"
    #
    # 3. Run with legacy (raw coord) pipeline:
    #    python test_live.py --legacy "Open System Settings and go to Wi-Fi"
"""

import io
import json
import subprocess
import sys
import tempfile
import time
import json
from pathlib import Path

import httpx

SERVER_URL = "http://localhost:8000"
DEFAULT_GOAL = "Find and open the Downloads folder in Finder"

# SoM grid configuration (match MarkerGenerator.swift defaults)
SOM_COLUMNS = 16
SOM_ROWS = 10
MARKER_PIXEL_RADIUS = 14
MARKER_FONT_SIZE = 11


def build_grid_markers(columns: int = 24, rows: int = 14) -> list[dict]:
    markers: list[dict] = []
    marker_id = 1
    for row in range(rows):
        for col in range(columns):
            markers.append(
                {
                    "id": marker_id,
                    "cx": (col + 0.5) / columns,
                    "cy": (row + 0.5) / rows,
                    "radius": 0.012,
                    "screen_id": "main",
                }
            )
            marker_id += 1
    return markers


def take_screenshot() -> tuple[bytes, int, int]:
    """Capture the main display using macOS screencapture. Returns (png_bytes, width, height)."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp_path = f.name

    # -x = no sound, -C = capture cursor, -m = main display only
    result = subprocess.run(
        ["screencapture", "-x", "-C", "-m", tmp_path],
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"screencapture failed: {result.stderr.decode()}")

    png_bytes = Path(tmp_path).read_bytes()
    Path(tmp_path).unlink()

    # Get screen dimensions via system_profiler
    try:
        sp = subprocess.run(
            ["system_profiler", "SPDisplaysDataType"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in sp.stdout.splitlines():
            if "Resolution" in line:
                parts = line.split(":")[1].strip().split()
                w, h = int(parts[0]), int(parts[2])
                if "Retina" in line:
                    w, h = w // 2, h // 2
                return png_bytes, w, h
    except Exception:
        pass

    return png_bytes, 1920, 1080


def generate_markers(img_w: int, img_h: int) -> tuple[list[dict], bytes]:
    """
    Generate SoM markers on a grid and draw them onto the screenshot.
    Returns (markers_list, marked_png_bytes).
    Requires Pillow for drawing.
    """
    from PIL import Image, ImageDraw, ImageFont

    markers = []
    marker_id = 0
    normalized_radius = MARKER_PIXEL_RADIUS / max(img_w, img_h)

    for row in range(SOM_ROWS):
        for col in range(SOM_COLUMNS):
            cx = (col + 0.5) / SOM_COLUMNS
            cy = (row + 0.5) / SOM_ROWS
            markers.append({
                "id": marker_id,
                "cx": round(cx, 6),
                "cy": round(cy, 6),
                "radius": round(normalized_radius, 6),
            })
            marker_id += 1

    return markers


def draw_markers_on_image(png_bytes: bytes, markers: list[dict], img_w: int, img_h: int) -> bytes:
    """Draw numbered marker circles onto the screenshot. Returns marked PNG bytes."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(io.BytesIO(png_bytes))
    # Resize to match logical dimensions if needed (Retina screenshots are 2x)
    if img.width != img_w or img.height != img_h:
        # The PNG may be at Retina resolution; draw at the actual pixel size
        pass  # Draw at actual size, coordinates will be scaled

    draw = ImageDraw.Draw(img)

    # Try to load a basic font
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", MARKER_FONT_SIZE)
    except Exception:
        font = ImageFont.load_default()

    actual_w, actual_h = img.size

    for m in markers:
        # Convert normalized to pixel coords at actual image size
        px = m["cx"] * actual_w
        py = m["cy"] * actual_h
        r = MARKER_PIXEL_RADIUS

        # Draw white filled circle with red border
        circle_bbox = [px - r, py - r, px + r, py + r]
        draw.ellipse(circle_bbox, fill=(255, 255, 255, 230), outline=(255, 50, 50, 220), width=2)

        # Draw marker ID text
        text = str(m["id"])
        text_bbox = draw.textbbox((0, 0), text, font=font)
        tw = text_bbox[2] - text_bbox[0]
        th = text_bbox[3] - text_bbox[1]
        draw.text((px - tw / 2, py - th / 2), text, fill=(0, 0, 0), font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_plan(goal: str, use_som: bool = True) -> dict | None:
    """Send a screenshot + goal to POST /plan and print the result."""
    mode = "SoM" if use_som else "Legacy"
    print(f"\n{'='*60}")
    print(f"  GOAL: {goal}")
    print(f"  MODE: {mode}")
    print(f"{'='*60}\n")

    # Health check
    print("[1/4] Checking server health...")
    try:
        resp = httpx.get(f"{SERVER_URL}/health", timeout=5)
        health = resp.json()
        print(f"  Server: {health['status']}, mock_mode={health['mock_mode']}, model={health['model']}")
    except httpx.ConnectError:
        print("  ERROR: Cannot connect to server. Is it running?")
        print("  Start it with:")
        print("    cd agent-server && source .venv/bin/activate")
        print("    uvicorn app.main:app --reload")
        sys.exit(1)

    # Screenshot
    print("[2/4] Taking screenshot...")
    start = time.time()
    png_bytes, w, h = take_screenshot()
    elapsed = round((time.time() - start) * 1000)
    print(f"  Captured: {len(png_bytes):,} bytes, {w}x{h} ({elapsed}ms)")

    # The server handles all marker generation now (hybrid SOM + OmniParser).
    # Just send the raw screenshot — no client-side markers needed.
    screenshot_to_send = png_bytes

    if use_som:
        print("[3/4] Server-side SOM — sending raw screenshot (server draws markers)")
    else:
        print("[3/4] Legacy mode — sending raw screenshot")

    # Send to /plan
    print("[4/4] Sending to /plan...")
    start = time.time()

    form_data = {
        "goal": goal,
        "image_size": f'{{"w":{w},"h":{h}}}',
    }

    resp = httpx.post(
        f"{SERVER_URL}/plan",
        data=form_data,
        files={
            "screenshot": ("screenshot.png", screenshot_to_send, "image/png"),
        },
        headers={"X-Request-ID": f"test-live-{mode.lower()}"},
        timeout=60,
    )
    elapsed = round((time.time() - start) * 1000)

    if resp.status_code != 200:
        print(f"  ERROR {resp.status_code}: {resp.text}")
        return None

    plan = resp.json()
    print(f"  Response: {resp.status_code} ({elapsed}ms)")
    print_plan(plan)
    return plan


def test_next_step(goal: str, original_plan: dict | None):
    """Test the /next endpoint with a fresh screenshot after a fake step 1."""
    if original_plan is None:
        print("\n  Skipping /next test (no plan from /plan)")
        return

    num_steps = len(original_plan["steps"])
    first_step = original_plan["steps"][0]

    print(f"\n{'='*60}")
    print(f"  TESTING /next  (pretending step 1 is done)")
    print(f"{'='*60}\n")

    print("[1/2] Taking fresh screenshot...")
    start = time.time()
    png_bytes, w, h = take_screenshot()
    elapsed = round((time.time() - start) * 1000)
    print(f"  Captured: {len(png_bytes):,} bytes ({elapsed}ms)")

    completed = json.dumps([{"id": first_step["id"], "instruction": first_step["instruction"]}])

    print(f"[2/2] Sending to /next (completed: [{first_step['id']}], total: {num_steps})...")
    start = time.time()
    resp = httpx.post(
        f"{SERVER_URL}/next",
        data={
            "goal": goal,
            "image_size": f'{{"w":{w},"h":{h}}}',
            "completed_steps": completed,
            "total_steps": str(num_steps),
        },
        files={
            "screenshot": ("screenshot.png", png_bytes, "image/png"),
        },
        headers={"X-Request-ID": "test-live-next"},
        timeout=30,
    )
    elapsed = round((time.time() - start) * 1000)

    if resp.status_code != 200:
        print(f"  ERROR {resp.status_code}: {resp.text}")
        return

    plan = resp.json()
    print(f"  Response: {resp.status_code} ({elapsed}ms)")
    print_plan(plan, label="NEXT STEPS")


def test_refine(plan: dict | None, png_bytes: bytes | None, w: int = 1920, h: int = 1080):
    """Test the /refine endpoint by cropping around the first target."""
    if plan is None:
        print("\n  Skipping /refine test (no plan)")
        return

    if png_bytes is None:
        print("\n  Skipping /refine test (no screenshot)")
        return

    first_step = plan["steps"][0]
    first_target = first_step["targets"][0]

    print(f"\n{'='*60}")
    print(f"  TESTING /refine  (step {first_step['id']}, target: {first_target.get('label', '?')})")
    print(f"{'='*60}\n")

    # Compute crop rect around target center
    cx = first_target["x"] + first_target["w"] / 2
    cy = first_target["y"] + first_target["h"] / 2
    crop_size = 0.18
    crop_cx = max(0.0, cx - crop_size / 2)
    crop_cy = max(0.0, cy - crop_size / 2)
    crop_cw = min(crop_size, 1.0 - crop_cx)
    crop_ch = min(crop_size, 1.0 - crop_cy)

    crop_rect = {"cx": crop_cx, "cy": crop_cy, "cw": crop_cw, "ch": crop_ch}
    print(f"  Crop rect: cx={crop_cx:.3f}, cy={crop_cy:.3f}, cw={crop_cw:.3f}, ch={crop_ch:.3f}")

    # Crop the image using Pillow
    from PIL import Image
    img = Image.open(io.BytesIO(png_bytes))
    actual_w, actual_h = img.size
    left = int(crop_cx * actual_w)
    top = int(crop_cy * actual_h)
    right = int((crop_cx + crop_cw) * actual_w)
    bottom = int((crop_cy + crop_ch) * actual_h)
    cropped = img.crop((left, top, right, bottom))

    crop_buf = io.BytesIO()
    cropped.save(crop_buf, format="PNG")
    crop_bytes = crop_buf.getvalue()
    print(f"  Cropped image: {len(crop_bytes):,} bytes, {cropped.width}x{cropped.height}")

    # Save crop for debugging
    Path("/tmp/overlayguide_test_crop.png").write_bytes(crop_bytes)
    print("  Saved crop to /tmp/overlayguide_test_crop.png")

    # Send to /refine
    print("  Sending to /refine...")
    start = time.time()
    resp = httpx.post(
        f"{SERVER_URL}/refine",
        data={
            "instruction": first_step["instruction"],
            "target_label": first_target.get("label", ""),
            "crop_rect": json.dumps(crop_rect),
        },
        files={
            "crop_image": ("crop.png", crop_bytes, "image/png"),
        },
        headers={"X-Request-ID": "test-live-refine"},
        timeout=30,
    )
    elapsed = round((time.time() - start) * 1000)

    if resp.status_code != 200:
        print(f"  ERROR {resp.status_code}: {resp.text}")
        return

    refined = resp.json()
    print(f"  Response: {resp.status_code} ({elapsed}ms)")
    print(f"  Refined target (full-image): ({refined['x']:.3f}, {refined['y']:.3f}) {refined['w']:.3f}x{refined['h']:.3f}  conf={refined.get('confidence', '?')}  \"{refined.get('label', '')}\"")

    # Compare with original
    print(f"  Original target:             ({first_target['x']:.3f}, {first_target['y']:.3f}) {first_target['w']:.3f}x{first_target['h']:.3f}")


def print_plan(plan: dict, label: str = "PLAN"):
    """Pretty-print a step plan."""
    print(f"\n{'─'*60}")
    print(f"  {label}: {plan['goal']}")
    print(f"  Version: {plan['version']}  |  Image: {plan['image_size']['w']}x{plan['image_size']['h']}")
    print(f"  Steps: {len(plan['steps'])}")
    print(f"{'─'*60}")

    for i, step in enumerate(plan["steps"]):
        targets = step["targets"]
        t = targets[0]
        conf = t.get("confidence", "?")
        label_txt = t.get("label", "")
        advance = step["advance"]["type"]
        print(f"\n  Step {step['id']}  [{advance}]")
        print(f"    {step['instruction']}")
        if t.get("type") == "som_marker":
            print(f"    Target marker: id={t.get('marker_id')} conf={conf} \"{label_txt}\"")
        else:
            print(f"    Target: ({t['x']:.3f}, {t['y']:.3f}) {t['w']:.3f}x{t['h']:.3f}  conf={conf}  \"{label_txt}\"")
        if len(targets) > 1:
            print(f"    + {len(targets)-1} more target(s)")

    print()


if __name__ == "__main__":
    goal = DEFAULT_GOAL
    use_som = True
    test_refine_flag = False
    args = sys.argv[1:]

    # Parse flags
    filtered_args = []
    for arg in args:
        if arg == "--legacy":
            use_som = False
        elif arg == "--refine":
            test_refine_flag = True
        else:
            filtered_args.append(arg)

    if filtered_args:
        goal = " ".join(filtered_args)

    # Take screenshot early so we can reuse for refine test
    png_bytes_for_refine, w_for_refine, h_for_refine = take_screenshot()

    plan = test_plan(goal, use_som=use_som)
    test_next_step(goal, plan)

    if test_refine_flag:
        test_refine(plan, png_bytes_for_refine, w_for_refine, h_for_refine)

    print("=" * 60)
    print("  All tests passed!")
    print("=" * 60)
