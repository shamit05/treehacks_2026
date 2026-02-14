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

    var body: some View {
        VStack(spacing: 16) {
            Text("What do you need help with?")
                .font(.title2)
                .fontWeight(.semibold)
                .foregroundColor(.white)

            HStack {
                TextField("e.g. Create a new calendar event for tomorrow at 3pm", text: $goalText)
                    .textFieldStyle(.plain)
                    .font(.body)
                    .foregroundColor(.white)
                    .padding(12)
                    .background(Color.white.opacity(0.15))
                    .cornerRadius(10)
                    .focused($isFocused)
                    .onSubmit {
                        guard !goalText.trimmingCharacters(in: .whitespaces).isEmpty else { return }
                        stateMachine.submitGoal(goalText)
                    }

                Button(action: {
                    guard !goalText.trimmingCharacters(in: .whitespaces).isEmpty else { return }
                    stateMachine.submitGoal(goalText)
                }) {
                    Image(systemName: "arrow.right.circle.fill")
                        .font(.title2)
                        .foregroundColor(.blue)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(24)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))
        .frame(maxWidth: 500)
        .onAppear { isFocused = true }
    }
}
