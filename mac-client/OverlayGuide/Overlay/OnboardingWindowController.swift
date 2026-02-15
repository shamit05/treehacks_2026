// Overlay/OnboardingWindowController.swift
// Owner: Eng 1 (Overlay UI)
//
// Manages the onboarding/settings window. Shown on first launch
// and re-openable from the app menu.

import AppKit
import SwiftUI

/// Non-activating panel that accepts keyboard input for the onboarding flow.
private final class OnboardingPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

class OnboardingWindowController {

    private var window: NSPanel?
    private let preferences: UserPreferences

    init(preferences: UserPreferences = .shared) {
        self.preferences = preferences
    }

    // MARK: - Public

    /// Show the onboarding window.
    /// - settingsMode: if true, shows the single-page settings panel instead of the multi-page first-launch flow
    func showOnboarding(settingsMode: Bool = false, onComplete: @escaping () -> Void) {
        // Always dismiss existing window so we get a fresh view
        if window != nil {
            dismiss()
        }

        let panelHeight: CGFloat = settingsMode ? 580 : 480
        let panel = OnboardingPanel(
            contentRect: CGRect(x: 0, y: 0, width: 460, height: panelHeight),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.level = .floating
        panel.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.isMovableByWindowBackground = true
        panel.becomesKeyOnlyIfNeeded = false

        let hostingView = NSHostingView(
            rootView: OnboardingView(
                preferences: preferences,
                settingsMode: settingsMode,
                onComplete: { [weak self] in
                    self?.dismiss()
                    onComplete()
                }
            )
        )
        hostingView.wantsLayer = true
        hostingView.layer?.cornerRadius = 20
        hostingView.layer?.masksToBounds = true
        panel.contentView = hostingView

        // Center on screen
        if let screen = NSScreen.main {
            let screenFrame = screen.visibleFrame
            let x = screenFrame.midX - 230
            let y = screenFrame.midY - (panelHeight / 2)
            panel.setFrameOrigin(CGPoint(x: x, y: y))
        }

        panel.orderFrontRegardless()
        panel.makeKey()
        window = panel
    }

    /// Dismiss the onboarding window
    func dismiss() {
        window?.orderOut(nil)
        window?.close()
        window = nil
    }

    /// Whether the onboarding window is currently visible
    var isShowing: Bool {
        window?.isVisible ?? false
    }
}
