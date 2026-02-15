// Overlay/OverlayContentView.swift
// Owner: Eng 1 (Overlay UI)
//
// SwiftUI content rendered inside the popup overlay window.

import AppKit
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
        Group {
            if case .guiding = stateMachine.phase,
               let plan = stateMachine.currentPlan,
               stateMachine.currentStepIndex < plan.steps.count {
                let step = plan.steps[stateMachine.currentStepIndex]
                HighlightOverlayCanvas(targets: step.targets, screenBounds: screenBounds)
            } else {
                Color.clear
            }
        }
        .frame(width: screenBounds.width, height: screenBounds.height)
        .background(Color.clear)
        .allowsHitTesting(false)
    }
}

private struct HighlightOverlayCanvas: NSViewRepresentable {
    let targets: [TargetRect]
    let screenBounds: CGRect

    func makeNSView(context: Context) -> HighlightOverlayNSView {
        let view = HighlightOverlayNSView()
        view.wantsLayer = true
        view.layer?.backgroundColor = NSColor.clear.cgColor
        return view
    }

    func updateNSView(_ nsView: HighlightOverlayNSView, context: Context) {
        nsView.screenBounds = screenBounds
        nsView.targets = targets
    }
}

private final class HighlightOverlayNSView: NSView {
    var targets: [TargetRect] = [] {
        didSet { needsDisplay = true }
    }
    var screenBounds: CGRect = .zero {
        didSet { needsDisplay = true }
    }

    override var isOpaque: Bool { false }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.clear.setFill()
        dirtyRect.fill()
        guard !targets.isEmpty, screenBounds.width > 0, screenBounds.height > 0 else { return }

        let mapper = CoordinateMapper(screenBounds: screenBounds, scaleFactor: 1.0)

        for target in targets {
            guard let screenRect = mapper.normalizedToScreen(target) else { continue }
            let localRect = screenRect.offsetBy(dx: -screenBounds.origin.x, dy: -screenBounds.origin.y)

            let path = NSBezierPath(roundedRect: localRect, xRadius: 8, yRadius: 8)

            NSColor.systemBlue.withAlphaComponent(0.16).setFill()
            path.fill()

            NSColor.systemBlue.withAlphaComponent(0.95).setStroke()
            path.lineWidth = 2.5
            path.stroke()
        }
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
