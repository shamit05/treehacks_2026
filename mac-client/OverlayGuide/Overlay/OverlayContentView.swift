// Overlay/OverlayContentView.swift
// Owner: Eng 1 (Overlay UI)
//
// SwiftUI view rendered inside each overlay NSPanel.
// Draws highlight rectangles and instruction bubbles for the current step.
// Stateless — reads everything from the state machine.

import SwiftUI

struct OverlayContentView: View {
    @ObservedObject var stateMachine: GuidanceStateMachine
    let screenBounds: CGRect

    var body: some View {
        ZStack {
            switch stateMachine.phase {
            case .idle:
                Color.clear
            case .inputGoal:
                Color.black.opacity(0.35)
                    .allowsHitTesting(false)
                GoalInputView(stateMachine: stateMachine)
            case .loading:
                Color.black.opacity(0.28)
                    .allowsHitTesting(false)
                LoadingOverlayCard()
            case .guiding:
                Color.black.opacity(0.2)
                    .allowsHitTesting(false)

                if let plan = stateMachine.currentPlan,
                   stateMachine.currentStepIndex < plan.steps.count {
                    let step = plan.steps[stateMachine.currentStepIndex]

                    ForEach(Array(step.targets.enumerated()), id: \.offset) { _, target in
                        HighlightRect(target: target, screenBounds: screenBounds)
                    }

                    InstructionBubble(
                        instruction: step.instruction,
                        stepNumber: stateMachine.currentStepIndex + 1,
                        totalSteps: plan.steps.count,
                        hintMessage: stateMachine.hintMessage
                    )
                }
            case .completed:
                Color.black.opacity(0.28)
                    .allowsHitTesting(false)
                CompletionView(stateMachine: stateMachine)
            case .error(let message):
                Color.black.opacity(0.35)
                    .allowsHitTesting(false)
                ErrorOverlayCard(message: message, stateMachine: stateMachine)
            }
        }
        .frame(width: screenBounds.width, height: screenBounds.height)
        .edgesIgnoringSafeArea(.all)
    }
}

// MARK: - HighlightRect

struct HighlightRect: View {
    let target: TargetRect
    let screenBounds: CGRect

    var body: some View {
        // Convert normalized coords to screen pixels
        let x = target.x * screenBounds.width
        let y = target.y * screenBounds.height
        let w = target.w * screenBounds.width
        let h = target.h * screenBounds.height

        RoundedRectangle(cornerRadius: 6)
            .fill(Color(red: 0.45, green: 0.75, blue: 1.0, opacity: 0.2))
            .overlay(
                RoundedRectangle(cornerRadius: 6)
                    .stroke(Color(red: 0.45, green: 0.75, blue: 1.0, opacity: 0.95), lineWidth: 2)
            )
            .frame(width: w, height: h)
            .position(x: x + w / 2, y: y + h / 2)
    }
}

// MARK: - InstructionBubble

struct InstructionBubble: View {
    let instruction: String
    let stepNumber: Int
    let totalSteps: Int
    let hintMessage: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Step \(stepNumber) of \(totalSteps)")
                    .font(.caption)
                    .foregroundColor(.secondary)
                Spacer()
            }

            Text(instruction)
                .font(.body)
                .foregroundColor(.primary)
                .fixedSize(horizontal: false, vertical: true)

            if let hintMessage {
                Text(hintMessage)
                    .font(.caption)
                    .foregroundColor(.orange)
            }
        }
        .padding(14)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 12))
        .frame(maxWidth: 560)
        .padding(.horizontal, 20)
        .padding(.bottom, 24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
    }
}

struct LoadingOverlayCard: View {
    var body: some View {
        VStack(spacing: 12) {
            ProgressView()
                .progressViewStyle(.circular)
            Text("Thinking...")
                .font(.headline)
        }
        .padding(24)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 14))
    }
}

struct ErrorOverlayCard: View {
    let message: String
    @ObservedObject var stateMachine: GuidanceStateMachine

    var body: some View {
        VStack(spacing: 12) {
            Text("Something went wrong")
                .font(.headline)
            Text(message)
                .font(.subheadline)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
            Button("Try Again") {
                stateMachine.reset()
                stateMachine.toggleOverlay()
            }
            .buttonStyle(.borderedProminent)
        }
        .padding(24)
        .frame(maxWidth: 420)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 14))
    }
}
