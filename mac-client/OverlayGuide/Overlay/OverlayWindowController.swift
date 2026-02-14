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

        let hostingView = NSHostingView(
            rootView: OverlayContentView(stateMachine: stateMachine)
        )
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
        let origin = CGPoint(
            x: visibleFrame.maxX - popupSize.width - 24,
            y: visibleFrame.maxY - popupSize.height - 24
        )

        let window = OverlayPopupWindow(
            contentRect: CGRect(origin: origin, size: popupSize),
            styleMask: [.titled, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "OverlayGuide"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.standardWindowButton(.closeButton)?.isHidden = true
        window.standardWindowButton(.miniaturizeButton)?.isHidden = true
        window.standardWindowButton(.zoomButton)?.isHidden = true
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
