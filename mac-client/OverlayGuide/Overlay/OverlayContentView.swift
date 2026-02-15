// Overlay/OverlayContentView.swift
// Owner: Eng 1 (Overlay UI)
//
// SwiftUI content rendered inside the popup overlay window.
// UNIFIED SINGLE-PANEL DESIGN: one persistent container that smoothly
// transitions between input, loading, guiding, and completed states.
// No separate popups or windows — everything renders in the same bubble.

import AppKit
import SwiftUI

// Notification posted when user taps the gear icon to open preferences
extension Notification.Name {
    static let openCookbookPreferences = Notification.Name("openCookbookPreferences")
}

// MARK: - Main Content View (unified panel)

struct OverlayContentView: View {
    @ObservedObject var stateMachine: GuidanceStateMachine
    @State private var goalText: String = ""
    @StateObject private var voiceInput = AppleVoiceInputService()
    @FocusState private var isFocused: Bool
    @State private var voiceError: String?

    private var isCompleted: Bool {
        if case .completed = stateMachine.phase { return true }
        return false
    }

    var body: some View {
        VStack(spacing: 0) {
            // ── Header: input field or submitted goal ──
            headerSection

            // ── Content: only shown after submission ──
            if stateMachine.phase != .inputGoal && stateMachine.phase != .idle {
                Divider()
                    .overlay(Color.secondary.opacity(0.25))

                contentSection
                    .transition(.opacity.combined(with: .move(edge: .bottom)))
            }
        }
        .animation(.easeInOut(duration: 0.25), value: stateMachine.phase)
        .background(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(Color(nsColor: .windowBackgroundColor))
        )
        .overlay(
            // Green tint overlay for completed state — drawn on top of opaque bg
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(isCompleted ? Color.green.opacity(0.12) : Color.clear)
                .allowsHitTesting(false)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(isCompleted
                    ? Color.green.opacity(0.4)
                    : Color.secondary.opacity(0.25), lineWidth: 1)
                .allowsHitTesting(false)
        )
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        .shadow(color: .black.opacity(0.15), radius: 12, y: 4)
        .padding(8)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .onExitCommand {
            stateMachine.reset()
        }
        .onChange(of: stateMachine.phase) { newPhase in
            if newPhase == .inputGoal {
                goalText = ""
                voiceError = nil
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                    isFocused = true
                }
            } else {
                // Stop voice input when leaving inputGoal phase
                voiceInput.stopListening()
            }
        }
        .onReceive(voiceInput.$transcript) { newTranscript in
            guard case .inputGoal = stateMachine.phase else { return }
            guard !newTranscript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
            goalText = newTranscript
        }
    }

    // MARK: - Header

    @ViewBuilder
    private var headerSection: some View {
        switch stateMachine.phase {
        case .idle:
            EmptyView()

        case .inputGoal:
            // Editable text field with voice input
            VStack(spacing: 0) {
                HStack(spacing: 10) {
                    Image(systemName: "magnifyingglass")
                        .font(.system(size: 15, weight: .medium))
                        .foregroundStyle(
                            LinearGradient(colors: [.blue, .purple], startPoint: .topLeading, endPoint: .bottomTrailing)
                        )

                    TextField("Help me with ...", text: $goalText)
                        .textFieldStyle(.plain)
                        .font(.system(size: 16, weight: .regular))
                        .foregroundColor(.primary)
                        .focused($isFocused)
                        .onSubmit { submitGoal() }

                    Button(action: toggleVoiceInput) {
                        Image(systemName: voiceInput.isListening ? "mic.fill" : "mic")
                            .font(.system(size: 14, weight: .semibold))
                    }
                    .buttonStyle(.plain)
                    .foregroundColor(voiceInput.isListening ? .red : .secondary)
                    .help(voiceInput.isListening ? "Stop voice input" : "Start voice input")
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 11)

                if let voiceError {
                    Text(voiceError)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundColor(.red)
                        .padding(.horizontal, 14)
                        .padding(.bottom, 6)
                }
            }
            .onAppear {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                    isFocused = true
                }
            }

        default:
            // Show submitted goal as header with dismiss button
            HStack(spacing: 10) {
                if case .completed = stateMachine.phase {
                    Image(systemName: "checkmark.circle.fill")
                        .font(.system(size: 15, weight: .medium))
                        .foregroundColor(.green)
                } else {
                    Image(systemName: "magnifyingglass")
                        .font(.system(size: 15, weight: .medium))
                        .foregroundStyle(
                            LinearGradient(colors: [.blue, .purple], startPoint: .topLeading, endPoint: .bottomTrailing)
                        )
                }

                Text(stateMachine.submittedGoal ?? "")
                    .font(.system(size: 15, weight: .medium))
                    .foregroundColor(.primary)
                    .lineLimit(1)
                    .truncationMode(.tail)

                Spacer()

                Button(action: { stateMachine.reset() }) {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 14))
                        .foregroundColor(.secondary.opacity(0.6))
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
        }
    }

    // MARK: - Content (below header)

    @ViewBuilder
    private var contentSection: some View {
        switch stateMachine.phase {

        case .loading:
            loadingContent
                .padding(.horizontal, 14)
                .padding(.vertical, 10)

        case .guiding:
            guidingContent
                .padding(.horizontal, 14)
                .padding(.vertical, 10)

        case .completed:
            completedContent
                .padding(.horizontal, 14)
                .padding(.vertical, 10)

        case .error(let message):
            errorContent(message: message)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)

        default:
            EmptyView()
        }
    }

    // MARK: - Loading State

    private var loadingContent: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                ProgressView()
                    .progressViewStyle(.circular)
                    .scaleEffect(0.65)
                    .tint(.blue)

                Text(stateMachine.loadingStatus)
                    .font(.callout)
                    .foregroundColor(.secondary)
                    .animation(.easeInOut(duration: 0.2), value: stateMachine.loadingStatus)
            }

            // Show streaming instruction prominently when it arrives
            if let instruction = stateMachine.streamingInstruction {
                Text(instruction)
                    .font(.system(size: 14, weight: .regular))
                    .foregroundColor(.primary)
                    .fixedSize(horizontal: false, vertical: true)
                    .transition(.opacity.combined(with: .move(edge: .top)))
                    .animation(.easeInOut(duration: 0.3), value: stateMachine.streamingInstruction)
            }
        }
    }

    // MARK: - Guiding State (step list)

    private var guidingContent: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let plan = stateMachine.currentPlan {
                ForEach(Array(plan.steps.enumerated()), id: \.offset) { index, step in
                    HStack(alignment: .top, spacing: 8) {
                        let stepNum = stateMachine.completedSteps.count + index + 1
                        let isActive = index == 0

                        ZStack {
                            Circle()
                                .fill(isActive ? Color.blue : Color.gray.opacity(0.25))
                                .frame(width: 22, height: 22)
                            Text("\(stepNum)")
                                .font(.caption2.bold())
                                .foregroundColor(isActive ? .white : .secondary)
                        }

                        VStack(alignment: .leading, spacing: 2) {
                            Text(step.instruction)
                                .font(.system(size: 13, weight: isActive ? .medium : .regular))
                                .foregroundColor(isActive ? .primary : .secondary)
                            if let label = step.targets.first?.label, !label.isEmpty {
                                Text(label)
                                    .font(.caption)
                                    .foregroundColor(.blue.opacity(0.8))
                            }
                        }
                    }
                }

                if let hint = stateMachine.hintMessage {
                    Text(hint)
                        .font(.caption)
                        .foregroundColor(.orange)
                        .padding(.top, 2)
                }
            } else {
                HStack(spacing: 8) {
                    ProgressView()
                        .progressViewStyle(.circular)
                        .scaleEffect(0.6)
                        .tint(.blue)
                    Text("Preparing next step...")
                        .font(.callout)
                        .foregroundColor(.secondary)
                }
            }
        }
    }

    // MARK: - Completed State

    private var completedContent: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 18))
                    .foregroundColor(.green)

                VStack(alignment: .leading, spacing: 2) {
                    Text(stateMachine.completionMessage ?? "All done!")
                        .font(.system(size: 14, weight: .medium))
                        .foregroundColor(.primary)

                    Text("\(stateMachine.completedSteps.count) steps completed")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
            }

            HStack(spacing: 10) {
                Button("New Task") {
                    stateMachine.showInputOverlay()
                }
                .buttonStyle(.bordered)
                .controlSize(.small)

                Button("Dismiss") {
                    stateMachine.reset()
                }
                .buttonStyle(.borderedProminent)
                .tint(.green)
                .controlSize(.small)
            }
            .padding(.top, 2)
        }
    }

    // MARK: - Error State

    private func errorContent(message: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundColor(.orange)
                    .font(.body)

                VStack(alignment: .leading, spacing: 2) {
                    Text("Something went wrong")
                        .font(.callout)
                        .foregroundColor(.primary)
                    Text(message)
                        .font(.caption)
                        .foregroundColor(.secondary)
                        .lineLimit(2)
                }
            }

            HStack(spacing: 10) {
                Button("Retry") {
                    // Re-submit the same goal
                    if let goal = stateMachine.submittedGoal {
                        stateMachine.showInputOverlay()
                        goalText = goal
                        // Auto-submit after brief delay for overlay to appear
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                            stateMachine.submitGoal(goal)
                        }
                    }
                }
                .buttonStyle(.bordered)
                .controlSize(.small)

                Button("Dismiss") {
                    stateMachine.reset()
                }
                .buttonStyle(.plain)
                .controlSize(.small)
                .foregroundColor(.secondary)
            }
        }
    }

    // MARK: - Actions

    private func submitGoal() {
        let trimmed = goalText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        voiceInput.stopListening()
        stateMachine.submitGoal(trimmed)
    }

    private func toggleVoiceInput() {
        voiceError = nil
        if voiceInput.isListening {
            voiceInput.stopListening()
            return
        }
        Task { @MainActor in
            let granted = await voiceInput.requestPermissions()
            guard granted else {
                voiceError = "Enable microphone + speech permissions in System Settings."
                return
            }
            do {
                try voiceInput.startListening()
            } catch {
                voiceError = error.localizedDescription
            }
        }
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
