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

Open `mac-client/` in Xcode:
```bash
cd mac-client
open Package.swift
```

Or build from terminal:
```bash
cd mac-client
swift build
```

> Requires macOS 13+ and Screen Recording permission.

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

| Engineer | Area | Directories |
|----------|------|-------------|
| **Eng 1** | Overlay UI | `mac-client/.../Overlay/`, `mac-client/.../UI/` |
| **Eng 2** | Capture + Input | `mac-client/.../Capture/`, `mac-client/.../Input/` |
| **Eng 3** | Agent Server | `agent-server/` |
| **Eng 4** | State + Integration | `mac-client/.../State/`, `mac-client/.../Networking/`, `mac-client/.../Models/` |

## Key Conventions

- **Coordinates**: Normalized `[0,1]`, top-left origin `(0,0)`
- **Schema**: All AI outputs validate against `shared/step_plan_schema.json`
- **Request IDs**: UUID in `X-Request-ID` header, logged on both client and server
- **Commit prefixes**: `[overlay]`, `[capture]`, `[agent]`, `[state]`

See `.cursor/rules/project.mdc` for the full coding rules.
