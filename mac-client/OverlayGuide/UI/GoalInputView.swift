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

    var body: some View {
        VStack(spacing: 12) {
            Capsule()
                .fill(Color.secondary.opacity(0.5))
                .frame(width: 36, height: 5)
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

            Text("What do you need help with?")
                .font(.title2)
                .fontWeight(.semibold)
                .foregroundColor(.primary)

            HStack {
                TextField("e.g. Explain to me how to use the crop feature", text: $goalText)
                    .textFieldStyle(.plain)
                    .font(.body)
                    .foregroundColor(.primary)
                    .padding(12)
                    .background(Color.primary.opacity(0.08))
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
        .padding(.horizontal, 18)
        .padding(.bottom, 18)
        .padding(.top, 10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 20))
        .frame(maxWidth: 500)
        .offset(dragOffset)
        .onAppear {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                isFocused = true
            }
        }
    }
}
