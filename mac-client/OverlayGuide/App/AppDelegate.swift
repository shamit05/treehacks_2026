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

    func applicationDidFinishLaunching(_ notification: Notification) {
        // TODO: Check permissions (Screen Recording) and prompt if missing

        // Initialize services
        networkClient = AgentNetworkClient()
        captureService = ScreenCaptureService()
        stateMachine = GuidanceStateMachine(networkClient: networkClient, captureService: captureService)
        overlayController = OverlayWindowController(stateMachine: stateMachine)
        inputMonitor = GlobalInputMonitor(stateMachine: stateMachine)

        // Show overlay when guiding; hide when idle
        phaseCancellable = stateMachine.$phase
            .receive(on: RunLoop.main)
            .sink { [weak self] phase in
                guard let self = self else { return }
                switch phase {
                case .idle:
                    self.overlayController.hideAll()
                case .inputGoal, .loading, .guiding, .completed, .error:
                    if case .inputGoal = phase {
                        NSApp.activate(ignoringOtherApps: true)
                    }
                    self.overlayController.showOverlay(for: phase)
                }
            }

        // Start listening for hotkey + clicks
        inputMonitor.start()

        // Fallback: force-show the popup once at launch so window rendering
        // works even if global hotkeys are flaky in this environment.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            guard let self else { return }
            NSApp.activate(ignoringOtherApps: true)
            self.overlayController.showOverlay(for: .inputGoal)
            self.stateMachine.showInputOverlay()
        }

        print("[AppDelegate] OverlayGuide launched and ready.")
    }

    func applicationWillTerminate(_ notification: Notification) {
        inputMonitor.stop()
        overlayController.hideAll()
    }
}
