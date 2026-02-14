// Overlay/OverlayContentView.swift
// Owner: Eng 1 (Overlay UI)
//
// SwiftUI view rendered inside each overlay NSPanel.
// Draws highlight rectangles and instruction bubbles for the current step.
// Stateless — reads everything from the state machine.

import SwiftUI

struct OverlayContentView: View {
    @ObservedObject var stateMachine: GuidanceStateMachine

    var body: some View {
        Group {
            switch stateMachine.phase {
            case .idle:
                EmptyView()
            case .inputGoal:
                GoalInputView(stateMachine: stateMachine)
            case .loading:
                VStack(spacing: 12) {
                    ProgressView()
                        .progressViewStyle(.circular)
                        .scaleEffect(1.2)
                        .tint(.blue)
                    Text("Taking screenshot and generating plan...")
                        .font(.headline)
                        .foregroundColor(.primary)
                    Text("This may take a few seconds.")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                .padding(20)
                .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16))
                .frame(maxWidth: 500)
            case .guiding:
                if let plan = stateMachine.currentPlan,
                   stateMachine.currentStepIndex < plan.steps.count {
                    let step = plan.steps[stateMachine.currentStepIndex]
                    InstructionBubble(
                        instruction: step.instruction,
                        stepNumber: stateMachine.currentStepIndex + 1,
                        totalSteps: plan.steps.count
                    )
                } else {
                    VStack(spacing: 8) {
                        Text("Waiting for plan...")
                            .foregroundColor(.primary)
                            .font(.headline)
                        Text("Preparing next guidance step.")
                            .foregroundColor(.secondary)
                            .font(.caption)
                    }
                    .padding(20)
                    .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16))
                    .frame(maxWidth: 500)
                }
            case .completed:
                CompletionView(stateMachine: stateMachine)
            case .error(let message):
                VStack(spacing: 12) {
                    Text("Error")
                        .font(.headline)
                        .foregroundColor(.primary)
                    Text(message)
                        .font(.body)
                        .foregroundColor(.primary)
                        .multilineTextAlignment(.center)
                }
                .padding(20)
                .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16))
                .frame(maxWidth: 500)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.clear)
    }
}

// MARK: - InstructionBubble

struct InstructionBubble: View {
    let instruction: String
    let stepNumber: Int
    let totalSteps: Int
    @State private var dragOffset: CGSize = .zero
    @State private var dragStartOffset: CGSize = .zero

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
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 12))
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .offset(dragOffset)
        .gesture(
            DragGesture()
                .onChanged { value in
                    dragOffset = CGSize(
                        width: dragStartOffset.width + value.translation.width,
                        height: dragStartOffset.height + value.translation.height
                    )
                }
                .onEnded { _ in
                    dragStartOffset = dragOffset
                }
        )
    }
}
