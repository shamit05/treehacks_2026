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

    // MARK: - Session

    private var session: SessionSnapshot

    // MARK: - Dependencies

    private let networkClient: AgentNetworkClient
    private let captureService: ScreenCaptureService

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

    /// User submitted a goal
    func submitGoal(_ goal: String) {
        guard phase == .inputGoal else { return }
        session.goal = goal
        phase = .loading

        Task { @MainActor in
            do {
                let screenshot = try await captureService.captureMainDisplay()
                let plan = try await networkClient.requestPlan(
                    goal: goal,
                    screenshotData: screenshot.imageData,
                    imageSize: ImageSize(w: Int(screenshot.screenBounds.width), h: Int(screenshot.screenBounds.height)),
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
        phase = .guiding
    }

    /// External integration entry point for `/next` responses.
    func applyNextPlan(_ plan: StepPlan) {
        currentPlan = plan
        currentStepIndex = 0
        session.stepPlan = plan
        session.currentStepIndex = 0
        addEvent(.planReceived, detail: "next")
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
        guard let screen = NSScreen.main else { return }

        let mapper = CoordinateMapper(
            screenBounds: screen.frame,
            scaleFactor: screen.backingScaleFactor
        )

        let hitTarget = step.targets.first { target in
            mapper.isClick(point, insideTarget: target)
        }

        if hitTarget != nil {
            handleStepCompletionFromClick()
        } else {
            addEvent(.clickMiss)
            hintMessage = "Try clicking the highlighted area"
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
                let nextPlan = try await networkClient.requestNext(
                    goal: session.goal,
                    screenshotData: screenshot.imageData,
                    imageSize: ImageSize(
                        w: Int(screenshot.screenBounds.width),
                        h: Int(screenshot.screenBounds.height)
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
}
