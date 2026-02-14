# Eng 3 — Agent Pipeline (Full Stack)

## What You Own

You own the **entire AI pipeline end-to-end**: the Python server that talks to the AI model, the Swift networking client that talks to the server, the shared JSON schema, and the prompt engineering. You are the person who makes the AI work and makes the client-server communication reliable.

### Your Directories

```
agent-server/                       # Python FastAPI backend (you own ALL of this)
  app/
    main.py                         # App entry, CORS, router registration
    routers/plan.py                 # POST /plan endpoint
    schemas/step_plan.py            # Pydantic models (must match JSON schema)
    prompts/plan_prompt.txt         # Agent prompt template
    services/agent.py               # AI model integration (OpenAI vision)
    services/mock.py                # Mock plan for demos
  requirements.txt
  .env.example

mac-client/OverlayGuide/
  Networking/                       # Swift HTTP client (you own this too)
    AgentNetworkClient.swift        # Multipart POST /plan, request IDs, retries

shared/                             # Cross-platform schema (you own this)
  step_plan_schema.json             # Source of truth JSON schema
  example_step_plan.json            # Example valid plan
```

### Your Commit Prefix

`[agent]` — e.g. `[agent] improve prompt for tighter target rects`

---

## What You Need to Build (MVP)

### 1. Agent Server — `POST /plan` (`agent-server/`)

**Request:** multipart form with:
- `goal` (string) — what the user wants to do
- `image_size` (JSON string) — `{"w": 1920, "h": 1080}`
- `screenshot` (file) — PNG image of the screen
- `learning_profile` (string, optional) — user's learning preference
- `app_context` (JSON string, optional) — `{"app_name": "...", "bundle_id": "..."}`
- `session_summary` (string, optional) — recent events summary for replanning
- `X-Request-ID` header — UUID for request tracing

**Response:** JSON matching `shared/step_plan_schema.json` exactly:
```json
{
  "version": "v1",
  "goal": "...",
  "image_size": {"w": 1920, "h": 1080},
  "steps": [
    {
      "id": "s1",
      "instruction": "Click the File menu",
      "targets": [{"x": 0.02, "y": 0.0, "w": 0.04, "h": 0.025, "confidence": 0.9, "label": "File menu"}],
      "advance": {"type": "click_in_target"}
    }
  ]
}
```

**Validation:** Every response must pass Pydantic validation against `StepPlan`. If the AI model returns invalid JSON, retry once. If still invalid, return a 500 with a clear error.

### 2. AI Integration (`services/agent.py`)

- Send the screenshot as a base64-encoded image to a multimodal model (GPT-4o or similar)
- Use the prompt template from `prompts/plan_prompt.txt`
- Parse the response, strip any markdown fences, validate with Pydantic
- Log the request ID at every step for debugging

**Prompt tuning is your main job after initial wiring.** The quality of the target rectangles and instructions determines the whole product. Iterate on the prompt to get:
- Tight bounding boxes around the actual UI elements
- Clear, actionable instructions
- Correct advance types (click vs text entry vs manual)
- 3-5 steps (not too many, not too few)

### 3. Mock Mode (`services/mock.py`)

When `MOCK_MODE=true`:
- `POST /plan` returns a hardcoded valid plan (no API key needed)
- `GET /health` returns `{"status": "ok", "mock_mode": true}`

This is critical for demo reliability. If the AI API goes down during the demo, flip to mock mode.

### 4. Swift Networking Client (`Networking/AgentNetworkClient.swift`)

This is the Swift-side HTTP client that Eng 4's state machine calls. You own it because you define the API contract.

**Key behavior:**
- Builds a multipart form request with goal, image_size, screenshot PNG, optional fields
- Sends `X-Request-ID` header (UUID)
- Timeout: 10 seconds
- Retry: 1 retry with 500ms backoff
- Decodes response as `StepPlan` (Codable)
- Throws `NetworkError` on failure

### 5. Shared Schema (`shared/step_plan_schema.json`)

You are the owner of the schema. If you change it:
1. Update `shared/step_plan_schema.json`
2. Update `agent-server/app/schemas/step_plan.py` (Pydantic)
3. Tell Eng 4 to update `mac-client/OverlayGuide/Models/StepPlan.swift` (Codable) — or do it yourself

---

## How to Test Locally

### Run the server:
```bash
cd agent-server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add your OPENAI_API_KEY

# Real mode:
uvicorn app.main:app --reload

# Mock mode (no API key needed):
MOCK_MODE=true uvicorn app.main:app --reload
```

### Test with curl:
```bash
# Health check
curl http://localhost:8000/health

# Mock plan
MOCK_MODE=true uvicorn app.main:app --reload &
curl -X POST http://localhost:8000/plan \
  -F "goal=Create a calendar event" \
  -F 'image_size={"w":1920,"h":1080}' \
  -F "screenshot=@/tmp/test_screenshot.png"

# Real plan (needs API key + a real screenshot)
curl -X POST http://localhost:8000/plan \
  -F "goal=Create a calendar event for tomorrow at 3pm" \
  -F 'image_size={"w":1920,"h":1080}' \
  -F "screenshot=@/tmp/test_screenshot.png" \
  -H "X-Request-ID: test-123"
```

### Test the Swift client:
Build the mac-client and use a debug button or breakpoint in `GuidanceStateMachine.submitGoal()` to verify:
- Request is sent correctly
- Response deserializes into a `StepPlan`
- Request ID appears in both client and server logs

### Validate schema compliance:
```python
import json
from app.schemas.step_plan import StepPlan

with open("../shared/example_step_plan.json") as f:
    data = json.load(f)

plan = StepPlan.model_validate(data)
print(f"Valid! {len(plan.steps)} steps")
```

---

## Integration Points

| What | Who | How |
|------|-----|-----|
| State machine calls your networking client | Eng 4 | `stateMachine.submitGoal()` calls `networkClient.requestPlan(...)` |
| Screenshot data comes from Eng 2 | Eng 2 | `ScreenshotResult.imageData` (PNG bytes) is passed to your `requestPlan()` |
| Schema must match Swift models | Eng 4 | If you change the schema, coordinate with Eng 4 on `Models/StepPlan.swift` |
| Mock mode for Eng 1 testing | Eng 1 | Eng 1 can test overlay rendering against your mock plan |

---

## Prompt Engineering Tips

- **Be specific about coordinate format** in the prompt — "x,y is top-left corner, normalized to [0,1]"
- **Require tight bounding boxes** — "target rectangle should tightly wrap the clickable element"
- **Cap steps at 6** — long plans overwhelm users
- **Set temperature low** (0.1) for consistent structured output
- **Test against multiple screenshots** — Safari, Chrome, Finder, System Settings
- **Watch for hallucinated UI elements** — if the model isn't confident, it should use fewer steps with a final `manual_next` step

---

## Later: `/replan` Endpoint

After MVP, add `POST /replan` that accepts:
- Everything from `/plan`
- Plus `current_step_id` and `session_summary`
- Returns a revised plan that reuses valid prior steps

This is triggered when the user is stuck (3+ click misses on one step).
