// UI/GoalInputView.swift
// Owner: Eng 1 (Overlay UI)
//
// SwiftUI view for the goal text input that appears when the overlay is activated.
// User types their goal and presses Enter to submit.

import SwiftUI

struct GoalInputView: View {
    @ObservedObject var stateMachine: GuidanceStateMachine
    @State private var goalText: String = ""
    @StateObject private var voiceInput = AppleVoiceInputService()
    @FocusState private var isFocused: Bool
    @State private var dragOffset: CGSize = .zero
    @State private var dragStartOffset: CGSize = .zero
    @State private var voiceError: String?

    var body: some View {
        VStack(spacing: 8) {
            VStack(spacing: 0) {
                HStack(spacing: 12) {
                    Image(systemName: "magnifyingglass")
                        .font(.system(size: 16, weight: .medium))
                        .foregroundColor(.secondary)

                    TextField("Help me with ...", text: $goalText)
                        .textFieldStyle(.plain)
                        .font(.system(size: 18, weight: .regular))
                        .foregroundColor(.primary)
                        .focused($isFocused)
                        .onSubmit { submitGoal() }

                    Button(action: toggleVoiceInput) {
                        Image(systemName: voiceInput.isListening ? "mic.fill" : "mic")
                            .font(.system(size: 15, weight: .semibold))
                    }
                    .buttonStyle(.plain)
                    .foregroundColor(voiceInput.isListening ? .red : .secondary)
                    .help(voiceInput.isListening ? "Stop voice input" : "Start voice input")
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

                Divider()
                    .overlay(Color.secondary.opacity(0.35))

                HStack(spacing: 8) {
                    Spacer()
                    if let voiceError {
                        Text(voiceError)
                            .font(.system(size: 12, weight: .medium))
                            .foregroundColor(.red)
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
            stateMachine.reset()
        }
        .onAppear {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                isFocused = true
            }
        }
        .onDisappear {
            voiceInput.stopListening()
        }
        .onReceive(voiceInput.$transcript) { transcript in
            guard !transcript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
            goalText = transcript
        }
    }

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
