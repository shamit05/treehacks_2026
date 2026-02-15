// Overlay/TargetHighlightOverlayView.swift
// Owner: Eng 1 (Overlay UI)
//
// Full-screen transparent overlay that renders target highlights using
// normalized StepPlan coordinates (top-left origin).

import SwiftUI

struct TargetHighlightOverlayView: View {
    let targets: [TargetRect]
    let screenFrame: CGRect

    var body: some View {
        ZStack(alignment: .topLeading) {
            Color.clear
            ForEach(Array(targets.enumerated()), id: \.offset) { _, target in
                if let rect = rectForTarget(target) {
                    RoundedRectangle(cornerRadius: 10)
                        .stroke(Color.blue.opacity(0.95), lineWidth: 3)
                        .background(
                            RoundedRectangle(cornerRadius: 10)
                                .fill(Color.blue.opacity(0.16))
                        )
                        .frame(width: max(rect.width, 18), height: max(rect.height, 18))
                        .position(
                            x: rect.minX + (max(rect.width, 18) / 2),
                            y: rect.minY + (max(rect.height, 18) / 2)
                        )
                }
            }
        }
        .frame(width: screenFrame.width, height: screenFrame.height)
        .allowsHitTesting(false)
    }

    private func rectForTarget(_ target: TargetRect) -> CGRect? {
        guard target.type == .bboxNorm,
              let xNorm = target.x,
              let yNorm = target.y,
              let wNorm = target.w,
              let hNorm = target.h else {
            return nil
        }
        let width = CGFloat(wNorm) * screenFrame.width
        let height = CGFloat(hNorm) * screenFrame.height
        let x = CGFloat(xNorm) * screenFrame.width
        // Convert schema top-left origin to AppKit/SwiftUI bottom-left coordinates.
        let yFromBottom = (1.0 - CGFloat(yNorm) - CGFloat(hNorm)) * screenFrame.height
        return CGRect(x: x, y: yFromBottom, width: width, height: height)
    }
}
