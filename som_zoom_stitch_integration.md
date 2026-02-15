# SoM + Zoom + Stitch Integration Guide (Cursor Implementation File)

This file describes how to integrate a **Set-of-Mark (SoM) + Zoom Refinement + Stitch-back** pipeline into the existing overlay agent architecture.

Goal:
- Improve target accuracy for small / ambiguous UI elements
- Keep latency low (1–2 model calls per step)
- Work across *any* app (including custom-rendered UIs like Blender)

---

## 1) High-level Approach

Instead of asking the model to output a full-screen bounding box directly:

1. **Coarse grounding (SoM):**
   - Render the screenshot with numbered markers (or regions) overlaid.
   - Ask the model: *which marker corresponds to the clickable UI element?*
   - Model outputs `marker_id` (integer).

2. **Refinement (optional zoom):**
   - Take a crop around the chosen marker.
   - Ask the model to output a tight bbox **within the crop** (normalized to the crop).
   - Model outputs `{x,y,w,h}` in crop-normalized coordinates.

3. **Stitch back to full-screen normalized coords:**
   - Convert crop bbox → full-image normalized bbox.
   - Render overlay using the stitched bbox.

This avoids fragile “raw coordinate guessing” and gives precision without heavy ML infra.

---

## 2) Data Structures

### 2.1 Marker Definition (client-side)
Markers are generated on the client for each screenshot.

Recommended MVP:
- uniform grid markers (fast)
- later: proposal-based markers (OCR boxes, edge boxes, etc.)

Represent each marker as:
```json
{
  "id": 37,
  "cx": 0.412,
  "cy": 0.281,
  "radius": 0.012,
  "screen_id": "main"
}
```

Where `cx,cy` are normalized point coords in the full screenshot coordinate space (origin top-left, normalized [0,1]).

### 2.2 Crop Definition
When refining, define a crop rectangle around the marker center:

```json
{
  "cx": 0.412,
  "cy": 0.281,
  "cw": 0.20,
  "ch": 0.20
}
```

Where:
- `(cx,cy)` is top-left of the crop in full-image normalized coords
- `(cw,ch)` is width/height of the crop in full-image normalized coords

**Crop sizing rules (MVP):**
- Use a fixed normalized crop size like `cw=ch=0.18` (tune)
- Clamp crop to image bounds [0,1]

---

## 3) Stitching Math (Core)

Given:
- crop rect in full-image normalized coords: `(cx, cy, cw, ch)`
- model returns bbox in crop-normalized coords: `(x, y, w, h)`

Stitched bbox in full-image normalized coords:

```text
x' = cx + x * cw
y' = cy + y * ch
w' = w  * cw
h' = h  * ch
```

All values remain normalized in [0,1] (clamp to bounds).

---

## 4) When to Refine (Latency Control)

Only do zoom refinement if any are true:

- target likely small (heuristic):
  - crop bbox predicted from marker radius would be < ~0.002 of screen area
- model confidence is low
- user miss-clicked twice on this step
- the selected marker is in a dense region (many nearby markers / OCR text clusters)

Otherwise:
- Use a default bbox around the marker (fast path)

### Fast-path bbox (no refine)
Create a bbox around marker center:
```text
w = h = 0.06   (tune)
x = cx_marker - w/2
y = cy_marker - h/2
```
Clamp to [0,1].

---

## 5) Minimal Schema Changes

### 5.1 StepPlan schema additions (non-breaking if you allow optional fields)

Add optional fields to each step:

```json
{
  "targets": [
    {
      "type": "som_marker",
      "marker_id": 37,
      "confidence": 0.78,
      "label": "New event (+)"
    }
  ],
  "refine": {
    "enabled": true,
    "crop_size": 0.18
  }
}
```

And for refined results (runtime-produced OR agent-produced if you do 2 calls server-side):

