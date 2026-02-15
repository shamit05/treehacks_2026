// Overlay/OverlayWindowController.swift
// Owner: Eng 1 (Overlay UI)
//
// Manages one NSPanel per display. Reads from the state machine
// and renders the current step's highlights + instruction bubble.
// This class must NOT perform networking or own state.

import AppKit
import SwiftUI

private final class OverlayPopupWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

class OverlayWindowController {

    private let stateMachine: GuidanceStateMachine
    private var window: NSWindow?
    private let popupSize = NSSize(width: 460, height: 320)

    init(stateMachine: GuidanceStateMachine) {
        self.stateMachine = stateMachine
    }

    // MARK: - Public

    /// Show a single movable popup window.
    func showOverlay(for phase: GuidancePhase) {
        if case .idle = phase {
            hideAll()
            return
        }

        if window == nil {
            window = createWindow()
        }
        guard let window else { return }

        // Reposition near cursor on every show request.
        let mouse = NSEvent.mouseLocation
        if let screen = screenContaining(point: mouse) {
            let origin = popupOrigin(near: mouse, in: screen.visibleFrame)
            window.setFrameOrigin(origin)
        }

        let hostingView = NSHostingView(
            rootView: OverlayContentView(stateMachine: stateMachine)
        )
        hostingView.wantsLayer = true
        hostingView.layer?.cornerRadius = 20
        hostingView.layer?.masksToBounds = true
        window.contentView = hostingView
        applyInteraction(for: phase, window: window)
    }

    /// Remove popup window.
    func hideAll() {
        window?.orderOut(nil)
        window?.close()
        window = nil
    }

    // MARK: - Private

    private func createWindow() -> NSWindow {
        let visibleFrame = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame
            ?? CGRect(x: 0, y: 0, width: 1440, height: 900)
        let mouse = NSEvent.mouseLocation
        let origin = popupOrigin(near: mouse, in: visibleFrame)

        let window = OverlayPopupWindow(
            contentRect: CGRect(origin: origin, size: popupSize),
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        window.isMovableByWindowBackground = true
        window.isOpaque = false
        window.backgroundColor = .clear
        window.level = .floating
        // Keep this conservative for NSWindow; some collectionBehavior combinations
        // that work for NSPanel will crash for NSWindow.
        window.collectionBehavior = [.moveToActiveSpace]
        window.hasShadow = true
        window.hidesOnDeactivate = false
        window.isReleasedWhenClosed = false
        return window
    }

    private func screenContaining(point: CGPoint) -> NSScreen? {
        NSScreen.screens.first { $0.frame.contains(point) } ?? NSScreen.main ?? NSScreen.screens.first
    }

    private func popupOrigin(near cursor: CGPoint, in visibleFrame: CGRect) -> CGPoint {
        let margin: CGFloat = 12
        let defaultX = cursor.x + 20
        let defaultY = cursor.y - (popupSize.height * 0.5)

        let minX = visibleFrame.minX + margin
        let maxX = visibleFrame.maxX - popupSize.width - margin
        let minY = visibleFrame.minY + margin
        let maxY = visibleFrame.maxY - popupSize.height - margin

        let clampedX = min(max(defaultX, minX), maxX)
        let clampedY = min(max(defaultY, minY), maxY)
        return CGPoint(x: clampedX, y: clampedY)
    }

    private func applyInteraction(for phase: GuidancePhase, window: NSWindow) {
        switch phase {
        case .inputGoal, .loading, .guiding, .completed, .error:
            window.orderFrontRegardless()
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        case .idle:
            window.orderOut(nil)
        }
    }
}
