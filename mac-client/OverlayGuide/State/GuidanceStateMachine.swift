// State/GuidanceStateMachine.swift
// Owner: Eng 4 (State + Integration)
//
// Central state machine for the overlay guidance flow.
// Owns the session state, controls step progression, and
// coordinates between capture, networking, and overlay.
//
// This is an ObservableObject so SwiftUI views can react to changes.

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
                self.currentPlan = plan
                self.currentStepIndex = 0
                self.session.stepPlan = plan
                self.session.currentStepIndex = 0
                self.addEvent(.planReceived)
                self.phase = .guiding
            } catch {
                self.phase = .error(error.localizedDescription)
            }
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
            advanceStep()
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

    /// Move to the next step
    func advanceStep() {
        guard let plan = currentPlan else { return }
        addEvent(.clickHit)
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
