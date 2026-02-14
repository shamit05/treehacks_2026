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
    private var phaseObserver: AnyCancellable?

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

        // Keep overlay visibility and interaction mode in sync with the state machine.
        phaseObserver = stateMachine.$phase
            .receive(on: RunLoop.main)
            .sink { [weak self] phase in
                guard let self else { return }
                switch phase {
                case .idle:
                    self.overlayController.hideAll()
                case .inputGoal, .loading, .guiding, .completed, .error:
                    self.overlayController.showOverlay()
                    self.overlayController.updateForPhase(phase)
                }
            }

        let isUITestMode = CommandLine.arguments.contains("--ui-test")
        if isUITestMode {
            OverlayUITester.runIfEnabled(stateMachine: stateMachine, args: CommandLine.arguments)
        } else {
            // Start listening for hotkey + clicks
            inputMonitor.start()
        }

        print("[AppDelegate] OverlayGuide launched and ready.")
    }

    func applicationWillTerminate(_ notification: Notification) {
        inputMonitor.stop()
        phaseObserver?.cancel()
        overlayController.hideAll()
    }
}
