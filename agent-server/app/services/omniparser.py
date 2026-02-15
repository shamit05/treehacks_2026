# app/services/omniparser.py
# Owner: Eng 3 (Agent Pipeline)
#
# OmniParser integration for detecting UI elements in screenshots.
# Primary: Local YOLO inference using OmniParser v2 weights (fast, reliable).
# Fallback: Gradio client to HuggingFace Space (if local weights unavailable).
# Returns structured element list with bounding boxes and labels,
# plus an annotated screenshot with numbered boxes.

import base64
import io
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


@dataclass
class OmniElement:
    """A single UI element detected by OmniParser."""

    id: int
    type: str  # "text" or "icon"
    content: str  # semantic label or OCR text
    bbox_xyxy: list[float]  # [x1, y1, x2, y2] normalized [0,1]
    interactivity: bool = True

    @property
    def bbox_xywh(self) -> tuple[float, float, float, float]:
        """Convert xyxy to xywh (x, y, width, height) — all normalized."""
        x1, y1, x2, y2 = self.bbox_xyxy
        return (x1, y1, x2 - x1, y2 - y1)


@dataclass
class OmniParserResult:
    """Result from OmniParser: detected elements + annotated image."""

    elements: list[OmniElement] = field(default_factory=list)
    annotated_image_bytes: bytes = b""  # PNG with numbered boxes


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_WEIGHTS_DIR = Path(__file__).parent.parent.parent / "weights" / "icon_detect"
_MODEL_PATH = _WEIGHTS_DIR / "model.pt"


def _describe_location(cx: float, cy: float) -> str:
    """Return a human-readable screen region for a normalized center point."""
    # Vertical position
    if cy < 0.04:
        v = "menu-bar"
    elif cy > 0.92:
        v = "dock"
    elif cy < 0.33:
        v = "top"
    elif cy < 0.66:
        v = "middle"
    else:
        v = "bottom"

    # Horizontal position
    if cx < 0.25:
        h = "left"
    elif cx > 0.75:
        h = "right"
    else:
        h = "center"

    if v in ("menu-bar", "dock"):
        return f"{v} {h}"
    return f"{v}-{h}"


# ---------------------------------------------------------------------------
# Local YOLO detection (primary backend)
# ---------------------------------------------------------------------------

# Lazy-loaded YOLO model singleton
_yolo_model = None


def _get_yolo_model():
    """Load OmniParser YOLO model (lazy singleton)."""
    global _yolo_model
    if _yolo_model is not None:
        return _yolo_model

    if not _MODEL_PATH.exists():
        # Try to download weights from HuggingFace
        print("[omniparser] YOLO weights not found, downloading from HuggingFace...")
        try:
            from huggingface_hub import hf_hub_download

            os.makedirs(_WEIGHTS_DIR, exist_ok=True)
            for fname in ["model.pt", "model.yaml"]:
                hf_hub_download(
                    repo_id="microsoft/OmniParser-v2.0",
                    filename=f"icon_detect/{fname}",
                    local_dir=str(_WEIGHTS_DIR.parent),
                )
            print("[omniparser] YOLO weights downloaded successfully")
        except Exception as e:
            raise RuntimeError(
                f"OmniParser YOLO weights not found at {_MODEL_PATH} "
                f"and auto-download failed: {e}"
            )

    from ultralytics import YOLO

    _yolo_model = YOLO(str(_MODEL_PATH))
    print(f"[omniparser] YOLO model loaded from {_MODEL_PATH}")
    return _yolo_model


# ---------------------------------------------------------------------------
# Light dedup only — no aggressive filtering
# ---------------------------------------------------------------------------
# The two-pass zoom pipeline means the LLM never sees all boxes at once.
# Pass 1 sees the RAW screenshot (no boxes). Pass 2 sees a zoomed crop
# with only the local elements. So we keep ALL elements here.
_DEDUP_IOU_THRESHOLD = 0.85  # Only remove true duplicates (near-identical boxes)


