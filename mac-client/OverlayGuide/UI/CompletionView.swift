// UI/CompletionView.swift
// Owner: Eng 1 (Overlay UI)
//
// Shown when all steps are completed. Displays a success message
// with a celebration animation and auto-dismisses after a few seconds.

import SwiftUI

struct CompletionView: View {
    @ObservedObject var stateMachine: GuidanceStateMachine
    @State private var showCheckmark = false
    @State private var showText = false
    @State private var showConfetti = false
    @State private var confettiPieces: [ConfettiPiece] = []

    var body: some View {
        ZStack {
            // Confetti particles
            ForEach(confettiPieces) { piece in
                Circle()
                    .fill(piece.color)
                    .frame(width: piece.size, height: piece.size)
                    .offset(x: piece.x, y: showConfetti ? piece.endY : piece.startY)
                    .opacity(showConfetti ? 0 : 1)
                    .animation(
                        .easeOut(duration: piece.duration)
                            .delay(piece.delay),
                        value: showConfetti
                    )
            }

            // Main card
            VStack(spacing: 16) {
                // Animated checkmark
                ZStack {
                    Circle()
                        .fill(Color.green.opacity(0.15))
                        .frame(width: 80, height: 80)
                        .scaleEffect(showCheckmark ? 1.0 : 0.3)
                        .opacity(showCheckmark ? 1 : 0)

                    Image(systemName: "checkmark.circle.fill")
                        .font(.system(size: 52))
                        .foregroundColor(.green)
                        .scaleEffect(showCheckmark ? 1.0 : 0.1)
                        .rotationEffect(.degrees(showCheckmark ? 0 : -30))
                }
                .animation(.spring(response: 0.5, dampingFraction: 0.6), value: showCheckmark)

                VStack(spacing: 8) {
                    Text(stateMachine.completionMessage ?? "All done!")
                        .font(.title2)
                        .fontWeight(.bold)
                        .foregroundColor(.white)

                    if let plan = stateMachine.currentPlan {
                        Text(plan.goal)
                            .font(.callout)
                            .foregroundColor(.secondary)
                            .multilineTextAlignment(.center)
                    }

                    Text("\(stateMachine.completedSteps.count) steps completed")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                .opacity(showText ? 1 : 0)
                .offset(y: showText ? 0 : 10)
                .animation(.easeOut(duration: 0.4).delay(0.3), value: showText)

                Button("Dismiss") {
                    stateMachine.reset()
                }
                .buttonStyle(.borderedProminent)
                .tint(.green)
                .controlSize(.large)
                .opacity(showText ? 1 : 0)
                .animation(.easeOut(duration: 0.3).delay(0.6), value: showText)
            }
            .padding(32)
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 20))
            .shadow(color: .green.opacity(0.2), radius: 20, y: 5)
        }
        .onAppear {
            generateConfetti()
            withAnimation {
                showCheckmark = true
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
                showText = true
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                showConfetti = true
            }
            // Auto-dismiss after 5 seconds
            DispatchQueue.main.asyncAfter(deadline: .now() + 5.0) {
                stateMachine.reset()
            }
        }
    }

    private func generateConfetti() {
        let colors: [Color] = [.green, .blue, .yellow, .orange, .pink, .purple, .mint]
        confettiPieces = (0..<30).map { _ in
            ConfettiPiece(
                color: colors.randomElement()!,
                size: CGFloat.random(in: 4...10),
                x: CGFloat.random(in: -150...150),
                startY: CGFloat.random(in: -80 ... -20),
                endY: CGFloat.random(in: 100...250),
                delay: Double.random(in: 0...0.3),
                duration: Double.random(in: 0.8...1.5)
            )
        }
    }
}

private struct ConfettiPiece: Identifiable {
    let id = UUID()
    let color: Color
    let size: CGFloat
    let x: CGFloat
    let startY: CGFloat
    let endY: CGFloat
    let delay: Double
    let duration: Double
}
