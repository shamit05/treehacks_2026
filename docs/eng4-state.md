# Eng 4 — State Machine

## What You Own

You are the **central nervous system** of the app. You own the state machine that coordinates everything: what phase the app is in, when to capture screenshots, when to call the agent, how to advance steps, and when to show/hide the overlay. Everyone else reads from your state or calls methods on it.

### Your Directories

```
mac-client/OverlayGuide/
  State/                            # Core state machine
    GuidanceStateMachine.swift      # ObservableObject — the brain of the app
  Models/                           # Shared Swift data models
    StepPlan.swift                  # Codable models matching JSON schema
    SessionState.swift              # Session snapshot, events, learning profile
  App/                              # App entry point + wiring
    OverlayGuideApp.swift           # @main SwiftUI App
    AppDelegate.swift               # Creates all services, wires them together
```

### Your Commit Prefix

`[state]` — e.g. `[state] add stuck detection after 3 click misses`

---

## What You Need to Build (MVP)

### 1. State Machine (`State/GuidanceStateMachine.swift`)

This is the most important file in the project. It's an `ObservableObject` that publishes state for the UI and exposes methods for the input monitor.

**Phases:**
```
idle → inputGoal → loading → guiding → completed
                                ↓
                              error
```

| Phase | What's Happening | Who Triggers It |
|-------|-----------------|-----------------|
| `idle` | App running, overlay hidden | Initial state, or `reset()` |
| `inputGoal` | Overlay visible, user typing | Eng 2 calls `toggleOverlay()` via hotkey |
| `loading` | Screenshot + agent request in flight | `submitGoal(text)` called |
| `guiding` | Showing step plan, user following instructions | Agent response received |
| `completed` | All steps done, success screen | Last step advanced |
| `error(msg)` | Something went wrong | Capture or network failure |

**Published properties** (Eng 1 observes these):
- `phase: GuidancePhase`
- `currentPlan: StepPlan?`
- `currentStepIndex: Int`
- `hintMessage: String?`

**Key methods:**
- `toggleOverlay()` — called by Eng 2's hotkey handler
- `submitGoal(_ goal: String)` — called by Eng 1's GoalInputView
- `handleClick(at point: CGPoint)` — called by Eng 2's click detector
- `advanceStep()` — moves to next step (or completes)
- `reset()` — back to idle, clear everything

### 2. Submit Goal Flow

When `submitGoal()` is called:
1. Set `phase = .loading`
2. Call Eng 2's `captureService.captureMainDisplay()` to get a screenshot
3. Call Eng 3's `networkClient.requestPlan(goal:screenshotData:imageSize:)` with the screenshot
4. On success: store the plan, set `currentStepIndex = 0`, set `phase = .guiding`
5. On failure: set `phase = .error(message)`

All of this happens in a `Task { @MainActor in ... }` block since published properties must update on the main thread.

### 3. Click Handling Flow

When `handleClick(at:)` is called (only when `phase == .guiding`):
1. Get the current step from `currentPlan.steps[currentStepIndex]`
2. Use Eng 2's `CoordinateMapper` to check if the click is inside any target rect
3. **Hit:** call `advanceStep()` — increment index, check if done
4. **Miss:** log a `clickMiss` event, set `hintMessage = "Try clicking the highlighted area"`, clear hint after 2 seconds

### 4. Session State (`Models/SessionState.swift`)

Track session data for debugging and future replanning:
- `sessionId: UUID`
- `goal: String`
- `learningProfile: LearningProfile?`
- `stepPlan: StepPlan?`
- `currentStepIndex: Int`
- `events: [SessionEvent]` — capped at 50

Event types: `clickHit`, `clickMiss`, `stepAdvanced`, `planReceived`, `replanRequested`

### 5. Data Models (`Models/StepPlan.swift`)

Swift `Codable` structs matching `shared/step_plan_schema.json`:
- `StepPlan` (top-level)
- `Step`, `TargetRect`, `Advance`, `AdvanceType`, `Safety`
- `AppContext`, `ImageSize`

If Eng 3 changes the schema, you update these models to match. Use `CodingKeys` for snake_case ↔ camelCase mapping.

### 6. App Wiring (`App/AppDelegate.swift`)

`AppDelegate.applicationDidFinishLaunching`:
1. Check Screen Recording permission (call Eng 2's `captureService.hasScreenRecordingPermission()`)
2. Create all services: `ScreenCaptureService`, `AgentNetworkClient`, `GuidanceStateMachine`, `OverlayWindowController`, `GlobalInputMonitor`
3. Pass dependencies via init injection (no singletons)
4. Start the input monitor
5. Observe `stateMachine.phase` changes and call `overlayController.showOverlay()` / `hideAll()` accordingly

---

## How to Test Locally

### State machine in isolation:
You can test the state machine without the real server:
1. Start the agent server in mock mode: `MOCK_MODE=true uvicorn app.main:app --reload`
2. Run the mac-client — it will hit localhost:8000 and get a valid mock plan
3. Verify phase transitions: idle → inputGoal → loading → guiding → completed

### Manual phase testing:
Add a debug menu or keyboard shortcuts (in DEBUG builds only) to:
- Force a mock plan into the state machine
- Jump to a specific step
- Trigger an error state

```swift
#if DEBUG
func debugLoadMockPlan() {
    currentPlan = StepPlan(
        version: "v1",
        goal: "Debug test",
        imageSize: ImageSize(w: 1920, h: 1080),
        steps: [ /* mock steps */ ]
    )
    currentStepIndex = 0
    phase = .guiding
}
#endif
```

### Click handling:
1. Load a mock plan
2. Print the screen rects for each target: `print(mapper.normalizedToScreen(target))`
3. Click inside and outside those rects
4. Verify step advancement and hint messages

---

## Integration Points

| What | Who | How |
|------|-----|-----|
| Eng 2 calls your methods | Eng 2 | `toggleOverlay()`, `handleClick(at:)` |
| Eng 1 observes your state | Eng 1 | `@ObservedObject var stateMachine` — reads phase, plan, step index |
| Eng 1 calls submitGoal | Eng 1 | From GoalInputView |
| You call Eng 2's capture | Eng 2 | `captureService.captureMainDisplay()` |
| You call Eng 3's network | Eng 3 | `networkClient.requestPlan(...)` |
| You use Eng 2's mapper | Eng 2 | `CoordinateMapper.isClick(point, insideTarget:)` |
| Eng 3 changes schema | Eng 3 | You update `Models/StepPlan.swift` to match |

**You are the integration point.** If something doesn't work end-to-end, it's probably a wiring issue in your code. Coordinate early with everyone.

---

## Phase Transition Diagram

```
                    hotkey
        idle ──────────────► inputGoal
         ▲                       │
         │ reset()               │ submitGoal()
         │                       ▼
    completed ◄──── guiding ◄── loading
                       │            │
                       │            │ error
                       │            ▼
                       └────► error(msg)
                  (3+ misses → replan later)
```

---

## Later: Stuck Detection + Replan

After MVP, add:
- Count consecutive `clickMiss` events per step
- After 3 misses, capture a new screenshot and call `networkClient.requestReplan(...)` (Eng 3 will build `/replan`)
- Replace the current plan with the new one, keep the step index where the user was stuck
