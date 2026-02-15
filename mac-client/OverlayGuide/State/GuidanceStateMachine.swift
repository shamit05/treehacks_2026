// State/GuidanceStateMachine.swift
// Owner: Eng 4 (State Machine)
//
// Central state machine for the overlay guidance flow.
// Owns the session state, controls step progression, and
// coordinates between capture, networking, and overlay.
//
// This is an ObservableObject so SwiftUI views can react to changes.

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

    // MARK: - Session

    private var session: SessionSnapshot

    // MARK: - Dependencies

    private let networkClient: AgentNetworkClient
    private let captureService: ScreenCaptureService
    private let lastGoalPath = "/tmp/overlayguide_last_goal.txt"
    private let lastScreenshotPath = "/tmp/overlayguide_last_screenshot.png"
    private let coordinateDebugEnabled = true
    private var currentMarkersByID: [Int: SOMMarker] = [:]
    private var stepMarkerByStepID: [String: SOMMarker] = [:]
    private var stepMissCounts: [String: Int] = [:]
    private var refinedStepIDs: Set<String> = []
    private var refineInFlight = false

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
                let screenshot = try await captureService.captureMainDisplay()
                self.capturedScreenBounds = screenshot.screenBounds
                self.currentMarkersByID = [:]
                self.stepMarkerByStepID = [:]
                self.stepMissCounts = [:]
                self.refinedStepIDs = []
                saveDebugArtifacts(goal: goal, screenshotData: screenshot.imageData)

                let markers = MarkerGenerator.generateGridMarkers()
                let markedScreenshot = try MarkerGenerator.renderMarkers(on: screenshot.imageData, markers: markers)
                let markersByID = Dictionary(uniqueKeysWithValues: markers.map { ($0.id, $0) })
                // Show loading only after the screenshot is already captured.
                self.phase = .loading
                let plan = try await networkClient.requestPlan(
                    goal: goal,
                    screenshotWithMarkersData: markedScreenshot,
                    markers: markers,
                    imageSize: ImageSize(w: screenshot.pixelWidth, h: screenshot.pixelHeight),
                    learningProfile: session.learningProfile
                )
                self.applyInitialPlan(plan, markersByID: markersByID)
            } catch {
                self.phase = .error(error.localizedDescription)
            }
        }
    }

    /// External integration entry point for the first `/plan` response.
    func applyInitialPlan(_ plan: StepPlan, markersByID: [Int: SOMMarker] = [:]) {
        let renderedPlan = materializePlanTargets(plan, markersByID: markersByID)
        currentPlan = renderedPlan
        currentStepIndex = 0
        completedSteps = []
        session.stepPlan = renderedPlan
        session.currentStepIndex = 0
        currentMarkersByID = markersByID
        stepMissCounts = [:]
        refinedStepIDs = []
        addEvent(.planReceived)
        logStepTargets(context: "initial-plan")
        phase = .guiding
        triggerRefineIfNeededForCurrentStep()
    }

    /// External integration entry point for `/next` responses.
    func applyNextPlan(_ plan: StepPlan) {
        currentPlan = plan
        currentStepIndex = 0
        session.stepPlan = plan
        session.currentStepIndex = 0
        stepMarkerByStepID = [:]
        stepMissCounts = [:]
        refinedStepIDs = []
        addEvent(.planReceived, detail: "next")
        logStepTargets(context: "next-plan")
        phase = .guiding
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

    /// Handle a mouse click at screen coordinates
    func handleClick(at point: CGPoint) {
        guard phase == .guiding,
              let plan = currentPlan,
              currentStepIndex < plan.steps.count else { return }

        let step = plan.steps[currentStepIndex]
        let screenBounds = capturedScreenBounds ?? NSScreen.main?.frame
        guard let screenBounds else { return }
        guard screenBounds.contains(point) else {
            addEvent(.clickMiss)
            hintMessage = "Try clicking the highlighted area"
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                self.hintMessage = nil
            }
            return
        }

        let mapper = CoordinateMapper(
            screenBounds: screenBounds,
            scaleFactor: NSScreen.main?.backingScaleFactor ?? 1.0
        )
        let normalizedClick = mapper.screenToNormalized(point)
        if coordinateDebugEnabled {
            print("[CoordsDebug] click screen=(\(format(point.x)), \(format(point.y))) normalized=(\(format(normalizedClick.x)), \(format(normalizedClick.y))) bounds=\(rectString(screenBounds))")
        }

        if coordinateDebugEnabled {
            for (idx, target) in step.targets.enumerated() {
                guard let targetRect = mapper.normalizedToScreen(target) else {
                    print("[CoordsDebug] target[\(idx)] non-bbox type=\(target.type.rawValue) hit=false")
                    continue
                }
                let inside = normalizedPoint(normalizedClick, isInside: target)
                print(
                    "[CoordsDebug] target[\(idx)] norm=(x:\(format(target.x ?? -1)), y:\(format(target.y ?? -1)), w:\(format(target.w ?? -1)), h:\(format(target.h ?? -1))) screen=\(rectString(targetRect)) hit=\(inside)"
                )
            }
        }

        let hitTarget = step.targets.first { target in
            normalizedPoint(normalizedClick, isInside: target)
        }

        if hitTarget != nil {
            stepMissCounts[step.id] = 0
            handleStepCompletionFromClick()
        } else {
            addEvent(.clickMiss)
            stepMissCounts[step.id, default: 0] += 1
            hintMessage = "Try clicking the highlighted area"
            triggerRefineIfNeeded(for: step)
            // Clear hint after 2 seconds
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                self.hintMessage = nil
            }
        }
    }

    /// Called when a click successfully hits the current step target.
    /// Captures a fresh screenshot and asks `/next` for updated guidance.
    func handleStepCompletionFromClick() {
        guard phase == .guiding,
              let plan = currentPlan,
              currentStepIndex < plan.steps.count else { return }

        let completedStep = plan.steps[currentStepIndex]
        completedSteps.append(completedStep)
        addEvent(.clickHit)
        addEvent(.stepAdvanced)

        phase = .loading

        Task { @MainActor in
            do {
                // NOTE: we intentionally only trigger next-step refresh after a click completion,
                // not hover or tooltip changes, to avoid false state-change detections.
                let screenshot = try await captureService.captureMainDisplay()
                self.capturedScreenBounds = screenshot.screenBounds
                let nextPlan = try await networkClient.requestNext(
                    goal: session.goal,
                    screenshotData: screenshot.imageData,
                    imageSize: ImageSize(
                        w: screenshot.pixelWidth,
                        h: screenshot.pixelHeight
                    ),
                    completedSteps: completedSteps,
                    totalSteps: max(completedSteps.count, session.stepPlan?.steps.count ?? plan.steps.count),
                    learningProfile: session.learningProfile,
                    appContext: session.appContext
                )
                applyNextPlan(nextPlan)
            } catch {
                // Fallback to local step progression if `/next` is unavailable.
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
        capturedScreenBounds = nil
        completedSteps = []
        currentMarkersByID = [:]
        stepMarkerByStepID = [:]
        stepMissCounts = [:]
        refinedStepIDs = []
        refineInFlight = false
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
        guard target.type == .bboxNorm,
              let x = target.x,
              let y = target.y,
              let w = target.w,
              let h = target.h else {
            return false
        }
        let epsilon: CGFloat = 0.001
        let minX = CGFloat(x) - epsilon
        let minY = CGFloat(y) - epsilon
        let maxX = CGFloat(x + w) + epsilon
        let maxY = CGFloat(y + h) + epsilon
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
                print(
                    "[CoordsDebug] target[\(idx)] norm=(x:\(format(target.x ?? -1)), y:\(format(target.y ?? -1)), w:\(format(target.w ?? -1)), h:\(format(target.h ?? -1))) screen=\(rectString(rect))"
                )
            } else {
                print("[CoordsDebug] target[\(idx)] non-bbox type=\(target.type.rawValue)")
            }
        }
    }

    private func materializePlanTargets(_ plan: StepPlan, markersByID: [Int: SOMMarker]) -> StepPlan {
        var updatedSteps: [Step] = []
        stepMarkerByStepID = [:]

        for step in plan.steps {
            var updatedTargets: [TargetRect] = []
            for target in step.targets {
                switch target.type {
                case .bboxNorm:
                    updatedTargets.append(target)
                case .somMarker:
                    guard let markerID = target.markerId,
                          let marker = markersByID[markerID] else {
                        continue
                    }
                    if stepMarkerByStepID[step.id] == nil {
                        stepMarkerByStepID[step.id] = marker
                    }
                    updatedTargets.append(
                        CropRefiner.defaultBBox(
                            around: marker,
                            confidence: target.confidence,
                            label: target.label
                        )
                    )
                }
            }

            if updatedTargets.isEmpty {
                updatedTargets = [
                    TargetRect(
                        type: .bboxNorm,
                        markerId: nil,
                        x: 0.45,
                        y: 0.45,
                        w: 0.1,
                        h: 0.1,
                        confidence: 0.2,
                        label: "fallback target"
                    )
                ]
            }

            updatedSteps.append(
                Step(
                    id: step.id,
                    instruction: step.instruction,
                    targets: updatedTargets,
                    advance: step.advance,
                    safety: step.safety
                )
            )
        }

        return StepPlan(
            version: plan.version,
            goal: plan.goal,
            appContext: plan.appContext,
            imageSize: plan.imageSize,
            steps: updatedSteps
        )
    }

    private func triggerRefineIfNeededForCurrentStep() {
        guard let plan = currentPlan,
              currentStepIndex < plan.steps.count else { return }
        triggerRefineIfNeeded(for: plan.steps[currentStepIndex])
    }

    private func triggerRefineIfNeeded(for step: Step) {
        guard shouldTriggerRefine(for: step) else { return }
        Task { @MainActor in
            await refineCurrentStep(step)
        }
    }

    private func shouldTriggerRefine(for step: Step) -> Bool {
        if refineInFlight || refinedStepIDs.contains(step.id) {
            return false
        }
        guard stepMarkerByStepID[step.id] != nil else {
            return false
        }

        let misses = stepMissCounts[step.id, default: 0]
        let confidence = step.targets.first?.confidence ?? 1.0
        if confidence < 0.7 {
            return misses >= 1
        }
        return misses >= 2
    }

    @MainActor
    private func refineCurrentStep(_ step: Step) async {
        guard let marker = stepMarkerByStepID[step.id], !refineInFlight else { return }
        refineInFlight = true
        defer { refineInFlight = false }

        do {
            let screenshot = try await captureService.captureMainDisplay()
            capturedScreenBounds = screenshot.screenBounds
            let cropRect = CropRefiner.makeCropRect(around: marker)
            let cropData = try CropRefiner.cropImage(pngData: screenshot.imageData, cropRect: cropRect)
            let refinedCropBBox = try await networkClient.requestRefine(
                goal: session.goal,
                stepId: step.id,
                instruction: step.instruction,
                cropImageData: cropData,
                cropRectFullNorm: cropRect,
                sessionSummary: recentSessionSummary()
            )

            guard let stitched = BBoxStitcher.stitch(cropRect: cropRect, cropBBox: refinedCropBBox) else {
                return
            }

            updateStepTarget(stepID: step.id, target: stitched)
            refinedStepIDs.insert(step.id)
            hintMessage = "Target refined for better accuracy."
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                self.hintMessage = nil
            }
            logStepTargets(context: "refined")
        } catch {
            print("[Refine] failed for step \(step.id): \(error.localizedDescription)")
        }
    }

    private func updateStepTarget(stepID: String, target: TargetRect) {
        guard let plan = currentPlan else { return }
        let updatedSteps = plan.steps.map { step -> Step in
            guard step.id == stepID else { return step }
            return Step(
                id: step.id,
                instruction: step.instruction,
                targets: [target],
                advance: step.advance,
                safety: step.safety
            )
        }
        currentPlan = StepPlan(
            version: plan.version,
            goal: plan.goal,
            appContext: plan.appContext,
            imageSize: plan.imageSize,
            steps: updatedSteps
        )
        session.stepPlan = currentPlan
    }

    private func recentSessionSummary() -> String {
        let recent = session.events.suffix(5)
        if recent.isEmpty {
            return "none"
        }
        return recent.map { "\($0.type.rawValue):\($0.detail ?? "-")" }.joined(separator: "; ")
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
