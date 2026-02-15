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

        if CommandLine.arguments.contains("--ui-test-help") {
            OverlayUITester.printUsage()
        }

        // Initialize services
        networkClient = AgentNetworkClient()
        captureService = ScreenCaptureService()
        stateMachine = GuidanceStateMachine(networkClient: networkClient, captureService: captureService)
        overlayController = OverlayWindowController(stateMachine: stateMachine)
        inputMonitor = GlobalInputMonitor(stateMachine: stateMachine)

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
        phaseCancellable?.cancel()
        overlayController.hideAll()
    }
}