def _compute_iou(a: list[float], b: list[float]) -> float:
    """IoU between two [x1,y1,x2,y2] boxes."""
    ix1 = max(a[0], b[0])
    iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2])
    iy2 = min(a[3], b[3])
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _deduplicate_boxes(elements: list[tuple[float, list[float]]]) -> list[tuple[float, list[float]]]:
    """
    Remove true duplicate boxes (IoU > 0.85). Keep higher-confidence one.
    Elements should be sorted by confidence (highest first).
    """
    kept: list[tuple[float, list[float]]] = []
    for conf, bbox in elements:
        is_dup = False
        for _, kept_bbox in kept:
            if _compute_iou(bbox, kept_bbox) > _DEDUP_IOU_THRESHOLD:
                is_dup = True
                break
        if not is_dup:
            kept.append((conf, bbox))
    return kept


def detect_elements(
    screenshot_bytes: bytes,
    box_threshold: float = 0.05,
    iou_threshold: float = 0.1,
) -> list[OmniElement]:
    """
    Run OmniParser YOLO v2 model on a screenshot.
    Returns ALL detected elements with normalized bboxes.
    Only removes true duplicates (IoU > 0.85). No confidence caps,
    no area filters, no element count limits — the two-pass zoom
    pipeline handles readability by showing boxes only on zoomed crops.
    """
    model = _get_yolo_model()

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(screenshot_bytes)
        tmp_path = tmp.name

    try:
        results = model.predict(
            source=tmp_path,
            conf=box_threshold,
            iou=iou_threshold,
            imgsz=1024,
            verbose=False,
        )

        raw_elements: list[tuple[float, list[float]]] = []
        if results and len(results) > 0:
            boxes = results[0].boxes
            for box in boxes:
                x1, y1, x2, y2 = box.xyxyn[0].tolist()
                conf = box.conf[0].item()
                raw_elements.append((conf, [x1, y1, x2, y2]))

        raw_count = len(raw_elements)

        # Sort by confidence (highest first)
        raw_elements.sort(key=lambda x: x[0], reverse=True)

        # Only remove true duplicates — keep everything else
        raw_elements = _deduplicate_boxes(raw_elements)

        print(f"[omniparser] {raw_count} raw -> {len(raw_elements)} elements (dedup only)")

        elements: list[OmniElement] = []
        for i, (conf, bbox) in enumerate(raw_elements):
            cx = (bbox[0] + bbox[2]) / 2
            cy = (bbox[1] + bbox[3]) / 2
            w_norm = bbox[2] - bbox[0]
            h_norm = bbox[3] - bbox[1]
            loc = _describe_location(cx, cy)

            area = w_norm * h_norm
            if area < 0.001:
                size = "tiny"
            elif area < 0.005:
                size = "small"
            elif area < 0.02:
                size = "medium"
            else:
                size = "large"

            elements.append(OmniElement(
                id=i,
                type="icon",
                content=f"{loc}, {size} element (conf={conf:.2f})",
                bbox_xyxy=bbox,
                interactivity=True,
            ))

        return elements

    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Annotated screenshot drawing
# ---------------------------------------------------------------------------


