// Models/SessionState.swift
// Owner: Eng 4 (State Machine)
//
// Data model for the session state. The StateMachine owns an instance of this.

import Foundation

// MARK: - SessionEvent

struct SessionEvent: Codable, Equatable {
    let timestamp: Date
    let type: EventType
    let detail: String?

    enum EventType: String, Codable, Equatable {
        case clickHit       // user clicked inside a target
        case clickMiss      // user clicked outside all targets
        case stepAdvanced   // step index moved forward
        case planReceived   // new plan loaded
        case replanRequested
    }
}

// MARK: - LearningProfile

struct LearningProfile: Codable, Equatable {
    var text: String
    var presets: [String]?
}

// MARK: - SessionState (value type snapshot)

struct SessionSnapshot: Codable, Equatable {
    let sessionId: UUID
    var goal: String
    var learningProfile: LearningProfile?
    var appContext: AppContext?
    var stepPlan: StepPlan?
    var currentStepIndex: Int
    var events: [SessionEvent]

    // Hard limits from spec
    static let maxEvents = 50
    static let maxScreenshots = 3
}
