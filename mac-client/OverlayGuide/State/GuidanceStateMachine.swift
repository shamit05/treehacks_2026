// State/GuidanceStateMachine.swift
// Owner: Eng 4 (State Machine)
//
// Central state machine for the overlay guidance flow.
// Owns the session state, controls step progression, and
// coordinates between capture, networking, and overlay.
//
// All SoM marker generation and refinement happens server-side.
// The client just sends raw screenshots and receives refined TargetRects.

import AppKit
import Combine
import Foundation

enum GuidancePhase: Equatable {
    case idle                // overlay hidden, waiting for hotkey
    case inputGoal           // overlay visible, user typing goal
    case loading             // screenshot + agent request in flight
    case guiding             // showing step plan, user following steps
    case completed           // all steps done
    case error(String)       // something went wrong
}

class GuidanceStateMachine: ObservableObject {

    // MARK: - Published State (UI observes these)

    @Published var phase: GuidancePhase = .idle
    @Published var currentPlan: StepPlan?
    @Published var currentStepIndex: Int = 0
    @Published var hintMessage: String?
    @Published private(set) var completedSteps: [Step] = []
    @Published private(set) var capturedScreenBounds: CGRect?
    @Published var completionMessage: String?
    @Published var loadingStatus: String = "Processing screen..."

    // MARK: - Session

    private var session: SessionSnapshot

    // MARK: - Dependencies

    private let networkClient: AgentNetworkClient
    private let captureService: ScreenCaptureService
    private let lastGoalPath = "/tmp/overlayguide_last_goal.txt"
    private let lastScreenshotPath = "/tmp/overlayguide_last_screenshot.png"
    private let coordinateDebugEnabled = true

    init(networkClient: AgentNetworkClient, captureService: ScreenCaptureService) {
        self.networkClient = networkClient
        self.captureService = captureService
        self.session = SessionSnapshot(
            sessionId: UUID(),
            goal: "",
            currentStepIndex: 0,
            events: []
        )
    }

    // MARK: - Actions

    /// Toggle overlay on/off (called by hotkey)
    func toggleOverlay() {
        switch phase {
        case .idle:
            phase = .inputGoal
        case .inputGoal, .guiding, .completed, .error:
            reset()
        case .loading:
            break // don't interrupt in-flight request
        }
    }

    /// Explicitly show goal input overlay.
    func showInputOverlay() {
        guard phase != .loading else { return }
        currentPlan = nil
        currentStepIndex = 0
        hintMessage = nil
        capturedScreenBounds = nil
        phase = .inputGoal
    }

    /// User submitted a goal
    func submitGoal(_ goal: String) {
        guard phase == .inputGoal else { return }
        session.goal = goal
        // Hide overlay before capture so the popup is never included in the screenshot.
        phase = .idle

        Task { @MainActor in
            do {
                // Give the window system a brief moment to remove overlay windows.
                try? await Task.sleep(nanoseconds: 150_000_000)

                loadingStatus = "Capturing screen..."
                self.phase = .loading

                let screenshot = try await captureService.captureMainDisplay()
                self.capturedScreenBounds = screenshot.screenBounds
                let imgSize = ImageSize(
                    w: Int(screenshot.screenBounds.width),
                    h: Int(screenshot.screenBounds.height)
                )

                saveDebugArtifacts(goal: goal, screenshotData: screenshot.imageData)

                loadingStatus = "Analyzing UI elements..."

                // Progressive status updates while waiting
                Task { @MainActor in
                    try? await Task.sleep(nanoseconds: 2_500_000_000)
                    if self.phase == .loading { self.loadingStatus = "Creating plan..." }
                    try? await Task.sleep(nanoseconds: 3_000_000_000)
                    if self.phase == .loading { self.loadingStatus = "Generating overlays..." }
                }

                let plan = try await networkClient.requestPlan(
                    goal: goal,
                    screenshotData: screenshot.imageData,
                    imageSize: imgSize,
                    learningProfile: session.learningProfile
                )

                self.applyInitialPlan(plan)
            } catch {
                self.phase = .error(error.localizedDescription)
            }
        }
    }

    /// External integration entry point for the first `/plan` response.
    func applyInitialPlan(_ plan: StepPlan) {
        currentPlan = plan
        currentStepIndex = 0
        completedSteps = []
        session.stepPlan = plan
        session.currentStepIndex = 0
        addEvent(.planReceived)
        logStepTargets(context: "initial-plan")
        phase = .guiding
    }

