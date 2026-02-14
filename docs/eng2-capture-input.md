# Eng 2 — Capture + Input

## What You Own

You are responsible for **interacting with the OS**: capturing screenshots, detecting the global hotkey, and detecting mouse clicks. You're the bridge between macOS system APIs and the rest of the app.

### Your Directories

```
mac-client/OverlayGuide/
  Capture/                  # Screenshot capture + coordinate mapping
    ScreenCaptureService.swift      # Screenshot capture via ScreenCaptureKit / CGDisplay
    CoordinateMapper.swift          # Normalized <-> screen coordinate conversion
  Input/                    # Global hotkey + click detection
    GlobalInputMonitor.swift        # CGEventTap for clicks, Carbon API for hotkey
```

### Your Commit Prefix

`[capture]` — e.g. `[capture] implement ScreenCaptureKit screenshot path`

---

## What You Need to Build (MVP)

### 1. Screenshot Capture (`Capture/ScreenCaptureService.swift`)

Capture the main display and return a `ScreenshotResult` containing:
- `image: NSImage` — for local display if needed
- `imageData: Data` — PNG bytes to send to the agent server
- `screenBounds: CGRect` — display frame
- `scaleFactor: CGFloat` — Retina backing scale factor
- `timestamp: Date`

**Implementation priority:**
1. `CGDisplayCreateImage(CGMainDisplayID())` — works immediately, no entitlements needed beyond Screen Recording
2. `ScreenCaptureKit` (SCShareableContent + SCScreenshotManager) — preferred long-term, async API

**Timeout:** The capture must complete within 5 seconds or throw `CaptureError.timeout`.

**Permissions:** Before any capture call, check `CGPreflightScreenCaptureAccess()`. If false, call `CGRequestScreenCaptureAccess()` and show a message to the user (coordinate with Eng 1 for the UI).

### 2. Coordinate Mapper (`Capture/CoordinateMapper.swift`)

This is critical — wrong coordinate math = highlights in the wrong place.

**Coordinate convention (memorize this):**
- StepPlan JSON uses **normalized [0,1]** coordinates, **top-left origin** `(0,0)`
- macOS screen coordinates use **bottom-left origin**
- Your mapper handles the flip

Key functions:
- `normalizedToScreen(target) -> CGRect` — convert a `TargetRect` to screen-space rect
- `isClick(point, insideTarget) -> Bool` — check if a screen-space click hits a target
- `screenToNormalized(point) -> CGPoint` — convert screen click to normalized coords

**Y-flip formula:**
```
screenY = screenHeight - (normalizedY * screenHeight + normalizedH * screenHeight) + screenOriginY
```

### 3. Global Hotkey (`Input/GlobalInputMonitor.swift`)

Register a system-wide hotkey (default: **Cmd+Shift+G**) that works even when the app isn't focused.

**Options:**
- Carbon `RegisterEventHotKey` API — old but reliable, works everywhere
- [KeyboardShortcuts](https://github.com/sindresorhus/KeyboardShortcuts) Swift package — nicer API, drop-in

On hotkey press, call `stateMachine.toggleOverlay()`.

### 4. Mouse Click Detection (`Input/GlobalInputMonitor.swift`)

Use `CGEventTap` to intercept system-wide left mouse clicks:

```swift
let eventMask = (1 << CGEventType.leftMouseDown.rawValue)
let tap = CGEvent.tapCreate(
    tap: .cgSessionEventTap,
    place: .headInsertEventTap,
    options: .defaultTap,
    eventsOfInterest: CGEventMask(eventMask),
    callback: { proxy, type, event, refcon in
        // get click location, call stateMachine.handleClick(at:)
        return Unmanaged.passRetained(event)
    },
    userInfo: pointer_to_self
)
```

On each click, call `stateMachine.handleClick(at: clickPoint)` where `clickPoint` is in screen coordinates (bottom-left origin). The state machine + coordinate mapper handle the rest.

**Important:** Only detect clicks when `stateMachine.phase == .guiding`. Don't intercept clicks during input or idle.

---

## What You Read From / Write To

### You call on the state machine (Eng 4):
- `stateMachine.toggleOverlay()` — when hotkey is pressed
- `stateMachine.handleClick(at: CGPoint)` — when mouse click detected during guiding

### Others call your code:
- Eng 4's state machine calls `captureService.captureMainDisplay()` when the user submits a goal
- Eng 3's networking client uses the PNG `imageData` from your screenshot result

---

## How to Test Locally

### Screenshot capture:
```swift
// Quick test in a playground or test target:
let service = ScreenCaptureService()
Task {
    let result = try await service.captureMainDisplay()
    print("Got screenshot: \(result.imageData.count) bytes, bounds: \(result.screenBounds)")
    // Write to /tmp to visually verify:
    try result.imageData.write(to: URL(fileURLWithPath: "/tmp/test_screenshot.png"))
}
```

### Coordinate mapper:
```swift
let mapper = CoordinateMapper(screenBounds: CGRect(x: 0, y: 0, width: 1920, height: 1080), scaleFactor: 2.0)
let target = TargetRect(x: 0.5, y: 0.5, w: 0.1, h: 0.05)
let screenRect = mapper.normalizedToScreen(target)
print(screenRect) // Should be roughly (960, 486, 192, 54) accounting for Y-flip
```

### Hotkey:
- Build and run the app, press Cmd+Shift+G, verify the state machine transitions to `.inputGoal`

### Click detection:
- While in `.guiding` phase with a mock plan, click inside and outside the target rects
- Verify `handleClick` is called and the state machine responds correctly

---

## Integration Points

| What | Who | How |
|------|-----|-----|
| State machine | Eng 4 | You hold a reference to `GuidanceStateMachine` and call `toggleOverlay()` / `handleClick(at:)` |
| Screenshot data | Eng 3 | Eng 3's networking client sends your `ScreenshotResult.imageData` to the server |
| Coordinate mapper | Eng 4 | Eng 4's state machine uses `CoordinateMapper` to check click hits |
| Overlay show/hide | Eng 1 | Eng 1 needs the overlay to NOT intercept your click events when `ignoresMouseEvents = true` |

---

## Gotchas

- **Screen Recording permission** must be granted or screenshots return a blank/desktop-only image. Test with permission OFF to make sure your error handling works.
- **Retina displays** have `backingScaleFactor = 2.0`. CGDisplayCreateImage returns pixels at native resolution (e.g. 3840x2160 for a "1920x1080" display). Make sure you pass the logical size, not pixel size, to the agent.
- **Multiple displays** have different origins. `NSScreen.screens[1].frame.origin` might be `(1920, 0)` or `(-1440, -900)`. The coordinate mapper must account for `screenBounds.origin`.
- **CGEventTap requires Accessibility permission** (System Settings > Privacy > Accessibility). Handle the case where it's not granted.
