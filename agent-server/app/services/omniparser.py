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


def detect_elements(
    screenshot_bytes: bytes,
    box_threshold: float = 0.05,
    iou_threshold: float = 0.1,
) -> list[OmniElement]:
    """
    Run OmniParser YOLO v2 model on a screenshot.
    Returns all detected elements with normalized bboxes, sorted by
    confidence (highest first) so the LLM sees the best candidates first.
    """
    model = _get_yolo_model()

    # Save to temp file (YOLO expects a path or array)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(screenshot_bytes)
        tmp_path = tmp.name

    try:
        # Use 1024px for better detection of small UI elements on Retina displays.
        # Default 640px loses too many menu items, small buttons, and icons.
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

        # Sort by confidence (highest first)
        raw_elements.sort(key=lambda x: x[0], reverse=True)

        elements: list[OmniElement] = []
        for i, (conf, bbox) in enumerate(raw_elements):
            # Describe location and size to help the LLM match visually
            cx = (bbox[0] + bbox[2]) / 2
            cy = (bbox[1] + bbox[3]) / 2
            w_norm = bbox[2] - bbox[0]
            h_norm = bbox[3] - bbox[1]
            loc = _describe_location(cx, cy)

            # Classify element size to help LLM distinguish buttons from icons etc.
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
    Returns annotated PNG bytes.
    """
    img = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")
    actual_w, actual_h = img.size
    draw = ImageDraw.Draw(img)

    # Medium font — readable after OpenAI downscaling but not overwhelming.
    # On 3024px image: font=34px, border=4px.
    font_size = max(18, actual_w // 90)
    border_width = max(3, actual_w // 600)

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
    except Exception:
        try:
            font = ImageFont.truetype("/System/Library/Fonts/SFNSMono.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()

    # Color palette for variety
    colors = [
        (255, 50, 50),    # red
        (50, 150, 255),   # blue
        (50, 200, 50),    # green
        (255, 165, 0),    # orange
        (200, 50, 200),   # purple
        (0, 200, 200),    # cyan
    ]

    for elem in elements:
        x1 = int(elem.bbox_xyxy[0] * actual_w)
        y1 = int(elem.bbox_xyxy[1] * actual_h)
        x2 = int(elem.bbox_xyxy[2] * actual_w)
        y2 = int(elem.bbox_xyxy[3] * actual_h)

        color = colors[elem.id % len(colors)]

        # Draw bounding box
        draw.rectangle([x1, y1, x2, y2], outline=color, width=border_width)

        # Draw number label INSIDE the box at top-left corner.
        # Previously drawn ABOVE the box, which caused the label for element N
        # to overlap element N-1 in stacked layouts (menus, lists), making the
        # LLM pick the wrong element (off-by-one).
        label = str(elem.id)
        label_bbox = draw.textbbox((0, 0), label, font=font)
        lw = label_bbox[2] - label_bbox[0] + 10
        lh = label_bbox[3] - label_bbox[1] + 6

        # Place label at the top-left corner of the bbox, inside the box
        lx = max(0, x1)
        ly = max(0, y1)

        draw.rectangle([lx, ly, lx + lw, ly + lh], fill=color)
        draw.text((lx + 5, ly + 3), label, fill=(255, 255, 255), font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
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


def format_elements_context(elements: list[OmniElement]) -> str:
    """
    Format OmniParser elements into a text context string for the LLM prompt.
    Each element is listed with its ID, type, content, and bbox.
    The ID matches the numbered box drawn on the annotated screenshot.
    """
    if not elements:
        return "(no elements detected)"

    lines = []
    for e in elements:
        x, y, w, h = e.bbox_xywh
        lines.append(
            f"  [Box {e.id}] \"{e.content}\" "
            f"— bbox(x={x:.3f}, y={y:.3f}, w={w:.3f}, h={h:.3f})"
        )
    return "\n".join(lines)