    /// External integration entry point for `/next` responses.
    /// Handles status: "continue" (more steps), "done" (task complete), "retry" (try again).
    func applyNextResponse(_ response: NextStepResponse) {
        switch response.status {
        case "done":
            // Task is complete!
            addEvent(.planReceived, detail: "done")
            print("[State] Task complete: \(response.message ?? "done")")
            completionMessage = response.message ?? "All done!"
            phase = .completed

        case "retry":
            // Action didn't take effect — stay on current step
            addEvent(.planReceived, detail: "retry")
            print("[State] Retry: \(response.message ?? "try again")")
            hintMessage = response.message ?? "That didn't seem to work. Try clicking the highlighted area again."
            phase = .guiding
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                self.hintMessage = nil
            }

        default:
            // "continue" — more steps to follow
            guard !response.steps.isEmpty else {
                // No steps but status is continue — treat as done
                completionMessage = response.message ?? "All done!"
                phase = .completed
                return
            }
            let plan = StepPlan(
                version: response.version,
                goal: response.goal,
                appContext: nil,
                imageSize: response.imageSize,
                steps: response.steps
            )
            currentPlan = plan
            currentStepIndex = 0
            session.stepPlan = plan
            session.currentStepIndex = 0
            addEvent(.planReceived, detail: "next")
            logStepTargets(context: "next-plan")
            phase = .guiding
        }
    }

    /// Convenience: apply a StepPlan as a "continue" next response.
    func applyNextPlan(_ plan: StepPlan) {
        let response = NextStepResponse(
            version: plan.version,
            goal: plan.goal,
            status: "continue",
            message: nil,
            imageSize: plan.imageSize,
            steps: plan.steps
        )
        applyNextResponse(response)
    }

    /// Debug integration helper: apply a raw JSON StepPlan payload from `/plan` or `/next`.
    func applyPlanJSON(_ json: String, asNextPlan: Bool = false) throws {
        let data = Data(json.utf8)
        let plan = try JSONDecoder().decode(StepPlan.self, from: data)
        if asNextPlan {
            applyNextPlan(plan)
        } else {
            applyInitialPlan(plan)
        }
    }

    /// Handle a mouse click at screen coordinates.
    /// NOTE: `point` comes from CGEvent.location which uses Quartz coordinates
    /// (top-left origin, Y increases downward) — same convention as our
    /// normalized [0,1] coordinates. capturedScreenBounds is from CGDisplayBounds
    /// which is also Quartz. So we normalize directly WITHOUT flipping Y.
    func handleClick(at point: CGPoint) {
        guard phase == .guiding,
              let plan = currentPlan,
              currentStepIndex < plan.steps.count else { return }

        let step = plan.steps[currentStepIndex]
        let screenBounds = capturedScreenBounds ?? NSScreen.main?.frame
        guard let screenBounds else { return }

        // Normalize CGEvent point (Quartz top-left origin) to [0,1] (also top-left origin).
        // Both coordinate systems use top-left origin, so NO Y-flip needed.
        let normX = (point.x - screenBounds.origin.x) / screenBounds.width
        let normY = (point.y - screenBounds.origin.y) / screenBounds.height
        let normalizedClick = CGPoint(x: normX, y: normY)

        // Check bounds (allow small margin outside [0,1] for edge clicks)
        guard normX >= -0.02 && normX <= 1.02 && normY >= -0.02 && normY <= 1.02 else {
            addEvent(.clickMiss)
            hintMessage = "Try clicking the highlighted area"
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                self.hintMessage = nil
            }
            return
        }

        // File-based debug log (print() is lost when launched via `open`)
        let debugMsg = "[Click] screen=(\(format(point.x)),\(format(point.y))) norm=(\(format(normalizedClick.x)),\(format(normalizedClick.y))) bounds=\(rectString(screenBounds)) targets=\(step.targets.map { "(\(format($0.x ?? -1)),\(format($0.y ?? -1)),\(format($0.w ?? -1)),\(format($0.h ?? -1)))" })"
        if coordinateDebugEnabled {
            print(debugMsg)
        }
        if let data = (debugMsg + "\n").data(using: .utf8) {
            let url = URL(fileURLWithPath: "/tmp/overlayguide_clicks.log")
            if let handle = try? FileHandle(forWritingTo: url) {
                handle.seekToEndOfFile()
                handle.write(data)
                handle.closeFile()
            } else {
                try? data.write(to: url)
            }
        }

        let hitTarget = step.targets.first { target in
            normalizedPoint(normalizedClick, isInside: target)
        }

        if hitTarget != nil {
            handleStepCompletionFromClick()
        } else {
            addEvent(.clickMiss)
            hintMessage = "Try clicking the highlighted area"
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                self.hintMessage = nil
            }
        }
    }

    /// Called when a click successfully hits the current step target.
    func handleStepCompletionFromClick() {
        guard phase == .guiding,
              let plan = currentPlan,
              currentStepIndex < plan.steps.count else { return }

        let completedStep = plan.steps[currentStepIndex]
        completedSteps.append(completedStep)
        addEvent(.clickHit)
        addEvent(.stepAdvanced)

        loadingStatus = "Processing next step..."
        phase = .loading

        Task { @MainActor in
            do {
                let screenshot = try await captureService.captureMainDisplay()
                self.capturedScreenBounds = screenshot.screenBounds
                let imgSize = ImageSize(
                    w: Int(screenshot.screenBounds.width),
                    h: Int(screenshot.screenBounds.height)
                )

                let nextResponse = try await networkClient.requestNext(
                    goal: session.goal,
                    screenshotData: screenshot.imageData,
                    imageSize: imgSize,
                    completedSteps: completedSteps,
                    totalSteps: max(completedSteps.count, session.stepPlan?.steps.count ?? plan.steps.count),
                    learningProfile: session.learningProfile,
                    appContext: session.appContext
                )
                applyNextResponse(nextResponse)
            } catch {
                // Fallback to local step progression
                phase = .guiding
                advanceStep()
                hintMessage = "Live refresh unavailable. Continuing with existing steps."
                Task { @MainActor in
                    try? await Task.sleep(nanoseconds: 2_000_000_000)
                    self.hintMessage = nil
                }
            }
        }
    }

    /// Move to the next step
    func advanceStep() {
        guard let plan = currentPlan else { return }
        addEvent(.stepAdvanced)

        currentStepIndex += 1
        session.currentStepIndex = currentStepIndex

        if currentStepIndex >= plan.steps.count {
            phase = .completed
        }
    }

    /// Reset to idle
    func reset() {
        phase = .idle
        currentPlan = nil
        currentStepIndex = 0
        hintMessage = nil
        completionMessage = nil
        capturedScreenBounds = nil
        completedSteps = []
        session = SessionSnapshot(
            sessionId: UUID(),
            goal: "",
            currentStepIndex: 0,
            events: []
        )
    }

    // MARK: - Private

    private func addEvent(_ type: SessionEvent.EventType, detail: String? = nil) {
        let event = SessionEvent(timestamp: Date(), type: type, detail: detail)
        session.events.append(event)
        if session.events.count > SessionSnapshot.maxEvents {
            session.events.removeFirst()
        }
    }

    private func normalizedPoint(_ point: CGPoint, isInside target: TargetRect) -> Bool {
        guard target.hasBBox, let tx = target.x, let ty = target.y, let tw = target.w, let th = target.h else {
            return false
        }
        let epsilon: CGFloat = 0.001
        let minX = CGFloat(tx) - epsilon
        let minY = CGFloat(ty) - epsilon
        let maxX = CGFloat(tx + tw) + epsilon
        let maxY = CGFloat(ty + th) + epsilon
        return point.x >= minX && point.x <= maxX && point.y >= minY && point.y <= maxY
    }

    private func logStepTargets(context: String) {
        guard coordinateDebugEnabled else { return }
        guard let plan = currentPlan, currentStepIndex < plan.steps.count else { return }
        guard let screenBounds = capturedScreenBounds ?? NSScreen.main?.frame else { return }

        let step = plan.steps[currentStepIndex]
        let mapper = CoordinateMapper(screenBounds: screenBounds, scaleFactor: NSScreen.main?.backingScaleFactor ?? 1.0)
        print("[CoordsDebug] \(context) step=\(currentStepIndex + 1)/\(plan.steps.count) bounds=\(rectString(screenBounds))")
        for (idx, target) in step.targets.enumerated() {
            if let rect = mapper.normalizedToScreen(target) {
                print("[CoordsDebug] target[\(idx)] norm=(x:\(format(target.x ?? -1)), y:\(format(target.y ?? -1)), w:\(format(target.w ?? -1)), h:\(format(target.h ?? -1))) screen=\(rectString(rect))")
            }
        }
    }

    private func rectString(_ rect: CGRect) -> String {
        "(x:\(format(rect.origin.x)), y:\(format(rect.origin.y)), w:\(format(rect.width)), h:\(format(rect.height)))"
    }

    private func format(_ value: CGFloat) -> String {
        String(format: "%.3f", value)
    }

    private func format(_ value: Double) -> String {
        String(format: "%.3f", value)
    }

    private func saveDebugArtifacts(goal: String, screenshotData: Data) {
        let goalURL = URL(fileURLWithPath: lastGoalPath)
        let screenshotURL = URL(fileURLWithPath: lastScreenshotPath)
        do {
            if let goalData = goal.data(using: .utf8) {
                try goalData.write(to: goalURL)
            }
            try screenshotData.write(to: screenshotURL)
            print("[Debug] Saved goal to \(lastGoalPath)")
            print("[Debug] Saved screenshot to \(lastScreenshotPath)")
        } catch {
            print("[Debug] Failed to save artifacts: \(error.localizedDescription)")
        }
    }
}
