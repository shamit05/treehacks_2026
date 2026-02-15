// Overlay/OverlayContentView.swift
// Owner: Eng 1 (Overlay UI)
//
// SwiftUI content rendered inside the popup overlay window.
// All states (loading, guiding, error, completed) render in
// the same compact bubble — no separate popup windows.

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
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .loading:
                // Compact inline pill — same visual style as the instruction bubble
                StatusBubble(
                    icon: "circle.dotted",
                    iconColor: .blue,
                    title: stateMachine.loadingStatus,
                    subtitle: nil,
                    showSpinner: true
                )
            case .guiding:
                if let plan = stateMachine.currentPlan,
                   stateMachine.currentStepIndex < plan.steps.count {
                    let step = plan.steps[stateMachine.currentStepIndex]
                    InstructionBubble(
                        instruction: step.instruction,
                        stepNumber: stateMachine.completedSteps.count + 1,
                        totalSteps: stateMachine.completedSteps.count + plan.steps.count,
                        hintMessage: stateMachine.hintMessage
                    )
                } else {
                    StatusBubble(
                        icon: "ellipsis.circle",
                        iconColor: .secondary,
                        title: "Preparing next step...",
                        subtitle: nil,
                        showSpinner: true
                    )
                }
            case .completed:
                CompletionView(stateMachine: stateMachine)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .error(let message):
                StatusBubble(
                    icon: "exclamationmark.triangle.fill",
                    iconColor: .orange,
                    title: "Something went wrong",
                    subtitle: message,
                    showSpinner: false
                )
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.clear)
    }
}

// MARK: - StatusBubble (reusable for loading, error, waiting states)

/// A compact status bubble that matches the InstructionBubble style.
/// Used for loading, errors, and waiting states so everything appears
/// in the same consistent bubble — no separate popup windows.
private struct StatusBubble: View {
    let icon: String
    let iconColor: Color
    let title: String
    let subtitle: String?
    let showSpinner: Bool

    var body: some View {
        HStack(spacing: 10) {
            if showSpinner {
                ProgressView()
                    .progressViewStyle(.circular)
                    .scaleEffect(0.7)
                    .tint(.blue)
            } else {
                Image(systemName: icon)
                    .foregroundColor(iconColor)
                    .font(.body)
            }

            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.callout)
                    .foregroundColor(.primary)
                    .animation(.easeInOut(duration: 0.3), value: title)
                if let subtitle {
                    Text(subtitle)
                        .font(.caption)
                        .foregroundColor(.secondary)
                        .lineLimit(2)
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 12))
        .shadow(color: .black.opacity(0.1), radius: 8, y: 2)
        .frame(maxWidth: 340)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .padding(12)
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
            } else if case .loading = stateMachine.phase,
                      let plan = stateMachine.currentPlan,
                      stateMachine.currentStepIndex < plan.steps.count {
                // During loading (between steps), gray out the previous target
                let step = plan.steps[stateMachine.currentStepIndex]
                HighlightOverlayCanvas(targets: step.targets, screenBounds: screenBounds, grayed: true)
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
    var grayed: Bool = false

    func makeNSView(context: Context) -> HighlightOverlayNSView {
        let view = HighlightOverlayNSView()
        view.wantsLayer = true
        view.layer?.backgroundColor = NSColor.clear.cgColor
        return view
    }

    func updateNSView(_ nsView: HighlightOverlayNSView, context: Context) {
        nsView.screenBounds = screenBounds
        nsView.targets = targets
        nsView.grayed = grayed
    }
}

private final class HighlightOverlayNSView: NSView {
    var targets: [TargetRect] = [] {
        didSet { needsDisplay = true }
    }
    var screenBounds: CGRect = .zero {
        didSet { needsDisplay = true }
    }
    var grayed: Bool = false {
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

            if grayed {
                NSColor.systemGray.withAlphaComponent(0.2).setFill()
                path.fill()
                NSColor.systemGray.withAlphaComponent(0.6).setStroke()
            } else {
                NSColor.systemBlue.withAlphaComponent(0.16).setFill()
                path.fill()
                NSColor.systemBlue.withAlphaComponent(0.95).setStroke()
            }
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
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Step \(stepNumber) of \(totalSteps)")
                    .font(.caption)
                    .foregroundColor(.secondary)
                Spacer()
            }

            Text(instruction)
                .font(.callout)
                .foregroundColor(.primary)
                .fixedSize(horizontal: false, vertical: true)

            if let hintMessage {
                Text(hintMessage)
                    .font(.caption)
                    .foregroundColor(.orange)
            }
        }
        .padding(12)
        .frame(maxWidth: 340)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 12))
        .shadow(color: .black.opacity(0.15), radius: 10, y: 3)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .padding(12)
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
