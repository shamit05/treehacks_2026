# Agentic Overlay Guide — Hackathon Architecture & Implementation Spec

## Overview
This project is a macOS system-wide AI guidance overlay that:
- launches via global hotkey
- accepts a natural language task
- analyzes the current screen
- generates step-by-step guidance
- overlays visual instructions on top of any app
- advances steps automatically when user clicks correct UI regions

This document defines:
- architecture
- tech stack
- module boundaries
- data schemas
- development roadmap
- team task splits
- repo structure
- Cursor rules

Goal: allow 4 engineers to work in parallel with minimal conflicts.

---

## 1. Core Design Principles

---

## Personalization Layer (Learning Style)

### Goal
Support different learning preferences (e.g., concise vs detailed, step-by-step vs conceptual, visual-heavy vs text-heavy) by collecting a short onboarding answer and injecting it into the agent’s system context.

### UX (MVP)
On first launch (or via Settings), ask:
- **“How do you learn best?”** (free text)
Optional quick-picks:
- “Show me minimal steps”
- “Explain why each step matters”
- “Use more visuals and callouts”
- “Let me try first, then correct me”
- “Be very explicit and slow”

Store result locally (non-sensitive, user-editable):
- `learning_profile.text` (string)
- `learning_profile.presets` (optional)

### How it affects guidance
At plan time, include `learning_profile` in the request to the agent.
Agent must adjust:
- instruction wording length
- whether to include brief rationale
- whether to include “checkpoints” (manual_next)
- whether to suggest “practice mode” (user attempts first)

**Important:** personalization changes the *style* of the plan, not the schema.



### Must Have
- Native macOS overlay reliability
- Deterministic state machine
- Structured AI output (never free-form)
- Clear module boundaries
- Fast iteration

### Must Avoid
- Tight coupling between UI + agent
- Streaming capture complexity (for MVP)
- Overengineering agent memory
- Cross-platform abstraction too early

---

## 2. Recommended Tech Stack

### Frontend (macOS overlay)
Language: **Swift**

Frameworks:
- SwiftUI → UI layout
- AppKit → overlay window control

Why native?
- Fullscreen support
- All Spaces support
- Input passthrough
- Reliable z-index stacking
- Proper permissions

### Screenshot Capture
Preferred: `ScreenCaptureKit`  
Fallback: `CGDisplayCreateImage`

### Input Detection
- Global hotkey → Carbon API (or KeyboardShortcuts package)
- Mouse click detection → CGEventTap

### Agent Backend
Language: **Python (FastAPI)**

Responsibilities:
- receive screenshot + prompt
- generate step plan JSON
- validate schema
- return structured instructions

### AI Model
For hackathon:
- remote multimodal model API

Later:
- local VLM
- CoreML or ONNX

---

## 3. System Architecture

```text
User presses hotkey
    ↓
Overlay UI appears
    ↓
User types goal
    ↓
Screenshot captured
    ↓
Request → Agent Server (/plan)
    ↓
Agent returns Step Plan JSON
    ↓
Overlay renders Step 1 highlight
    ↓
User clicks target
    ↓
Event tap detects click
    ↓
Advance step (optionally recapture → replan)
```

---

## 4. Module Architecture

### 4.1 Overlay Controller (Swift)
Responsible for:
- rendering overlay windows
- drawing highlights
- text prompts
- step instruction display

Must not:
- call AI
- manage state logic

### 4.2 Capture Service (Swift)
Responsibilities:
- capture active display (or all displays)
- return screenshot image
- return screen bounds
- return scaling factor / coordinate metadata

### 4.3 Input Monitor (Swift)
Responsibilities:
- detect global hotkey
- detect mouse click location
- notify state machine

### 4.4 State Machine (Swift or Backend)
Tracks:
- goal
- step index
- plan
- screenshot history

Controls:
- step progression
- retries
- completion

### 4.5 Agent Service (Python / FastAPI)
Endpoints:
- `POST /plan` → goal + screenshot + context → StepPlan JSON
- `POST /replan` (later) → goal + currentStep + screenshot → revised StepPlan JSON

---

## 5. Step Plan Schema (Strict)
Agent must always output valid JSON matching `shared/step_plan_schema.json`.

