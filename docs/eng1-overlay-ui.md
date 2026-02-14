# Eng 1 — Overlay + UI

## What You Own

You are responsible for **everything the user sees**. The overlay windows, the highlight rectangles, the instruction bubbles, the goal input screen, the completion screen, animations, and visual polish. Your code makes or breaks the demo.

### Your Directories

```
mac-client/OverlayGuide/
  Overlay/                  # NSPanel window management + overlay content
    OverlayWindowController.swift   # Creates/manages one NSPanel per display
    OverlayContentView.swift        # SwiftUI view: highlights + instruction bubble
  UI/                       # Standalone SwiftUI views
    GoalInputView.swift             # Text input for the user's goal
    CompletionView.swift            # "All done!" success screen
```

### Your Commit Prefix

`[overlay]` — e.g. `[overlay] add pulsing animation to highlight rects`

---

## What You Need to Build (MVP)

### 1. Overlay Window System (`Overlay/OverlayWindowController.swift`)

Create one `NSPanel` per connected display that:
- Is borderless, transparent, always-on-top
- Appears on all Spaces and over fullscreen apps (`.canJoinAllSpaces`, `.fullScreenAuxiliary`)
- Does NOT steal focus (`.nonactivatingPanel`)
- Can toggle `ignoresMouseEvents` — pass-through when guiding, capture when showing input

Key API:
- `showOverlay()` — create panels for all `NSScreen.screens`
- `hideAll()` — close and remove all panels
- The content of each panel is a SwiftUI `NSHostingView`

### 2. Highlight Rendering (`Overlay/OverlayContentView.swift`)

For the current step, draw:
- **Semi-transparent dark backdrop** (Color.black.opacity(0.4)) covering the full screen
- **Cut-out highlight rectangles** around each target (bright border, slightly lighter fill)
- **Instruction bubble** near the target area with the step's `instruction` text
- **Step progress indicator** — "Step 2 of 5"

Targets come as normalized `[0,1]` coordinates. Convert to screen pixels:
```
pixelX = target.x * screenBounds.width
pixelY = target.y * screenBounds.height
pixelW = target.w * screenBounds.width
pixelH = target.h * screenBounds.height
```

### 3. Goal Input Screen (`UI/GoalInputView.swift`)

When `stateMachine.phase == .inputGoal`:
- Show a centered text field with placeholder like "What do you need help with?"
- Submit on Enter or click the send button
- Call `stateMachine.submitGoal(text)` on submit

### 4. Completion Screen (`UI/CompletionView.swift`)

When `stateMachine.phase == .completed`:
- Show a success message with the original goal
- "Dismiss" button that calls `stateMachine.reset()`

### 5. Loading State

When `stateMachine.phase == .loading`:
- Show a spinner / "Thinking..." indicator so the user knows the agent is working

### 6. Error State

When `stateMachine.phase == .error(message)`:
- Show the error message with a "Try Again" button

---

## What You Read From (Don't Modify)

You observe `GuidanceStateMachine` (owned by Eng 4). It's an `ObservableObject` with:

| Property | Type | What it tells you |
|----------|------|-------------------|
| `phase` | `GuidancePhase` | What screen to show (idle/inputGoal/loading/guiding/completed/error) |
| `currentPlan` | `StepPlan?` | The full plan with all steps and targets |
| `currentStepIndex` | `Int` | Which step to render |
| `hintMessage` | `String?` | "Try clicking the highlighted area" hint |

You call these methods on the state machine:
- `stateMachine.submitGoal(text)` — from GoalInputView
- `stateMachine.reset()` — from CompletionView dismiss button

---

## How to Test Locally

1. **Without the server:** Ask Eng 4 to add a "load mock plan" debug button on the state machine, or set `MOCK_MODE=true` on the agent server and run it locally.

2. **Visual testing:** Hardcode a `StepPlan` in a SwiftUI preview to iterate on highlight rendering:
   ```swift
   #Preview {
       let mockPlan = StepPlan(version: "v1", goal: "Test", imageSize: ImageSize(w: 1920, h: 1080), steps: [
           Step(id: "s1", instruction: "Click here", targets: [TargetRect(x: 0.1, y: 0.2, w: 0.15, h: 0.05)], advance: Advance(type: .clickInTarget))
       ])
       // render with mock data
   }
   ```

3. **Multi-display:** Test on an external monitor if possible. Each display gets its own NSPanel. Coordinates are per-display.

---

## Integration Points

| What | Who | How |
|------|-----|-----|
| State machine data | Eng 4 | `@ObservedObject var stateMachine: GuidanceStateMachine` |
| StepPlan / TargetRect models | Eng 4 | Import from `Models/StepPlan.swift` |
| Click detection | Eng 2 | You don't handle clicks — Eng 2's InputMonitor sends them to the state machine |
| Overlay show/hide | Eng 4 | Eng 4's AppDelegate calls your `showOverlay()` / `hideAll()` based on phase changes |

---

## Polish Checklist (Demo Day)

- [ ] Highlights pulse or glow subtly to draw the eye
- [ ] Instruction bubble has nice rounded corners + material blur
- [ ] Smooth transitions between steps (fade or slide)
- [ ] Loading spinner looks professional
- [ ] Works on Retina displays (no blurry rendering)
- [ ] Works over fullscreen apps (e.g., fullscreen Chrome)
