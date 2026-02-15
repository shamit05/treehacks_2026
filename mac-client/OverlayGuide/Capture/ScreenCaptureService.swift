// Capture/ScreenCaptureService.swift
// Owner: Eng 2 (Capture + Input)
//
// Captures a screenshot of the current display using ScreenCaptureKit (preferred)
// or CGDisplayCreateImage (fallback).
// Returns the image along with screen metadata (bounds, scaling factor).

import AppKit
import Foundation

struct ScreenshotResult {
    let image: NSImage
    let imageData: Data        // PNG data for sending to the agent
    let displayID: CGDirectDisplayID
    let screenBounds: CGRect
    let scaleFactor: CGFloat
    let timestamp: Date
}

class ScreenCaptureService {

    /// Capture the main display. Timeout: 5s.
    /// - Returns: ScreenshotResult with image data and metadata
    func captureMainDisplay() async throws -> ScreenshotResult {
        print("[Capture] Starting main display capture")
        guard CGPreflightScreenCaptureAccess() else {
            CGRequestScreenCaptureAccess()
            throw CaptureError.permissionDenied
        }

        let result = try await withThrowingTaskGroup(of: ScreenshotResult.self) { group in
            group.addTask {
                try await self.performCapture()
            }
            group.addTask {
                try await Task.sleep(nanoseconds: 5_000_000_000)
                throw CaptureError.timeout
            }
            let result = try await group.next()!
            group.cancelAll()
            return result
        }
        print("[Capture] Capture complete: \(result.imageData.count) bytes")
        return result
    }

    private func performCapture() async throws -> ScreenshotResult {
        let displayId = CGMainDisplayID()
        guard let cgImage = CGDisplayCreateImage(displayId) else {
            throw CaptureError.captureFailed
        }
        let displayBounds = CGDisplayBounds(displayId)
        guard displayBounds.width > 0, displayBounds.height > 0 else {
            throw CaptureError.noDisplay
        }

        let scaleFactor = self.scaleFactor(for: displayId)

        let nsImage = NSImage(cgImage: cgImage, size: displayBounds.size)

        guard let tiffData = nsImage.tiffRepresentation,
              let bitmap = NSBitmapImageRep(data: tiffData),
              let pngData = bitmap.representation(using: .png, properties: [:]) else {
            throw CaptureError.encodingFailed
        }

        return ScreenshotResult(
            image: nsImage,
            imageData: pngData,
            displayID: displayId,
            screenBounds: displayBounds,
            scaleFactor: scaleFactor,
            timestamp: Date()
        )
    }

    private func scaleFactor(for displayID: CGDirectDisplayID) -> CGFloat {
        for screen in NSScreen.screens {
            if let num = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber,
               CGDirectDisplayID(num.uint32Value) == displayID {
                return screen.backingScaleFactor
            }
        }
        return 1.0
    }

    /// Check if Screen Recording permission is granted
    func hasScreenRecordingPermission() -> Bool {
        return CGPreflightScreenCaptureAccess()
    }

    /// Prompt for Screen Recording permission
    func requestScreenRecordingPermission() {
        CGRequestScreenCaptureAccess()
    }
}

// MARK: - Errors

enum CaptureError: Error, LocalizedError {
    case noDisplay
    case captureFailed
    case encodingFailed
    case permissionDenied
    case timeout

    var errorDescription: String? {
        switch self {
        case .noDisplay: return "No display found"
        case .captureFailed: return "Screenshot capture failed"
        case .encodingFailed: return "Failed to encode screenshot as PNG"
        case .permissionDenied: return "Screen Recording permission not granted"
        case .timeout: return "Screenshot capture timed out"
        }
    }
}