**Coordinates are normalized** in `[0,1]` relative to the screenshot width/height.

Minimum fields:
- `goal`
- `version`
- `image_size`
- `steps[]` with:
  - `id`
  - `instruction`
  - `targets[]` (at least one)
  - `advance.type`

No pixel coordinates, no extra commentary.

---

## 6. Overlay Rendering Rules

Each step draws:
- translucent dark background (optional; can be per-step)
- highlighted rectangle(s)
- instruction bubble near target
- progress indicator (Step i / N)

Window settings:
- borderless
- transparent background
- always on top
- non-activating (doesn’t steal focus)
- appears on all Spaces and over full-screen apps

Implementation notes:
- Create **one overlay window per display** via `NSScreen.screens`
- Use:
  - `.canJoinAllSpaces`
  - `.fullScreenAuxiliary`
- Consider toggling click-through using `ignoresMouseEvents`

---

## 7. Click Detection Logic (MVP)

On global mouse click:
1) Convert click location to screen coordinate space
2) For current step:
   - convert each normalized target rect → screen pixels
   - if click is inside any target rect → advance
   - else show a “Try clicking the highlighted area” hint

Rect conversion (per display):
- `pixelX = rect.x * screenWidth`
- `pixelY = rect.y * screenHeight`

Be consistent about origin:
- macOS screen coordinates are typically bottom-left; screenshots may be top-left origin.
- Normalize your coordinate conventions once and document it in code.

---

## 8. Screenshot Policy (MVP)

Capture screenshot when:
- plan generated
- step advances
- user presses refresh / replan

Do **not** capture continuously for MVP.

---

## 9. Context Strategy

### Hackathon Mode
Store:
- `goal`
- `current_step_index`
- `step_plan`

### Later: Long Tasks
Add:
- last N screenshots (or cropped regions)
- action history (clicks + step transitions)
- extracted UI labels (OCR)
- lightweight “task graph” representation (nodes = steps)

---
---

## Context & Memory Management (Strong Requirement)

This product needs reliable context handling for tasks that span multiple steps, UI changes, and potential replans. Treat this as a core system feature, not an afterthought.

### 1) Session Memory (MVP)
Maintain a `SessionState` object (in the mac client state machine) containing:
- `session_id` (uuid)
- `goal` (string)
- `learning_profile` (string)
- `app_context` (frontmost app, bundle id, window title)
- `step_plan` (current plan)
- `current_step_index`
- `events` (bounded list: clicks, step transitions, misses)
- `artifacts` (bounded list of screenshots/crops with hashes + timestamps)

Hard limits (hackathon-safe):
- keep last **3** screenshots (or **6** target crops)
- keep last **50** events

### 2) Evidence-Based Replanning
When user is stuck (e.g., 3+ misses on a step) or UI likely changed:
- capture a fresh screenshot
- send `/replan` with:
  - goal
  - current step id
  - learning_profile
  - minimal session summary (last 5 events)
  - optional target crop(s) from previous step

The agent should:
- reuse prior steps when still valid
- adjust only where mismatch occurred
- keep plan short (≤ 6 steps)

### 3) Long-Task Memory (Post-hackathon)
Introduce a lightweight **Task Graph**:
- nodes: steps with targets + instructions + evidence
- edges: dependencies / ordering
Store per step:
- instruction text
- target rect(s)
- success criteria (click_in_target, manual_next, etc.)
- evidence references (screenshot crop IDs, OCR snippets)

### 4) Summarization + Retrieval (Post-hackathon)
For very long sessions, periodically summarize into:
- `task_summary`: 5–10 bullets
- `progress_summary`: current step + what’s been completed
- `user_preference_summary`: learning style constraints

Use embeddings **only for text** (not raw images):
- extracted labels (OCR)
- instructions
- summaries
Keep images as referenced artifacts by ID.

### 5) Privacy & Persistence
- Session memory should default to **ephemeral** (cleared on app quit).
- Optional: “Remember my learning style” is persisted locally.
- Optional later: “Remember my common workflows” requires explicit opt-in and a visible history UI.



## 10. Agent Prompt Strategy

