// Overlay/OverlayWindowController.swift
// Owner: Eng 1 (Overlay UI)
//
// Manages a single movable popup panel for overlay UI.
// Uses a non-activating panel so the target app stays visible
// and its menu bar remains showing (important for screenshots).

import AppKit
import SwiftUI

/// Non-activating panel that accepts keyboard input
/// without stealing app activation from the target application.
private final class OverlayPopupPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

class OverlayWindowController {

    private let stateMachine: GuidanceStateMachine
    private var window: NSPanel?
    private var highlightPanel: NSPanel?
    private let popupSize = NSSize(width: 460, height: 320)

    init(stateMachine: GuidanceStateMachine) {
        self.stateMachine = stateMachine
    }

    // MARK: - Public

    /// Show a single movable popup panel.
    /// Uses a non-activating panel so the target app stays visible.
    func showOverlay(for phase: GuidancePhase) {
        if case .idle = phase {
            hideAll()
            return
        }

        if window == nil {
            window = createPanel()
            // Position near cursor only when first created
            let mouse = NSEvent.mouseLocation
            if let screen = screenContaining(point: mouse) {
                let origin = popupOrigin(near: mouse, in: screen.visibleFrame)
                window!.setFrameOrigin(origin)
            }
            // Set SwiftUI content view once — it observes stateMachine via @ObservedObject
            let hostingView = NSHostingView(
                rootView: OverlayContentView(stateMachine: stateMachine)
            )
            hostingView.wantsLayer = true
            hostingView.layer?.cornerRadius = 20
            hostingView.layer?.masksToBounds = true
            window!.contentView = hostingView
        }
        guard let window else { return }

        applyInteraction(for: phase, panel: window)
        updateHighlightPanels(for: phase)
    }

    /// Hide all overlay panels. Keeps the window alive so SwiftUI state is preserved.
    func hideAll() {
        window?.orderOut(nil)
        hideHighlightPanels()
    }

    /// Fully destroy all panels (called on app quit).
    func destroyAll() {
        window?.close()
        window = nil
        hideHighlightPanels()
    }

    // MARK: - Private

    private func createPanel() -> NSPanel {
        let visibleFrame = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame
            ?? CGRect(x: 0, y: 0, width: 1440, height: 900)
        let mouse = NSEvent.mouseLocation
        let origin = popupOrigin(near: mouse, in: visibleFrame)

        let panel = OverlayPopupPanel(
            contentRect: CGRect(origin: origin, size: popupSize),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isMovableByWindowBackground = true
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.level = .floating
        panel.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        // Accept keyboard input without requiring app activation
        panel.becomesKeyOnlyIfNeeded = false
        return panel
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

    private func applyInteraction(for phase: GuidancePhase, panel: NSPanel) {
        switch phase {
        case .inputGoal, .loading, .guiding, .completed, .error:
            // Show the panel and give it keyboard focus, but do NOT
            // activate OverlayGuide — the target app stays in front.
            panel.orderFrontRegardless()
            panel.makeKey()
        case .idle:
            panel.orderOut(nil)
        }
    }

    private func updateHighlightPanels(for phase: GuidancePhase) {
        // Show highlights during guiding AND loading (grayed out during loading)
        switch phase {
        case .guiding, .loading:
            break
        default:
            hideHighlightPanels()
            return
        }

        let targetBounds = stateMachine.capturedScreenBounds
            ?? NSScreen.main?.frame
            ?? NSScreen.screens.first?.frame
            ?? .zero

        if highlightPanel == nil {
            let panel = NSPanel(
                contentRect: targetBounds,
                styleMask: [.borderless, .nonactivatingPanel],
                backing: .buffered,
                defer: false
            )
            panel.isOpaque = false
            panel.backgroundColor = .clear
            panel.level = .statusBar
            panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            panel.ignoresMouseEvents = true
            panel.hasShadow = false
            highlightPanel = panel
        }

        guard let highlightPanel else { return }
        highlightPanel.setFrame(targetBounds, display: true)
        highlightPanel.contentView = NSHostingView(
            rootView: HighlightOverlayView(stateMachine: stateMachine, screenBounds: targetBounds)
        )
        highlightPanel.orderFrontRegardless()
    }

    private func hideHighlightPanels() {
        highlightPanel?.close()
        highlightPanel = nil
    }
}
