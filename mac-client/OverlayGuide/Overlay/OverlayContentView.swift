// Overlay/OverlayContentView.swift
// Owner: Eng 1 (Overlay UI)
//
// SwiftUI content rendered inside the popup overlay window.

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
                        totalSteps: plan.steps.count,
                        hintMessage: stateMachine.hintMessage
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

// MARK: - Full-Screen Highlight Overlay

struct HighlightOverlayView: View {
    @ObservedObject var stateMachine: GuidanceStateMachine
    let screenBounds: CGRect

    var body: some View {
        ZStack {
            if case .guiding = stateMachine.phase,
               let plan = stateMachine.currentPlan,
               stateMachine.currentStepIndex < plan.steps.count {
                let step = plan.steps[stateMachine.currentStepIndex]
                ForEach(Array(step.targets.enumerated()), id: \.offset) { _, target in
                    HighlightRectOnScreen(target: target, screenBounds: screenBounds)
                }
            }
        }
        .frame(width: screenBounds.width, height: screenBounds.height)
        .background(Color.clear)
        .allowsHitTesting(false)
    }
}

struct HighlightRectOnScreen: View {
    let target: TargetRect
    let screenBounds: CGRect

    var body: some View {
        let x = target.x * screenBounds.width
        let y = target.y * screenBounds.height
        let w = target.w * screenBounds.width
        let h = target.h * screenBounds.height

        RoundedRectangle(cornerRadius: 8)
            .fill(Color.blue.opacity(0.16))
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .stroke(Color.blue.opacity(0.95), lineWidth: 2.5)
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
    @State private var dragOffset: CGSize = .zero
    @State private var dragStartOffset: CGSize = .zero

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
