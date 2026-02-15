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
                if let plan = stateMachine.currentPlan {
                    PlanView(plan: plan, completedCount: stateMachine.completedSteps.count, hintMessage: stateMachine.hintMessage, onDismiss: { stateMachine.reset() })
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

    // Use top-left origin — matches our normalized coordinate system (top-left = 0,0).
    // This eliminates the confusing Y-flip in CoordinateMapper.
    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.clear.setFill()
        dirtyRect.fill()
        guard !targets.isEmpty, screenBounds.width > 0, screenBounds.height > 0 else { return }

        let viewW = bounds.width
        let viewH = bounds.height

        for target in targets {
            guard target.type == .bboxNorm,
                  let tx = target.x, let ty = target.y,
                  let tw = target.w, let th = target.h else { continue }

            // Direct mapping: normalized [0,1] → view-local pixels (both top-left origin)
            let x = CGFloat(tx) * viewW
            let y = CGFloat(ty) * viewH
            let w = CGFloat(tw) * viewW
            let h = CGFloat(th) * viewH
            let localRect = CGRect(x: x, y: y, width: w, height: h)

            // Outer glow (larger, semi-transparent)
            let glowInset: CGFloat = -4
            let glowRect = localRect.insetBy(dx: glowInset, dy: glowInset)
            let glowPath = NSBezierPath(roundedRect: glowRect, xRadius: 12, yRadius: 12)

            if grayed {
                NSColor.systemGray.withAlphaComponent(0.10).setFill()
                glowPath.fill()
            } else {
                NSColor.systemBlue.withAlphaComponent(0.08).setFill()
                glowPath.fill()
            }

            // Main highlight rectangle
            let path = NSBezierPath(roundedRect: localRect, xRadius: 8, yRadius: 8)

            if grayed {
                NSColor.systemGray.withAlphaComponent(0.15).setFill()
                path.fill()
                NSColor.systemGray.withAlphaComponent(0.5).setStroke()
                path.lineWidth = 2.0
            } else {
                NSColor.systemBlue.withAlphaComponent(0.12).setFill()
                path.fill()
                NSColor.systemBlue.withAlphaComponent(0.95).setStroke()
                path.lineWidth = 3.0
            }
            path.stroke()

            // Draw label badge (if available and not grayed)
            if !grayed, let label = target.label, !label.isEmpty {
                drawLabelBadge(label, above: localRect)
            }
        }
    }

    private func drawLabelBadge(_ label: String, above rect: CGRect) {
        let font = NSFont.systemFont(ofSize: 11, weight: .medium)
        let attrs: [NSAttributedString.Key: Any] = [
            .font: font,
            .foregroundColor: NSColor.white,
        ]
        let textSize = (label as NSString).size(withAttributes: attrs)
        let badgePadH: CGFloat = 8
        let badgePadV: CGFloat = 4
        let badgeW = textSize.width + badgePadH * 2
        let badgeH = textSize.height + badgePadV * 2

        // Position badge above the target rect, left-aligned
        let badgeX = rect.minX
        let badgeY = rect.minY - badgeH - 4
        let badgeRect = CGRect(x: badgeX, y: max(0, badgeY), width: badgeW, height: badgeH)

        let badgePath = NSBezierPath(roundedRect: badgeRect, xRadius: 6, yRadius: 6)
        NSColor.systemBlue.withAlphaComponent(0.9).setFill()
        badgePath.fill()

        let textPoint = CGPoint(x: badgeRect.minX + badgePadH, y: badgeRect.minY + badgePadV)
        (label as NSString).draw(at: textPoint, withAttributes: attrs)
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

// MARK: - PlanView (shows all steps as a text list)

struct PlanView: View {
    let plan: StepPlan
    let completedCount: Int
    let hintMessage: String?
    let onDismiss: () -> Void
    @State private var dragOffset: CGSize = .zero
    @State private var dragStartOffset: CGSize = .zero

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // Header
            HStack {
                Image(systemName: "list.bullet.rectangle")
                    .foregroundColor(.blue)
                Text(plan.goal)
                    .font(.headline)
                    .foregroundColor(.primary)
                    .lineLimit(2)
                Spacer()
                Button(action: onDismiss) {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundColor(.secondary)
                }
                .buttonStyle(.plain)
            }

            Divider()

            // Steps
            ForEach(Array(plan.steps.enumerated()), id: \.offset) { index, step in
                HStack(alignment: .top, spacing: 10) {
                    // Step number circle
                    let stepNum = completedCount + index + 1
                    let isFirst = index == 0
                    ZStack {
                        Circle()
                            .fill(isFirst ? Color.blue : Color.gray.opacity(0.3))
                            .frame(width: 24, height: 24)
                        Text("\(stepNum)")
                            .font(.caption2.bold())
                            .foregroundColor(isFirst ? .white : .secondary)
                    }

                    VStack(alignment: .leading, spacing: 2) {
                        Text(step.instruction)
                            .font(.callout)
                            .foregroundColor(isFirst ? .primary : .secondary)
                        if let label = step.targets.first?.label, !label.isEmpty {
                            Text(label)
                                .font(.caption)
                                .foregroundColor(.blue)
                        }
                    }
                }
            }

            if let hint = hintMessage {
                Text(hint)
                    .font(.caption)
                    .foregroundColor(.orange)
                    .padding(.top, 4)
            }
        }
        .padding(16)
        .frame(maxWidth: 400)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 14))
        .shadow(color: .black.opacity(0.15), radius: 12, y: 4)
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
