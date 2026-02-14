// UI/CompletionView.swift
// Owner: Eng 1 (Overlay UI)
//
// Shown when all steps are completed. Displays a success message
// and a button to dismiss the overlay.

import SwiftUI

struct CompletionView: View {
    @ObservedObject var stateMachine: GuidanceStateMachine

    var body: some View {
        VStack(spacing: 20) {
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 48))
                .foregroundColor(.green)

            Text("All done!")
                .font(.title)
                .fontWeight(.bold)
                .foregroundColor(.white)

            if let plan = stateMachine.currentPlan {
                Text(plan.goal)
                    .font(.body)
                    .foregroundColor(.secondary)
            }

            Button("Dismiss") {
                stateMachine.reset()
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
        }
        .padding(32)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))
    }
}
