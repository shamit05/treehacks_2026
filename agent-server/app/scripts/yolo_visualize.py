#!/usr/bin/env python3
"""Iterative zoom-and-click element identification.

Usage:
    # Just detect + save annotated image:
    python -m app.scripts.yolo_visualize input.png

    # Iterative zoom pick for a task:
    python -m app.scripts.yolo_visualize input.png --task "Click the File menu"

    # Tune YOLO parameters:
    python -m app.scripts.yolo_visualize input.png --imgsz 1280 --conf 0.05 --task "Open Settings"
"""

import argparse
import base64
import io
import json
import math
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from app.services.omniparser import OmniElement, detect_elements, draw_numbered_boxes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json(raw: str) -> dict:
    """Extract a JSON object from Claude's response."""
    clean = raw.strip().strip("`").strip()
    if clean.startswith("json"):
        clean = clean[4:].strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', clean, re.DOTALL)
        if match:
            return json.loads(match.group())
    return {}


def _draw_boxes_on_zoomed(
    zoomed_bytes: bytes,
    elements: list[OmniElement],
    scale: float,
    crop_x1: int,
    crop_y1: int,
    img_w: int,
    img_h: int,
) -> bytes:
    """Draw element bounding boxes (green) with ID labels on a zoomed crop."""
    img = Image.open(io.BytesIO(zoomed_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 14)
    except Exception:
        font = ImageFont.load_default()

    for e in elements:
        x1 = int((e.bbox_xyxy[0] * img_w - crop_x1) * scale)
        y1 = int((e.bbox_xyxy[1] * img_h - crop_y1) * scale)
        x2 = int((e.bbox_xyxy[2] * img_w - crop_x1) * scale)
        y2 = int((e.bbox_xyxy[3] * img_h - crop_y1) * scale)
        draw.rectangle([x1, y1, x2, y2], outline=(0, 255, 0), width=2)
        label = str(e.id)
        bbox = font.getbbox(label)
        lw, lh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        ly = y1 - lh - 4 if y1 - lh - 4 > 0 else y2 + 2
        draw.rectangle([x1, ly, x1 + lw + 4, ly + lh + 2], fill=(0, 0, 0))
        draw.text((x1 + 2, ly), label, fill=(0, 255, 0), font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _render_crosshair(image_bytes: bytes, x: int, y: int) -> bytes:
    """Draw a red crosshair at (x, y) and return PNG bytes."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    r = 15
    draw.line([(x - r, y), (x + r, y)], fill=(255, 0, 0), width=3)
    draw.line([(x, y - r), (x, y + r)], fill=(255, 0, 0), width=3)
    draw.ellipse([x - r, y - r, x + r, y + r], outline=(255, 0, 0), width=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _nearby_elements(
    click_x: float,
    click_y: float,
    elements: list[OmniElement],
    img_w: int,
    img_h: int,
    radius_px: float = 300,
) -> list[tuple[float, OmniElement]]:
    """Find ALL elements within radius_px of the click point, sorted by distance."""
    scored: list[tuple[float, OmniElement]] = []
    for e in elements:
        ex1 = e.bbox_xyxy[0] * img_w
        ey1 = e.bbox_xyxy[1] * img_h
        ex2 = e.bbox_xyxy[2] * img_w
        ey2 = e.bbox_xyxy[3] * img_h
        cx = (ex1 + ex2) / 2
        cy = (ey1 + ey2) / 2
        dist = math.sqrt((click_x - cx) ** 2 + (click_y - cy) ** 2)
        scored.append((dist, e))

    scored.sort(key=lambda x: x[0])
    nearby = [(d, e) for d, e in scored if d <= radius_px]

    # Fallback: at least return the closest element
    if not nearby and scored:
        nearby = [scored[0]]

    return nearby


def _crop_element(
    img: Image.Image,
    elem: OmniElement,
    pad: int = 5,
    min_side: int = 80,
) -> bytes:
    """Crop an element from the image, pad it, and scale up to be clearly visible."""
    img_w, img_h = img.size
    x1 = max(0, int(elem.bbox_xyxy[0] * img_w) - pad)
    y1 = max(0, int(elem.bbox_xyxy[1] * img_h) - pad)
    x2 = min(img_w, int(elem.bbox_xyxy[2] * img_w) + pad)
    y2 = min(img_h, int(elem.bbox_xyxy[3] * img_h) + pad)
    crop = img.crop((x1, y1, x2, y2))

    s = max(1.0, min_side / max(crop.width, crop.height, 1))
    crop = crop.resize(
        (max(1, int(crop.width * s)), max(1, int(crop.height * s))),
        Image.LANCZOS,
    )

    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()


def _visible_elements(
    elements: list[OmniElement],
    view_x1: int, view_y1: int, view_x2: int, view_y2: int,
    img_w: int, img_h: int,
) -> list[OmniElement]:
    """Return elements whose center falls inside the view rectangle."""
    visible = []
    for e in elements:
        ecx = (e.bbox_xyxy[0] + e.bbox_xyxy[2]) / 2 * img_w
        ecy = (e.bbox_xyxy[1] + e.bbox_xyxy[3]) / 2 * img_h
        if view_x1 <= ecx <= view_x2 and view_y1 <= ecy <= view_y2:
            visible.append(e)
    return visible


# ---------------------------------------------------------------------------
# Iterative zoom-and-click pipeline
# ---------------------------------------------------------------------------

def iterative_zoom_pick(
    screenshot_bytes: bytes,
    elements: list[OmniElement],
    task: str,
    input_path: str,
    zoom_rounds: int = 3,
    crop_frac: float = 0.25,
    nearby_pct: float = 0.10,
) -> dict:
    """Iterative zoom-and-click → disambiguate.

    1. Show full screenshot → Claude clicks
    2. Crop crop_frac centered on click, scale up → Claude clicks again
    3. Repeat for zoom_rounds total
    4. Find nearby elements around final click → show individual crops → Claude picks

    nearby_pct: radius for nearby search as fraction of max(img_w, img_h).
    """
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    img = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")
    img_w, img_h = img.size
    nearby_radius = nearby_pct * max(img_w, img_h)

    screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
    stem = Path(input_path).stem

    # Track current view in original-image pixel coordinates
    view_x1, view_y1 = 0, 0
    view_w_orig, view_h_orig = img_w, img_h  # size in original pixels
    current_scale = 1.0

    full_x, full_y = img_w / 2.0, img_h / 2.0
    click_reason = ""
    messages: list[dict] = []  # conversation history across rounds

    for rnd in range(zoom_rounds):
        rnd1 = rnd + 1
        print(f"\n  ── Zoom round {rnd1}/{zoom_rounds} ──")

        # ── Build current view image ──
        if rnd == 0:
            # Round 1: resize full screenshot to a controlled display size
            # so Claude's returned pixel coords match the image it actually sees.
            view_x1, view_y1 = 0, 0
            view_w_orig, view_h_orig = img_w, img_h

            target_long = 1200
            current_scale = min(1.0, target_long / max(img_w, img_h, 1))
            display_w = max(1, int(img_w * current_scale))
            display_h = max(1, int(img_h * current_scale))

            if current_scale < 1.0:
                resized = img.resize((display_w, display_h), Image.LANCZOS)
                buf = io.BytesIO()
                resized.save(buf, format="PNG")
                view_bytes = buf.getvalue()
            else:
                view_bytes = screenshot_bytes

            view_b64 = base64.b64encode(view_bytes).decode()
        else:
            # Crop crop_frac of previous view, centered on last click
            new_w = max(1, int(view_w_orig * crop_frac))
            new_h = max(1, int(view_h_orig * crop_frac))

            cx, cy = int(full_x), int(full_y)
            x1 = max(0, cx - new_w // 2)
            y1 = max(0, cy - new_h // 2)
            x2 = min(img_w, x1 + new_w)
            y2 = min(img_h, y1 + new_h)

            # Re-adjust if we hit image boundary
            if x2 - x1 < new_w:
                x1 = max(0, x2 - new_w)
            if y2 - y1 < new_h:
                y1 = max(0, y2 - new_h)

            view_x1, view_y1 = x1, y1
            view_w_orig = x2 - x1
            view_h_orig = y2 - y1

            crop = img.crop((x1, y1, x2, y2))
            target_long = 1200
            current_scale = max(1.0, min(4.0, target_long / max(crop.width, crop.height, 1)))
            zoomed = crop.resize(
                (max(1, int(crop.width * current_scale)),
                 max(1, int(crop.height * current_scale))),
                Image.LANCZOS,
            )
            display_w, display_h = zoomed.size

            buf = io.BytesIO()
            zoomed.save(buf, format="PNG")
            view_bytes = buf.getvalue()
            view_b64 = base64.b64encode(view_bytes).decode()

        view_x2 = view_x1 + view_w_orig
        view_y2 = view_y1 + view_h_orig
        print(f"  View: ({view_x1},{view_y1})→({view_x2},{view_y2}) "
              f"orig={view_w_orig}x{view_h_orig} scale={current_scale:.1f}x "
              f"display={display_w}x{display_h}")

        # ── Save annotated debug image ──
        vis_elems = _visible_elements(elements, view_x1, view_y1, view_x2, view_y2, img_w, img_h)
        annotated_view = _draw_boxes_on_zoomed(
            view_bytes, vis_elems, current_scale, view_x1, view_y1, img_w, img_h,
        )
        debug_path = str(Path(input_path).with_name(f"{stem}_zoom{rnd1}.png"))
        Path(debug_path).write_bytes(annotated_view)
        print(f"  Saved annotated view ({len(vis_elems)} elements): {debug_path}")

        # ── Ask Claude to click (normalized 0-1 coordinates) ──
        coord_instructions = (
            "Reply with ONLY a JSON object with x and y as FRACTIONS between 0.0 and 1.0:\n"
            "- x=0.0 means left edge, x=1.0 means right edge\n"
            "- y=0.0 means top edge, y=1.0 means bottom edge\n"
            "- Example: the center of the image would be {\"x\": 0.5, \"y\": 0.5}\n\n"
            "{\"x\": <float between 0.0 and 1.0>, \"y\": <float between 0.0 and 1.0>, \"reason\": \"<why>\"}"
        )

        if rnd == 0:
            prompt = (
                f"This is a screenshot of a desktop application.\n\n"
                f"Task: {task}\n\n"
                f"Point to where I should click to accomplish this task.\n\n"
                f"{coord_instructions}"
            )
        else:
            prompt = (
                f"I've zoomed into the area you pointed at. Here is the zoomed-in view.\n\n"
                f"Refine your click — point to the SAME target you identified before, "
                f"but now more precisely within THIS zoomed image.\n\n"
                f"{coord_instructions}"
            )

        # Build user message with image + prompt
        user_content: list[dict] = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": view_b64}},
            {"type": "text", "text": prompt},
        ]
        messages.append({"role": "user", "content": user_content})

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=150,
            messages=messages,
        )

        raw = response.content[0].text
        # Add assistant response to history so next round has context
        messages.append({"role": "assistant", "content": raw})
        parsed = _parse_json(raw)
        raw_x = float(parsed.get("x", 0.5))
        raw_y = float(parsed.get("y", 0.5))
        print(f"  Claude raw response: x={raw_x}, y={raw_y}")

        # Auto-detect if Claude returned pixel coords instead of 0-1 fractions
        if raw_x > 1.0 or raw_y > 1.0:
            print(f"  ⚠ Values > 1 detected — interpreting as pixel coords in {display_w}x{display_h} display")
            nx = raw_x / display_w
            ny = raw_y / display_h
        else:
            nx = raw_x
            ny = raw_y

        nx = max(0.0, min(1.0, nx))
        ny = max(0.0, min(1.0, ny))
        click_reason = parsed.get("reason", "")
        print(f"  Normalized: ({nx:.3f}, {ny:.3f}) — {click_reason}")

        # Convert normalized coords → full-image coords
        full_x = view_x1 + nx * view_w_orig
        full_y = view_y1 + ny * view_h_orig
        print(f"  → Full-image coords: ({full_x:.0f}, {full_y:.0f})")

        # Save click debug (annotated + crosshair in display pixel space)
        click_px = int(nx * display_w)
        click_py = int(ny * display_h)
        click_marked = _render_crosshair(annotated_view, click_px, click_py)
        click_dbg = str(Path(input_path).with_name(f"{stem}_zoom{rnd1}_click.png"))
        Path(click_dbg).write_bytes(click_marked)
        print(f"  Saved click debug: {click_dbg}")

    # ── Find nearby elements around final click ──
    print(f"\n  ── Disambiguation ──")
    print(f"  Nearby radius: {nearby_radius:.0f}px ({nearby_pct*100:.0f}% of {max(img_w, img_h)}px)")

    nearby = _nearby_elements(full_x, full_y, elements, img_w, img_h, radius_px=nearby_radius)
    print(f"  Found {len(nearby)} nearby elements:")
    for dist, e in nearby:
        ew = int((e.bbox_xyxy[2] - e.bbox_xyxy[0]) * img_w)
        eh = int((e.bbox_xyxy[3] - e.bbox_xyxy[1]) * img_h)
        print(f"    [{e.id:>3}] dist={dist:.0f}px size={ew}x{eh}")

    # If only 1 nearby element, skip disambiguation
    if len(nearby) == 1:
        eid = nearby[0][1].id
        print(f"  Only 1 nearby element — picking [{eid}] directly")
        return {
            "element_id": eid,
            "click_x": int(full_x),
            "click_y": int(full_y),
            "reason": click_reason,
        }

    # ── Build zoomed crop around click + nearby elements for context ──
    disambig_elems = [e for _, e in nearby]
    # Bounding box around click + all nearby elements
    all_x1 = min(int(full_x), *(int(e.bbox_xyxy[0] * img_w) for e in disambig_elems))
    all_y1 = min(int(full_y), *(int(e.bbox_xyxy[1] * img_h) for e in disambig_elems))
    all_x2 = max(int(full_x), *(int(e.bbox_xyxy[2] * img_w) for e in disambig_elems))
    all_y2 = max(int(full_y), *(int(e.bbox_xyxy[3] * img_h) for e in disambig_elems))

    pad = 120
    dx1 = max(0, all_x1 - pad)
    dy1 = max(0, all_y1 - pad)
    dx2 = min(img_w, all_x2 + pad)
    dy2 = min(img_h, all_y2 + pad)

    d_crop = img.crop((dx1, dy1, dx2, dy2))
    d_scale = max(1.0, min(4.0, 1200 / max(d_crop.width, d_crop.height, 1)))
    d_zoomed = d_crop.resize(
        (max(1, int(d_crop.width * d_scale)), max(1, int(d_crop.height * d_scale))),
        Image.LANCZOS,
    )
    buf = io.BytesIO()
    d_zoomed.save(buf, format="PNG")
    disambig_zoomed_bytes = buf.getvalue()

    # Save annotated disambiguation crop
    disambig_annotated = _draw_boxes_on_zoomed(
        disambig_zoomed_bytes, disambig_elems, d_scale, dx1, dy1, img_w, img_h,
    )
    disambig_path = str(Path(input_path).with_name(f"{stem}_disambig.png"))
    Path(disambig_path).write_bytes(disambig_annotated)
    print(f"  Saved disambiguation view: {disambig_path}")

    disambig_b64 = base64.b64encode(disambig_zoomed_bytes).decode()

    # ── Disambiguation call: full screenshot + zoomed context + individual crops ──
    print(f"  Disambiguating {len(nearby)} candidates...")
    content: list[dict] = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": screenshot_b64}},
        {"type": "text", "text": "Above: full screenshot showing the CURRENT UI state. Note any open menus, dropdowns, or dialogs."},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": disambig_b64}},
        {"type": "text", "text": "Above: zoomed view of the area around where you clicked."},
        {"type": "text", "text": (
            f"\nTask: {task}\n\n"
            "Below are individual crops of candidate UI elements near the click point. "
            "Pick the one to click NEXT given the CURRENT UI state.\n\n"
            "IMPORTANT: If a dropdown/menu is already open, click the item INSIDE it — "
            "not the button that opened it.\n"
        )},
    ]

    for dist, e in nearby:
        crop_bytes = _crop_element(img, e)
        crop_b64 = base64.b64encode(crop_bytes).decode()
        content.append({"type": "text", "text": f"\nElement {e.id}:"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": crop_b64},
        })

    content.append({
        "type": "text",
        "text": (
            f"\nWhich element should be clicked NEXT for: \"{task}\"?\n\n"
            "If a menu is already open, pick the item from the menu.\n\n"
            "Reply ONLY: {\"element_id\": <int>, \"reason\": \"<why>\"}"
        ),
    })

    response2 = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=100,
        messages=[{"role": "user", "content": content}],
    )

    raw2 = response2.content[0].text
    parsed2 = _parse_json(raw2)
    eid = parsed2.get("element_id", nearby[0][1].id)
    reason = parsed2.get("reason", click_reason)
    print(f"  Disambiguated → element [{eid}]: {reason}")

    # Verify eid is valid
    nearby_ids = {e.id for _, e in nearby}
    if eid not in nearby_ids:
        print(f"  ⚠ ID {eid} not in nearby set {nearby_ids}, falling back to closest")
        eid = nearby[0][1].id

    return {
        "element_id": eid,
        "click_x": int(full_x),
        "click_y": int(full_y),
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Top-level identify_element
# ---------------------------------------------------------------------------

def identify_element(
    screenshot_bytes: bytes,
    task: str,
    elements: list[OmniElement],
    input_path: str = "input.png",
    zoom_rounds: int = 3,
    crop_frac: float = 0.25,
    nearby_pct: float = 0.10,
) -> dict:
    """Iterative zoom → click → disambiguate pipeline. Returns result dict."""
    result = iterative_zoom_pick(
        screenshot_bytes, elements, task, input_path,
        zoom_rounds=zoom_rounds, crop_frac=crop_frac, nearby_pct=nearby_pct,
    )

    eid = result.get("element_id", -1)
    if eid >= 0:
        elem = next((e for e in elements if e.id == eid), None)
        if elem:
            print(f"  Mapped to element [{eid}]")
            result["bbox"] = elem.bbox_xyxy
        else:
            print(f"  ⚠ Element ID {eid} not found in elements")
            result["bbox"] = None
    else:
        print("  ⚠ No matching element found")
        result["bbox"] = None

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="Input screenshot PNG")
    parser.add_argument("output", nargs="?", default=None, help="Output path for annotated image")
    parser.add_argument("--conf", type=float, default=0.0)
    parser.add_argument("--iou", type=float, default=0.0)
    parser.add_argument("--imgsz", type=int, default=640, help="YOLO inference resolution")
    parser.add_argument("--task", type=str, default=None, help="Iterative zoom pick for this task")
    parser.add_argument("--zoom-rounds", type=int, default=3, help="Number of zoom rounds (default 3)")
    parser.add_argument("--crop-frac", type=float, default=0.25, help="Fraction to crop each zoom round (default 0.25)")
    parser.add_argument("--nearby-pct", type=float, default=0.10, help="Nearby radius as pct of max dim (default 0.10)")
    args = parser.parse_args()

    screenshot_bytes = Path(args.input).read_bytes()
    stem = Path(args.input).stem

    # Detect elements
    elements = detect_elements(screenshot_bytes, args.conf, args.iou, imgsz=args.imgsz)
    print(f"{len(elements)} elements detected (imgsz={args.imgsz})")

    # Save full annotated image
    annotated = draw_numbered_boxes(screenshot_bytes, elements)
    suffix = f"_yolo_{args.imgsz}" if args.imgsz != 640 else "_yolo"
    out_annotated = args.output or str(Path(args.input).with_name(f"{stem}{suffix}.png"))
    Path(out_annotated).write_bytes(annotated)
    print(f"Saved annotated: {out_annotated}")

    # Iterative zoom pick
    if args.task:
        if not os.getenv("ANTHROPIC_API_KEY"):
            print("ERROR: ANTHROPIC_API_KEY not set")
            sys.exit(1)

        result = identify_element(
            screenshot_bytes, args.task, elements, args.input,
            zoom_rounds=args.zoom_rounds,
            crop_frac=args.crop_frac,
            nearby_pct=args.nearby_pct,
        )
        eid = result.get("element_id", -1)
        print(f"\n✅ Picked element [{eid}]: {result.get('reason', '')}")
        print(f"   Click point: ({result.get('click_x')}, {result.get('click_y')})")

        # Highlight picked element + click point on clean screenshot
        highlight = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")
        draw = ImageDraw.Draw(highlight)
        w, h = highlight.size

        cx, cy = result.get("click_x", 0), result.get("click_y", 0)
        r = 20
        draw.line([(cx - r, cy), (cx + r, cy)], fill=(255, 0, 0), width=3)
        draw.line([(cx, cy - r), (cx, cy + r)], fill=(255, 0, 0), width=3)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 0, 0), width=2)

        bbox = result.get("bbox")
        if bbox:
            x1 = int(bbox[0] * w)
            y1 = int(bbox[1] * h)
            x2 = int(bbox[2] * w)
            y2 = int(bbox[3] * h)
            for off in range(4):
                draw.rectangle([x1 - off, y1 - off, x2 + off, y2 + off], outline=(0, 255, 0))
            print(f"   bbox=({bbox[0]:.3f}, {bbox[1]:.3f}, {bbox[2]:.3f}, {bbox[3]:.3f})")
        else:
            print("   ⚠ No matching element found")

        buf = io.BytesIO()
        highlight.save(buf, format="PNG")
        out_pick = str(Path(args.input).with_name(f"{stem}_picked.png"))
        Path(out_pick).write_bytes(buf.getvalue())
        print(f"Saved highlight: {out_pick}")


if __name__ == "__main__":
    main()
