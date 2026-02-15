import cv2
import numpy as np

INPUT_PATH = "/Users/shreyas/Desktop/test.png"
OUTPUT_PATH = "/Users/shreyas/Desktop/test_output.png"

SCALE = 0.5       # Change to 0.3 for even faster
MIN_AREA = 500    # Adjust if too many tiny boxes


def detect_boxes_fast(image_path):
    original = cv2.imread(image_path)
    if original is None:
        raise ValueError("Could not load image at path:", image_path)

    orig_h, orig_w = original.shape[:2]

    # Resize down
    small = cv2.resize(
        original,
        (int(orig_w * SCALE), int(orig_h * SCALE)),
        interpolation=cv2.INTER_AREA
    )

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

    # Fast edge detection
    edges = cv2.Canny(gray, 50, 150)

    contours, _ = cv2.findContours(
        edges,
        cv2.RETR_TREE,
        cv2.CHAIN_APPROX_SIMPLE
    )

    boxes = []

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < MIN_AREA:
            continue

        x, y, w, h = cv2.boundingRect(cnt)

        # Scale box back up
        x = int(x / SCALE)
        y = int(y / SCALE)
        w = int(w / SCALE)
        h = int(h / SCALE)

        boxes.append((x, y, w, h))

    # Draw on original image
    for (x, y, w, h) in boxes:
        cv2.rectangle(original, (x, y), (x + w, y + h), (0, 255, 0), 2)

    return original, boxes


if __name__ == "__main__":
    result, boxes = detect_boxes_fast(INPUT_PATH)
    cv2.imwrite(OUTPUT_PATH, result)

    print(f"Detected {len(boxes)} boxes")
    print("Saved output to:", OUTPUT_PATH)
