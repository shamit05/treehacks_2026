"""
Standalone CV2 bounding box detection utility.
Detects rectangular UI element regions in a screenshot using edge detection.
Useful for quick visual debugging — not used in the main pipeline.

Usage:
    python -m app.scripts.bbox --input /path/to/screenshot.png --output /path/to/output.png
"""

import argparse

import cv2
import numpy as np

SCALE = 0.5       # Resize factor for faster processing
MIN_AREA = 500    # Minimum contour area (filters tiny noise)


def detect_boxes_fast(image_path: str) -> tuple:
    """
    Detect bounding boxes in a screenshot using Canny edge detection.
    Returns (annotated_image, boxes) where boxes is a list of (x, y, w, h).
    """
    original = cv2.imread(image_path)
    if original is None:
        raise ValueError(f"Could not load image at path: {image_path}")

    orig_h, orig_w = original.shape[:2]

    # Resize down for speed
    small = cv2.resize(
        original,
        (int(orig_w * SCALE), int(orig_h * SCALE)),
        interpolation=cv2.INTER_AREA,
    )

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

    # Fast edge detection
    edges = cv2.Canny(gray, 50, 150)

    contours, _ = cv2.findContours(
        edges,
        cv2.RETR_TREE,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    boxes = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < MIN_AREA:
            continue

        x, y, w, h = cv2.boundingRect(cnt)

        # Scale box back up to original resolution
        x = int(x / SCALE)
        y = int(y / SCALE)
        w = int(w / SCALE)
        h = int(h / SCALE)

        boxes.append((x, y, w, h))

    # Draw on original image
    for (x, y, w, h) in boxes:
        cv2.rectangle(original, (x, y), (x + w, y + h), (0, 255, 0), 2)

    return original, boxes


def main():
    parser = argparse.ArgumentParser(description="Detect bounding boxes in a screenshot")
    parser.add_argument("--input", "-i", required=True, help="Path to input screenshot")
    parser.add_argument("--output", "-o", required=True, help="Path to save annotated output")
    args = parser.parse_args()

    result, boxes = detect_boxes_fast(args.input)
    cv2.imwrite(args.output, result)

    print(f"Detected {len(boxes)} boxes")
    print(f"Saved output to: {args.output}")


if __name__ == "__main__":
    main()
