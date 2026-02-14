// Overlay/OverlayWindowController.swift
// Owner: Eng 1 (Overlay UI)
//
// Manages one NSPanel per display. Reads from the state machine
// and renders the current step's highlights + instruction bubble.
// This class must NOT perform networking or own state.

import AppKit
import SwiftUI

class OverlayWindowController {

    private let stateMachine: GuidanceStateMachine
    private var panels: [NSPanel] = []

    init(stateMachine: GuidanceStateMachine) {
        self.stateMachine = stateMachine
    }

    // MARK: - Public

    /// Create and show overlay panels on all displays
    func showOverlay() {
        hideAll()

        for screen in NSScreen.screens {
            let panel = createPanel(for: screen)
            let hostingView = NSHostingView(
                rootView: OverlayContentView(stateMachine: stateMachine, screenBounds: screen.frame)
            )
            panel.contentView = hostingView
            panel.orderFrontRegardless()
            panels.append(panel)
        }
    }

    /// Remove all overlay panels
    func hideAll() {
        panels.forEach { $0.close() }
        panels.removeAll()
    }

    // MARK: - Private

    private func createPanel(for screen: NSScreen) -> NSPanel {
        let panel = NSPanel(
            contentRect: screen.frame,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.level = .statusBar
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.ignoresMouseEvents = false // will toggle per-step if needed
        panel.hasShadow = false
        return panel
    }
}