Hard requirements:
- return JSON only
- max 6 steps
- use normalized coordinates
- include `image_size` exactly as received
- avoid targeting ambiguous regions (prefer buttons/fields)

Fallback strategy:
- If unsure, return fewer steps and ask for user clarification as a final step.

---

## 11. Feature Roadmap

### Phase 0 (Hackathon MVP)
- hotkey launch
- text input
- screenshot capture
- `/plan` returns 3–5 step plan
- overlay highlights step 1
- click-in-rect advances
- completion screen

### Phase 1
- re-capture each step
- “Back / Skip / Refresh”
- stuck detection (too many misses → replan)
- basic frontmost app detection (bundle id)

### Phase 2
- web search tool for “best way to do X” (agentic)
- per-app cached flows
- OCR extraction for better grounding

### Phase 3
- Accessibility API grounding (AXUIElement)
- prefer element-level targeting over raw vision

### Phase 4
- action execution (calendar/email) with confirmation + audit log
- voice input

---

## 12. Engineering Task Split (4 Eng)

### Engineer 1 — Overlay UI (SwiftUI + AppKit)
- NSPanel windows (per display)
- highlight rendering
- instruction bubble + step indicator
- simple animations and transitions

### Engineer 2 — Capture + Input (Swift)
- screenshot capture
- scaling / coordinate mapping helpers
- CGEventTap for click detection
- global hotkey registration

### Engineer 3 — Agent Server (Python / FastAPI)
- `/plan` endpoint
- prompt + schema validation
- mocked response mode (for demos)
- logging + request IDs

### Engineer 4 — State + Integration
- state machine
- networking client
- step progression rules
- retries / error UI hooks

---

## 13. Repo Structure

```text
repo/
  mac-client/
    Overlay/
    Capture/
    Input/
    State/
    UI/
    Resources/
  agent-server/
    app/
      main.py
      routers/
      schemas/
      prompts/
      services/
    tests/
  shared/
    step_plan_schema.json
    example_step_plan.json
  docs/
    spec.md
    cursor.md
```

---

## 14. Error Handling Strategy

| Failure | UX | Action |
|---|---|---|
| Screen capture permission missing | show permission dialog instructions | stop |
| Agent timeout | show “Retry” | retry with backoff |
| Invalid JSON | show “Replan” | retry once then fallback mock |
| Click outside targets | subtle hint | keep same step |
| Targets clearly wrong | “Refresh / Replan” button | recapture + replan |

---

## 15. Permissions Needed
- Screen Recording (for screenshots / ScreenCaptureKit)
- Accessibility (later, for AX UI grounding and interaction)

Explain why you need it before prompting.

---

## 16. Demo Strategy
Pick **one workflow** and make it flawless.
Examples:
- create a calendar event (Google Calendar web)
- export a PDF (Preview)
- share a file (Finder)

General intelligence comes later.

---

## 17. Performance Targets (MVP)
- Overlay open: < 100ms
- Screenshot: < 150ms
- Agent response: < 2s (demo acceptable)
- Step advance: instant

---

## 18. Security Design
Never allow the agent to:
- run arbitrary OS commands
- send emails or create events without explicit confirmation
- access files without the user choosing them

All “actions” go through a confirmation layer.

---

## 19. Cursor.md Rules (for codegen)

**Hard rules:**
1. Never mix UI rendering and networking in the same file.
2. All AI outputs must validate against the JSON schema.
3. Overlay rendering must be stateless; state lives in the state machine.
4. Use normalized coordinates only.
5. Every async call has a timeout and retry policy.
6. No hardcoded screen sizes; always read `NSScreen` info.
7. New features must be added as new modules; avoid god-classes.
8. Permission checks must happen before capture or input taps.
9. Log request IDs across client↔server for debugging.
10. Provide a mock plan mode for demo reliability.

---

## 20. MVP Definition of Done
- Hotkey launches overlay
- User types request
- Screenshot captured
- Agent returns valid step plan JSON
- Overlay shows guidance
- Clicking highlighted region advances steps
- Completion screen displays success

---

## 21. Key Insight
This is not primarily an AI problem. It is:
- overlay/windowing
- state machine correctness
- schema discipline
- UX clarity

If the architecture is clean, the AI can be swapped later.
