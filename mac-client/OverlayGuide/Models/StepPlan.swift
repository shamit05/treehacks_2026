// Models/StepPlan.swift
// Owner: Eng 4 (State Machine)
//
// Swift Codable models matching shared/step_plan_schema.json.
// This is the single source of truth on the client side.
// Keep in sync with the JSON schema and the Pydantic models in agent-server.

import Foundation

// MARK: - StepPlan

struct StepPlan: Codable, Equatable {
    let version: String
    let goal: String
    let appContext: AppContext?
    let imageSize: ImageSize
    let steps: [Step]

    enum CodingKeys: String, CodingKey {
        case version, goal, steps
        case appContext = "app_context"
        case imageSize = "image_size"
    }
}

// MARK: - AppContext

struct AppContext: Codable, Equatable {
    let appName: String
    let bundleId: String?
    let windowTitle: String?

    enum CodingKeys: String, CodingKey {
        case appName = "app_name"
        case bundleId = "bundle_id"
        case windowTitle = "window_title"
    }
}

// MARK: - ImageSize

struct ImageSize: Codable, Equatable {
    let w: Int
    let h: Int
}

// MARK: - Step

struct Step: Codable, Equatable, Identifiable {
    let id: String
    let instruction: String
    let targets: [TargetRect]
    let advance: Advance
    let safety: Safety?
}

// MARK: - TargetRect

struct TargetRect: Codable, Equatable {
    /// Normalized x (0..1), top-left origin
    let x: Double
    /// Normalized y (0..1), top-left origin
    let y: Double
    /// Normalized width (0..1)
    let w: Double
    /// Normalized height (0..1)
    let h: Double
    /// Model confidence (0..1), optional
    let confidence: Double?
    /// Human-readable label for the target
    let label: String?
}

// MARK: - Advance

struct Advance: Codable, Equatable {
    let type: AdvanceType
    let notes: String?
}

enum AdvanceType: String, Codable, Equatable {
    case clickInTarget = "click_in_target"
    case textEnteredOrNext = "text_entered_or_next"
    case manualNext = "manual_next"
    case waitForUIChange = "wait_for_ui_change"
}

// MARK: - Safety

struct Safety: Codable, Equatable {
    let requiresConfirmation: Bool?
    let riskLevel: RiskLevel?

    enum CodingKeys: String, CodingKey {
        case requiresConfirmation = "requires_confirmation"
        case riskLevel = "risk_level"
    }
}

enum RiskLevel: String, Codable, Equatable {
    case low, medium, high
}