def draw_numbered_boxes(
    screenshot_bytes: bytes,
    elements: list[OmniElement],
) -> bytes:
    """
    Draw numbered bounding boxes on the screenshot for each detected element.
    Optimized for LLM readability:
      - Thin box outlines (don't obscure content)
      - Large, high-contrast number labels with pill backgrounds
      - Labels placed OUTSIDE the box (above) when possible, to keep
        the element's content visible
      - Consistent bright colors with dark text for maximum contrast
    Returns annotated PNG bytes.
    """
    img = Image.open(io.BytesIO(screenshot_bytes)).convert("RGBA")
    actual_w, actual_h = img.size

    # Transparent overlay for boxes + labels (so we don't paint over text)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Larger font for readability — must survive Gemini's image downscaling.
    # On 3024px image: font=42px. On 1512px: font=24px.
    font_size = max(20, actual_w // 72)
    border_width = max(2, actual_w // 800)

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
    except Exception:
        try:
            font = ImageFont.truetype("/System/Library/Fonts/SFNSMono.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()

    # High-contrast color palette — bright backgrounds with dark text.
    # Using fewer, more distinct colors to reduce visual confusion.
    label_colors = [
        (255, 255, 50),    # yellow
        (50, 255, 50),     # green
        (50, 200, 255),    # cyan
        (255, 150, 50),    # orange
        (255, 100, 255),   # magenta
        (150, 255, 150),   # light green
        (255, 200, 100),   # gold
        (100, 255, 255),   # light cyan
    ]
    # Box outline color: semi-transparent to not obscure content
    box_outline_alpha = 180

    for elem in elements:
        x1 = int(elem.bbox_xyxy[0] * actual_w)
        y1 = int(elem.bbox_xyxy[1] * actual_h)
        x2 = int(elem.bbox_xyxy[2] * actual_w)
        y2 = int(elem.bbox_xyxy[3] * actual_h)

        color = label_colors[elem.id % len(label_colors)]
        outline_color = (*color, box_outline_alpha)

        # Draw thin box outline (semi-transparent — content stays visible)
        draw.rectangle([x1, y1, x2, y2], outline=outline_color, width=border_width)

        # Draw number label as a pill/badge ABOVE the box
        label = str(elem.id)
        label_bbox = draw.textbbox((0, 0), label, font=font)
        lw = label_bbox[2] - label_bbox[0] + 14
        lh = label_bbox[3] - label_bbox[1] + 8

        # Try placing above the box; if too close to top edge, place inside
        if y1 - lh - 2 >= 0:
            lx = max(0, min(x1, actual_w - lw))
            ly = y1 - lh - 2
        else:
            lx = max(0, min(x1, actual_w - lw))
            ly = max(0, y1 + 2)

        # Pill background (bright, opaque) with dark text
        draw.rounded_rectangle(
            [lx, ly, lx + lw, ly + lh],
            radius=4,
            fill=(*color, 230),
        )
        draw.text((lx + 7, ly + 4), label, fill=(0, 0, 0, 255), font=font)

    # Composite overlay onto the screenshot
    result = Image.alpha_composite(img, overlay).convert("RGB")

    buf = io.BytesIO()
    result.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def parse_screenshot(
    screenshot_bytes: bytes,
    box_threshold: float = 0.05,
    iou_threshold: float = 0.1,
    request_id: str = "",
) -> OmniParserResult:
    """
    Detect UI elements in a screenshot using OmniParser YOLO v2 model.
    Draws numbered bounding boxes on the screenshot for LLM consumption.

    Primary: local YOLO inference (fast, reliable).
    Fallback: HuggingFace Space via gradio_client.
    """
    print(f"[omniparser] rid={request_id} detecting UI elements...")

    # Try local YOLO detection
    try:
        elements = detect_elements(
            screenshot_bytes,
            box_threshold=box_threshold,
            iou_threshold=iou_threshold,
        )
        print(f"[omniparser] rid={request_id} local YOLO detected {len(elements)} elements")

    except Exception as e:
        print(f"[omniparser] rid={request_id} local YOLO failed: {type(e).__name__}: {e}")
        # Try HuggingFace Space as fallback
        try:
            elements = await _parse_via_gradio(screenshot_bytes, box_threshold, iou_threshold, request_id)
            print(f"[omniparser] rid={request_id} HF Space detected {len(elements)} elements")
        except Exception as e2:
            print(f"[omniparser] rid={request_id} HF Space also failed: {type(e2).__name__}: {e2}")
            raise RuntimeError(
                f"OmniParser detection failed. Local YOLO: {e}. HF Space: {e2}"
            )

    if not elements:
        print(f"[omniparser] rid={request_id} WARNING: no elements detected")

    # Draw numbered boxes on the screenshot
    annotated_bytes = draw_numbered_boxes(screenshot_bytes, elements)

    # Save annotated image for debugging
    try:
        with open("/tmp/overlayguide_omniparser_annotated.png", "wb") as f:
            f.write(annotated_bytes)
    except Exception:
        pass

    return OmniParserResult(
        elements=elements,
        annotated_image_bytes=annotated_bytes,
    )


# ---------------------------------------------------------------------------
# HuggingFace Space fallback
# ---------------------------------------------------------------------------

# Patterns for parsing OmniParser text output
_BOX_PATTERN = re.compile(
    r"(?:(?:Text|Icon)\s+)?Box\s*(?:ID\s*)?(\d+):\s*(.+)",
    re.IGNORECASE,
)


async def _parse_via_gradio(
    screenshot_bytes: bytes,
    box_threshold: float,
    iou_threshold: float,
    request_id: str,
) -> list[OmniElement]:
    """Fallback: call OmniParser via HuggingFace Space Gradio API."""
    from gradio_client import Client, handle_file

    omniparser_url = os.getenv("OMNIPARSER_URL", "microsoft/OmniParser-v2")
    print(f"[omniparser] rid={request_id} trying HF Space: {omniparser_url}")

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(screenshot_bytes)
        tmp_path = tmp.name

    try:
        client = Client(omniparser_url)
        result = client.predict(
            handle_file(tmp_path),
            box_threshold,
            iou_threshold,
            api_name="/process",
        )

        # Parse response
        label_coords = {}
        parsed_text = ""

        if isinstance(result, (list, tuple)):
            if len(result) >= 2:
                parsed_text = result[1] if isinstance(result[1], str) else ""
            if len(result) >= 3 and isinstance(result[2], dict):
                label_coords = result[2]

        # Build elements from label_coordinates
        elements: list[OmniElement] = []
        if label_coords:
            for key, coords in label_coords.items():
                eid = int(key)
                if len(coords) == 4:
                    cx, cy, w, h = coords
                    bbox = [
                        max(0.0, cx - w / 2),
                        max(0.0, cy - h / 2),
                        min(1.0, cx + w / 2),
                        min(1.0, cy + h / 2),
                    ]
                else:
                    bbox = coords[:4]

                # Try to find label from text output
                content = f"element_{eid}"
                elem_type = "icon"
                for line in parsed_text.strip().split("\n"):
                    match = _BOX_PATTERN.match(line.strip())
                    if match and int(match.group(1)) == eid:
                        content = match.group(2).strip()
                        if line.lower().startswith("text"):
                            elem_type = "text"
                        break

                elements.append(OmniElement(
                    id=eid,
                    type=elem_type,
                    content=content,
                    bbox_xyxy=bbox,
                    interactivity=True,
                ))

        return elements

    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Snap Gemini box_2d to nearest YOLO element
# ---------------------------------------------------------------------------


def snap_to_nearest_element(
    gemini_x: float, gemini_y: float, gemini_w: float, gemini_h: float,
    elements: list[OmniElement],
    max_distance: float = 0.06,
) -> tuple[float, float, float, float, int | None]:
    """
    Find the YOLO element that best matches Gemini's bounding box.

    Three-pass strategy optimized for dropdown menus and stacked items:
      1. Center containment: which YOLO element's bbox contains the center
         of Gemini's box? Pick the smallest (tightest fit). This is the most
         precise for dropdown menus where items are stacked vertically.
      2. IoU: best for overlapping boxes of similar size.
      3. Center-distance: fallback for near-miss cases.

    Returns (x, y, w, h, matched_element_id).
    """
    if not elements:
        return gemini_x, gemini_y, gemini_w, gemini_h, None

    gcx = gemini_x + gemini_w / 2
    gcy = gemini_y + gemini_h / 2
    gx1 = gemini_x
    gy1 = gemini_y
    gx2 = gemini_x + gemini_w
    gy2 = gemini_y + gemini_h
    g_area = gemini_w * gemini_h

    # Size threshold: don't snap to elements that are much smaller than Gemini's box.
    # This prevents matching random tiny icons when the target is a menu item or button.
    gemini_area = g_area if g_area > 0 else 0.0001
    min_area_ratio = 0.15  # YOLO element must be at least 15% the area of Gemini's box

    # --- Pass 1: Center containment (best for dropdown menus) ---
    # Find all YOLO elements whose bbox contains the CENTER of Gemini's box.
    # Among matches, pick the smallest (most specific) element that isn't too tiny.
    containment_matches = []
    for elem in elements:
        ex, ey, ew, eh = elem.bbox_xywh
        e_area = ew * eh
        if (ex <= gcx <= ex + ew) and (ey <= gcy <= ey + eh):
            # Skip elements that are much smaller than Gemini's box
            if e_area / gemini_area >= min_area_ratio:
                containment_matches.append((e_area, elem))

    if containment_matches:
        # Pick smallest containing element (tightest fit around the center)
        containment_matches.sort(key=lambda x: x[0])
        best = containment_matches[0][1]
        ex, ey, ew, eh = best.bbox_xywh
        print(f"[snap] center-containment -> elem[{best.id}] ({ex:.3f},{ey:.3f},{ew:.3f},{eh:.3f}) [{len(containment_matches)} candidates]")
        return ex, ey, ew, eh, best.id

    # --- Pass 2: IoU-based matching (skip tiny elements) ---
    best_iou_elem = None
    best_iou = 0.0

    for elem in elements:
        ex, ey, ew, eh = elem.bbox_xywh
        e_area = ew * eh
        # Skip elements much smaller than Gemini's box
        if e_area / gemini_area < min_area_ratio:
            continue
        ex1, ey1 = ex, ey
        ex2, ey2 = ex + ew, ey + eh

        ix1 = max(gx1, ex1)
        iy1 = max(gy1, ey1)
        ix2 = min(gx2, ex2)
        iy2 = min(gy2, ey2)

        if ix2 > ix1 and iy2 > iy1:
            inter_area = (ix2 - ix1) * (iy2 - iy1)
            union_area = g_area + e_area - inter_area
            iou = inter_area / union_area if union_area > 0 else 0.0
            if iou > best_iou:
                best_iou = iou
                best_iou_elem = elem

    if best_iou_elem is not None and best_iou > 0.05:
        ex, ey, ew, eh = best_iou_elem.bbox_xywh
        print(f"[snap] IoU={best_iou:.2f} -> elem[{best_iou_elem.id}] ({ex:.3f},{ey:.3f},{ew:.3f},{eh:.3f})")
        return ex, ey, ew, eh, best_iou_elem.id

    # --- Pass 3: Center-distance fallback (skip tiny elements) ---
    best_elem = None
    best_dist = float("inf")

    for elem in elements:
        ex, ey, ew, eh = elem.bbox_xywh
        e_area = ew * eh
        if e_area / gemini_area < min_area_ratio:
            continue
        ecx = ex + ew / 2
        ecy = ey + eh / 2
        dist = ((gcx - ecx) ** 2 + (gcy - ecy) ** 2) ** 0.5
        if dist < best_dist:
            best_dist = dist
            best_elem = elem

    if best_elem is not None and best_dist <= max_distance:
        ex, ey, ew, eh = best_elem.bbox_xywh
        print(f"[snap] center-dist={best_dist:.4f} -> elem[{best_elem.id}] ({ex:.3f},{ey:.3f},{ew:.3f},{eh:.3f})")
        return ex, ey, ew, eh, best_elem.id

    # --- No suitable YOLO match: use Gemini's raw coordinates ---
    # This is the right call for dropdown menus where YOLO doesn't detect
    # the menu items (only tiny icons within them).
    print(f"[snap] NO SNAP — using Gemini raw box ({gemini_x:.3f},{gemini_y:.3f},{gemini_w:.3f},{gemini_h:.3f})")
    return gemini_x, gemini_y, gemini_w, gemini_h, None


# ---------------------------------------------------------------------------
# Element context formatting
# ---------------------------------------------------------------------------


def format_elements_context(elements: list[OmniElement], max_elements: int = 120) -> str:
    """
    Format OmniParser elements into a text context string for the LLM prompt.
    Each element is listed with its ID, type, content, bbox, and nearby neighbors.
    The ID matches the numbered box drawn on the annotated screenshot.

    Neighbor info helps the LLM cross-reference: "Box 12 is right of Box 11
    and below Box 5" lets it verify it's looking at the right box.

    Caps at max_elements to keep the prompt within reasonable token limits.
    Elements are drawn on the image regardless, so the LLM can still see them.
    """
    if not elements:
        return "(no elements detected)"

    # If there are too many elements, keep them all but add a note
    display_elements = elements
    truncated = False
    if len(elements) > max_elements:
        display_elements = elements[:max_elements]
        truncated = True

    # Pre-compute centers for neighbor lookup (use ALL elements for neighbor calculation)
    centers = {}
    for e in elements:
        x, y, w, h = e.bbox_xywh
        centers[e.id] = (x + w / 2, y + h / 2)

    lines = []
    for e in display_elements:
        x, y, w, h = e.bbox_xywh
        cx, cy = centers[e.id]

        # Find closest neighbors in each direction (for disambiguation)
        neighbors = []
        best_left = (None, float("inf"))
        best_right = (None, float("inf"))
        best_above = (None, float("inf"))
        best_below = (None, float("inf"))

        for other in elements:
            if other.id == e.id:
                continue
            ocx, ocy = centers[other.id]
            dx = ocx - cx
            dy = ocy - cy
            dist = (dx ** 2 + dy ** 2) ** 0.5

            # Must be relatively close (within 15% of screen) to be a useful neighbor
            if dist > 0.15:
                continue

            # Classify direction (must be clearly in one direction)
            if dx < -0.02 and abs(dy) < abs(dx) * 0.8:
                if dist < best_left[1]:
                    best_left = (other.id, dist)
            elif dx > 0.02 and abs(dy) < abs(dx) * 0.8:
                if dist < best_right[1]:
                    best_right = (other.id, dist)
            elif dy < -0.02 and abs(dx) < abs(dy) * 0.8:
                if dist < best_above[1]:
                    best_above = (other.id, dist)
            elif dy > 0.02 and abs(dx) < abs(dy) * 0.8:
                if dist < best_below[1]:
                    best_below = (other.id, dist)

        neighbor_parts = []
        if best_left[0] is not None:
            neighbor_parts.append(f"left=Box{best_left[0]}")
        if best_right[0] is not None:
            neighbor_parts.append(f"right=Box{best_right[0]}")
        if best_above[0] is not None:
            neighbor_parts.append(f"above=Box{best_above[0]}")
        if best_below[0] is not None:
            neighbor_parts.append(f"below=Box{best_below[0]}")
        neighbor_str = f" neighbors({', '.join(neighbor_parts)})" if neighbor_parts else ""

        lines.append(
            f"  [Box {e.id}] \"{e.content}\" "
            f"— bbox(x={x:.3f}, y={y:.3f}, w={w:.3f}, h={h:.3f}){neighbor_str}"
        )

    if truncated:
        lines.append(f"\n  (Showing {max_elements} of {len(elements)} detected elements. "
                      f"All {len(elements)} are numbered in the screenshot image.)")

    return "\n".join(lines)
