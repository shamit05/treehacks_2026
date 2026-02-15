// App/AppDelegate.swift
// Owner: Shared (App entry point)
//
// AppDelegate bootstraps the app: creates the state machine,
// starts the input monitor, and prepares overlay windows.

import AppKit
import Combine
import SwiftUI

class AppDelegate: NSObject, NSApplicationDelegate {

    // Core services — instantiated once at launch
    private var stateMachine: GuidanceStateMachine!
    private var inputMonitor: GlobalInputMonitor!
    private var overlayController: OverlayWindowController!
    private var captureService: ScreenCaptureService!
    private var networkClient: AgentNetworkClient!
    private var phaseCancellable: AnyCancellable?
    private var onboardingController: OnboardingWindowController!
    private var prefsNotificationObserver: Any?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // TODO: Check permissions (Screen Recording) and prompt if missing

        if CommandLine.arguments.contains("--ui-test-help") {
            OverlayUITester.printUsage()
        }

        // Initialize services
        networkClient = AgentNetworkClient()
        captureService = ScreenCaptureService()
        stateMachine = GuidanceStateMachine(networkClient: networkClient, captureService: captureService)
        overlayController = OverlayWindowController(stateMachine: stateMachine)
        inputMonitor = GlobalInputMonitor(stateMachine: stateMachine)
        onboardingController = OnboardingWindowController()

        // Keep overlay visibility in sync with state.
        phaseCancellable = stateMachine.$phase
            .receive(on: RunLoop.main)
            .sink { [weak self] phase in
                guard let self else { return }
                self.overlayController.showOverlay(for: phase)
            }

        let isUITestMode = CommandLine.arguments.contains("--ui-test")
        if isUITestMode {
            OverlayUITester.runIfEnabled(stateMachine: stateMachine, args: CommandLine.arguments)
        } else {
            // Start listening for hotkey + clicks
            inputMonitor.start()
        }

        // Wire preferences hotkey (Cmd+Option+,) to open onboarding
        inputMonitor.onOpenPreferences = { [weak self] in
            self?.openLearningPreferences()
        }

        // Also listen for the gear icon tap from the overlay UI
        prefsNotificationObserver = NotificationCenter.default.addObserver(
            forName: .openCookbookPreferences,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            self?.openLearningPreferences()
        }

        // Build the Cookbook menu bar items
        buildAppMenu()

        // Check if onboarding is needed
        let preferences = UserPreferences.shared
        if !preferences.hasCompletedOnboarding {
            // Show onboarding on first launch
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
                guard let self else { return }
                NSApp.activate(ignoringOtherApps: true)
                self.onboardingController.showOnboarding {
                    // After onboarding completes, show the main overlay
                    self.overlayController.showOverlay(for: .inputGoal)
                    self.stateMachine.showInputOverlay()
                }
            }
        } else {
            // Onboarding already done — show overlay directly
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
                guard let self else { return }
                NSApp.activate(ignoringOtherApps: true)
                self.overlayController.showOverlay(for: .inputGoal)
                self.stateMachine.showInputOverlay()
            }
        }

        print("[AppDelegate] Cookbook launched and ready.")
    }

    func applicationWillTerminate(_ notification: Notification) {
        inputMonitor.stop()
        phaseCancellable?.cancel()
        overlayController.hideAll()
        if let observer = prefsNotificationObserver {
            NotificationCenter.default.removeObserver(observer)
        }
    }

    // MARK: - Menu Bar

    private func buildAppMenu() {
        guard let mainMenu = NSApp.mainMenu else { return }

        // Find or create the app menu (first item)
        let appMenu: NSMenu
        if let existingItem = mainMenu.items.first, let submenu = existingItem.submenu {
            appMenu = submenu
        } else {
            appMenu = NSMenu(title: "Cookbook")
            let appMenuItem = NSMenuItem(title: "Cookbook", action: nil, keyEquivalent: "")
            appMenuItem.submenu = appMenu
            mainMenu.addItem(appMenuItem)
        }

        // Add "Learning Preferences..." menu item
        let prefsItem = NSMenuItem(
            title: "Learning Preferences...",
            action: #selector(openLearningPreferences),
            keyEquivalent: ","
        )
        prefsItem.keyEquivalentModifierMask = [.command]
        prefsItem.target = self

        // Insert before the last item (Quit) or at end
        let insertIndex = max(0, appMenu.items.count - 1)
        appMenu.insertItem(NSMenuItem.separator(), at: insertIndex)
        appMenu.insertItem(prefsItem, at: insertIndex + 1)
        appMenu.insertItem(NSMenuItem.separator(), at: insertIndex + 2)
    }

    @objc private func openLearningPreferences() {
        NSApp.activate(ignoringOtherApps: true)
        onboardingController.showOnboarding(settingsMode: true) {
            // preferences saved — nothing else to do
        }
    }
}