```json
{
  "targets": [
    {
      "type": "bbox_norm",
      "x": 0.40,
      "y": 0.26,
      "w": 0.05,
      "h": 0.03,
      "confidence": 0.86,
      "label": "New event (+)"
    }
  ]
}
```

**Recommendation for hackathon implementation:**  
- Keep StepPlan outputs from the agent as **marker selection** only.
- Perform refinement with a second call only when needed.
- The client ultimately renders `bbox_norm` targets.

This keeps the agent simpler and makes behavior deterministic.

---

## 6) API Design (Python FastAPI)

### 6.1 POST /plan (SoM selection only)
Input:
- `goal`
- `learning_profile` (optional)
- `image_size`
- `screenshot_with_markers` (the marked-up image)
- `markers_json` (list of markers + ids)
- `app_context`

Output:
- StepPlan where each step returns `som_marker` target(s)

### 6.2 POST /refine (crop bbox)
Input:
- `goal`
- `step_id`
- `instruction`
- `crop_image` (image crop around marker)
- `crop_rect_full_norm` (cx,cy,cw,ch)
- optional `session_summary`

Output:
- A single `bbox_crop_norm` (x,y,w,h) + confidence + label

Client stitches it back using the formula above.

---

## 7) Prompt Templates

### 7.1 SoM Marker Selection Prompt (planner/selector)
**Input:** screenshot WITH numbered markers  
**Output:** choose marker id(s) only

Rules:
- return JSON only
- select the marker that best matches the instruction
- if ambiguous, return top-2 marker_ids (ordered)

Example output:
```json
{ "marker_id": 37, "confidence": 0.78, "label": "New event (+)" }
```

### 7.2 Refinement Prompt (bbox in crop)
**Input:** cropped image (tight region)  
**Output:** bbox in crop-normalized coords

Example output:
```json
{ "x": 0.31, "y": 0.42, "w": 0.22, "h": 0.18, "confidence": 0.86, "label": "New event (+)" }
```

---

## 8) Client Implementation Checklist (Swift)

### 8.1 Generate markers
- Choose a grid (e.g., 24 columns × 14 rows = 336 markers)
- Compute each marker's center normalized
- Draw circles + IDs onto a copy of the screenshot for the agent

### 8.2 Render overlay
- If step target is `som_marker`, render a default bbox around marker (fast path)
- If `refine.enabled` and refinement rule triggers:
  - compute crop rect around marker
  - crop screenshot
  - call `/refine`
  - stitch bbox back
  - replace current step target with `bbox_norm`

### 8.3 Click gating
- Use the stitched bbox as the authoritative clickable region
- If user misses twice:
  - trigger refine (if not already)
  - or trigger replan

---

## 9) Suggested Task Split (4 Eng)

- Eng 1: Overlay rendering supports both marker + bbox targets
- Eng 2: Marker generation + crop + stitching utilities
- Eng 3: FastAPI endpoints `/plan` and `/refine` + prompts
- Eng 4: State machine integration + refinement decision logic + retries

---

## 10) Cursor Implementation Notes (Rules)
- Keep SoM marker rendering and bbox stitching in a dedicated module (e.g., `mac-client/Targeting/`)
- Do not mix:
  - overlay drawing
  - screenshot capture
  - AI networking
  - state machine logic
- Add unit tests for:
  - crop clamping
  - stitch-back correctness
  - coordinate origin consistency

---

## 11) Definition of Done for SoM Integration
- Model no longer outputs full-screen coords in `/plan`
- `/plan` returns marker ids
- Client can render a default bbox around marker
- Optional `/refine` can tighten bbox and improve click gating
- Refinement is triggered only when needed (misses/small target)

---

## 12) Origin Convention (Important)
Standardize everywhere:
- Normalized coordinates
- Origin = TOP-LEFT of the image
- x increases to the right, y increases downward

If macOS screen coords differ, convert once at the boundary between:
- screenshot image space
- screen rendering space
