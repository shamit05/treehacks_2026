// Capture/CoordinateMapper.swift
// Owner: Eng 2 (Capture + Input)
//
// Converts between coordinate spaces:
// - Normalized [0,1] (top-left origin, as used in StepPlan)
// - Screen pixels (macOS bottom-left origin)
// - Screenshot pixels (top-left origin)
//
// IMPORTANT: macOS screen coords use bottom-left origin.
// The JSON schema uses top-left origin. This mapper handles the flip.

import AppKit
import Foundation

struct CoordinateMapper {

    let screenBounds: CGRect
    let scaleFactor: CGFloat

    /// Convert a normalized target rect (top-left origin) to screen coordinates (bottom-left origin).
    func normalizedToScreen(_ target: TargetRect) -> CGRect {
        let x = target.x * screenBounds.width + screenBounds.origin.x
        let w = target.w * screenBounds.width
        let h = target.h * screenBounds.height
        // Flip Y: macOS bottom-left origin
        let y = screenBounds.height - (target.y * screenBounds.height + h) + screenBounds.origin.y

        return CGRect(x: x, y: y, width: w, height: h)
    }

    /// Check if a screen-space click point (bottom-left origin) falls inside a normalized target rect.
    func isClick(_ clickPoint: CGPoint, insideTarget target: TargetRect) -> Bool {
        let screenRect = normalizedToScreen(target)
        return screenRect.contains(clickPoint)
    }

    /// Convert a screen-space point (bottom-left origin) to normalized coordinates (top-left origin).
    func screenToNormalized(_ point: CGPoint) -> CGPoint {
        let normX = (point.x - screenBounds.origin.x) / screenBounds.width
        let normY = 1.0 - ((point.y - screenBounds.origin.y) / screenBounds.height)
        return CGPoint(x: normX, y: normY)
    }
}
