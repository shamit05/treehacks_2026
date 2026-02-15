// UI/GoalInputView.swift
// Owner: Eng 1 (Overlay UI)
//
// SwiftUI view for the goal text input that appears when the overlay is activated.
// User types their goal and presses Enter to submit.

import SwiftUI

struct GoalInputView: View {
    @ObservedObject var stateMachine: GuidanceStateMachine
    @State private var goalText: String = ""
    @FocusState private var isFocused: Bool
    @State private var dragOffset: CGSize = .zero
    @State private var dragStartOffset: CGSize = .zero

    private var isVoiceAvailable: Bool {
        stateMachine.voiceMode != .disabled
    }

    var body: some View {
        VStack(spacing: 8) {
            VStack(spacing: 0) {
                HStack(spacing: 12) {
                    Image(systemName: "magnifyingglass")
                        .font(.system(size: 16, weight: .medium))
                        .foregroundColor(.secondary)

                    if stateMachine.isVoiceListening {
                        // Voice listening indicator (replaces text field)
                        VoiceListeningIndicator()
                    } else {
                        TextField("Help me with ...", text: $goalText)
                            .textFieldStyle(.plain)
                            .font(.system(size: 18, weight: .regular))
                            .foregroundColor(.primary)
                            .focused($isFocused)
                            .onSubmit { submitGoal() }
                            .disabled(stateMachine.isVoiceListening)
                    }

                    // Microphone button (only when voice is available)
                    if isVoiceAvailable {
                        Button(action: toggleVoice) {
                            Image(systemName: stateMachine.isVoiceListening
                                ? "mic.fill"
                                : "mic")
                                .font(.system(size: 18, weight: .medium))
                                .foregroundColor(
                                    stateMachine.isVoiceListening
                                        ? .red
                                        : .secondary
                                )
                                .frame(width: 32, height: 32)
                                .background(
                                    Circle()
                                        .fill(
                                            stateMachine.isVoiceListening
                                                ? Color.red.opacity(0.15)
                                                : Color.clear
                                        )
                                )
                        }
                        .buttonStyle(.plain)
                        .help(stateMachine.isVoiceListening
                            ? "Stop listening"
                            : "Use voice input")
                    }
                }
                .padding(.horizontal, 18)
                .padding(.vertical, 12)
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

                // Voice status message
                if let voiceStatus = stateMachine.voiceStatusMessage {
                    Text(voiceStatus)
                        .font(.system(size: 13, weight: .medium))
                        .foregroundColor(.blue)
                        .padding(.horizontal, 18)
                        .padding(.vertical, 6)
                }

                Divider()
                    .overlay(Color.secondary.opacity(0.35))

                HStack(spacing: 8) {
                    Spacer()
                    if isVoiceAvailable && !stateMachine.isVoiceListening {
                        Image(systemName: "mic")
                            .font(.system(size: 11, weight: .medium))
                            .foregroundColor(.secondary)
                        Text("voice")
                            .font(.system(size: 13, weight: .medium))
                            .foregroundColor(.secondary)
                            .padding(.trailing, 6)
                    }
                    Keycap(text: "↩")
                    Text("to submit")
                        .font(.system(size: 13, weight: .medium))
                        .foregroundColor(.secondary)
                    Keycap(text: "esc")
                        .padding(.leading, 10)
                    Text("to cancel")
                        .font(.system(size: 13, weight: .medium))
                        .foregroundColor(.secondary)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
            }
        }
        .background(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(Color(nsColor: .windowBackgroundColor).opacity(0.94))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .stroke(Color.secondary.opacity(0.35), lineWidth: 1)
        )
        .padding(.horizontal, 6)
        .padding(.top, 4)
        .frame(maxWidth: 900)
        .offset(dragOffset)
        .onExitCommand {
            if stateMachine.isVoiceListening {
                stateMachine.stopVoiceListening()
            } else {
                stateMachine.reset()
            }
        }
        .onAppear {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                isFocused = true
            }
        }
    }

    private func submitGoal() {
        let trimmed = goalText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        stateMachine.submitGoal(trimmed)
    }

    private func toggleVoice() {
        if stateMachine.isVoiceListening {
            stateMachine.stopVoiceListening()
        } else {
            Task {
                await stateMachine.submitVoiceGoal()
            }
        }
    }
}

// MARK: - Voice Listening Indicator

private struct VoiceListeningIndicator: View {
    @State private var animationPhase: CGFloat = 0

    var body: some View {
        HStack(spacing: 4) {
            ForEach(0..<5, id: \.self) { index in
                Capsule()
                    .fill(Color.red.opacity(0.7))
                    .frame(
                        width: 3,
                        height: 8 + 12 * abs(sin(animationPhase + Double(index) * 0.6))
                    )
            }
            Text("Listening...")
                .font(.system(size: 18, weight: .regular))
                .foregroundColor(.primary)
                .padding(.leading, 8)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .onAppear {
            withAnimation(.easeInOut(duration: 0.6).repeatForever(autoreverses: true)) {
                animationPhase = .pi
            }
        }
    }
}

private struct Keycap: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.system(size: 12, weight: .semibold))
            .foregroundColor(.secondary)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(Color.secondary.opacity(0.18))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .stroke(Color.secondary.opacity(0.35), lineWidth: 1)
            )
    }
}
