# OverlayGuide

A macOS system-wide AI guidance overlay. Press a hotkey, describe what you want to do, and get step-by-step visual instructions overlaid on top of any app.

## Quick Start

### Agent Server (Python)

```bash
cd agent-server
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your OPENAI_API_KEY
uvicorn app.main:app --reload
```

Mock mode (no API key needed):
```bash
MOCK_MODE=true uvicorn app.main:app --reload
```

Server runs at `http://localhost:8000`. Health check: `GET /health`.

### Mac Client (Swift)

**Recommended — run as .app (shows in Accessibility):**
```bash
cd mac-client
chmod +x run.sh
./run.sh
```

On first run, add **OverlayGuide** to System Settings > Privacy & Security > Accessibility (click + and select the OverlayGuide.app that appears). Then press **Cmd+Option+O** to toggle the overlay.

**Or build and run directly:**
```bash
cd mac-client
swift run OverlayGuide
```
(Requires adding Terminal to Accessibility, or the binary path when prompted.)

> Requires macOS 13+, Screen Recording permission, and Accessibility permission for the hotkey.

## Architecture

```
User presses hotkey
  → Overlay UI appears
  → User types goal
  → Screenshot captured
  → POST /plan to agent-server
  → Agent returns StepPlan JSON
  → Overlay renders step highlights
  → User clicks target → step advances
  → Completion screen
```

## Repo Structure

```
mac-client/                 # Swift macOS app
  OverlayGuide/
    App/                    # Entry point, AppDelegate
    Overlay/                # Overlay windows + highlight rendering
    Capture/                # Screenshot capture + coordinate mapping
    Input/                  # Global hotkey + mouse click detection
    State/                  # State machine + session state
    Networking/             # HTTP client to agent-server
    Models/                 # Shared data models (StepPlan, etc.)
    UI/                     # SwiftUI views (goal input, completion)
agent-server/               # Python FastAPI backend
  app/
    main.py                 # App entry + CORS
    routers/plan.py         # POST /plan endpoint
    schemas/step_plan.py    # Pydantic models
    prompts/                # Agent prompt templates
    services/agent.py       # AI model integration
    services/mock.py        # Mock mode for demos
shared/                     # Cross-platform artifacts
  step_plan_schema.json     # Source of truth JSON schema
  example_step_plan.json    # Example plan for testing
docs/
  spec.md                   # Full project spec
```

## Engineer Ownership

Each engineer has a detailed runbook in `docs/` — **read yours before starting**.

| Engineer | Area | Directories | Runbook |
|----------|------|-------------|---------|
| **Eng 1** | Overlay + UI | `mac-client/.../Overlay/`, `mac-client/.../UI/` | [`docs/eng1-overlay-ui.md`](docs/eng1-overlay-ui.md) |
| **Eng 2** | Capture + Input | `mac-client/.../Capture/`, `mac-client/.../Input/` | [`docs/eng2-capture-input.md`](docs/eng2-capture-input.md) |
| **Eng 3** | Agent Pipeline | `agent-server/`, `mac-client/.../Networking/`, `shared/` | [`docs/eng3-agent.md`](docs/eng3-agent.md) |
| **Eng 4** | State Machine | `mac-client/.../State/`, `mac-client/.../Models/`, `mac-client/.../App/` | [`docs/eng4-state.md`](docs/eng4-state.md) |

## Key Conventions

- **Coordinates**: Normalized `[0,1]`, top-left origin `(0,0)`
- **Schema**: All AI outputs validate against `shared/step_plan_schema.json`
- **Request IDs**: UUID in `X-Request-ID` header, logged on both client and server
- **Commit prefixes**: `[overlay]`, `[capture]`, `[agent]`, `[state]`

See `.cursor/rules/project.mdc` for the full coding rules.

## Testing Overlay Output Updates

- Use backend mock mode to exercise both `POST /plan` and `POST /next`.
- The mac state machine now supports applying raw API payloads directly via `applyPlanJSON(_:asNextPlan:)` for UI-only testing.
- Start with `shared/example_step_plan.json` as the initial payload, then paste a `/next`-shaped `StepPlan` JSON payload and call `applyPlanJSON(..., asNextPlan: true)` to verify the overlay refreshes in place.

### Quick UI Tester (CLI)

Run the mac client in synthetic UI test mode and pass parameters:

```bash
cd mac-client
swift run OverlayGuide --ui-test --goal "Create calendar event" --steps 4 --x 0.22 --y 0.24 --w 0.18 --h 0.05 --next-after 3
```

- Prints the generated `StepPlan` JSON to terminal (`initial`, then `next` if `--next-after` is provided)
- Renders the overlay directly with those synthetic steps
- Skips hotkey/click monitor setup in this mode to make testing deterministic

See all test flags:

```bash
cd mac-client
swift run OverlayGuide --ui-test-help
```
