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
            // Semi-transparent background
            Color.black.opacity(0.4)
                .allowsHitTesting(false)

            if let plan = stateMachine.currentPlan,
               stateMachine.currentStepIndex < plan.steps.count {
                let step = plan.steps[stateMachine.currentStepIndex]

                // Draw highlight rects (cut-outs)
                ForEach(Array(step.targets.enumerated()), id: \.offset) { _, target in
                    HighlightRect(target: target, screenBounds: screenBounds)
                }

                // Instruction bubble
                InstructionBubble(
                    instruction: step.instruction,
                    stepNumber: stateMachine.currentStepIndex + 1,
                    totalSteps: plan.steps.count
                )
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

        RoundedRectangle(cornerRadius: 8)
            .stroke(Color.blue, lineWidth: 3)
            .background(Color.white.opacity(0.1))
            .frame(width: w, height: h)
            .position(x: x + w / 2, y: y + h / 2)
    }
}

// MARK: - InstructionBubble

struct InstructionBubble: View {
    let instruction: String
    let stepNumber: Int
    let totalSteps: Int

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Step \(stepNumber) of \(totalSteps)")
                .font(.caption)
                .foregroundColor(.secondary)

            Text(instruction)
                .font(.body)
                .foregroundColor(.primary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding()
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 12))
        .frame(maxWidth: 360)
        .padding(.bottom, 80)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
    }
}
