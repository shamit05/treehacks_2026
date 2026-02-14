// App/AppDelegate.swift
// Owner: Shared (App entry point)
//
// AppDelegate bootstraps the app: creates the state machine,
// starts the input monitor, and prepares overlay windows.

import AppKit
import SwiftUI

class AppDelegate: NSObject, NSApplicationDelegate {

    // Core services — instantiated once at launch
    private var stateMachine: GuidanceStateMachine!
    private var inputMonitor: GlobalInputMonitor!
    private var overlayController: OverlayWindowController!
    private var captureService: ScreenCaptureService!
    private var networkClient: AgentNetworkClient!

    func applicationDidFinishLaunching(_ notification: Notification) {
        // TODO: Check permissions (Screen Recording) and prompt if missing

        // Initialize services
        networkClient = AgentNetworkClient()
        captureService = ScreenCaptureService()
        stateMachine = GuidanceStateMachine(networkClient: networkClient, captureService: captureService)
        overlayController = OverlayWindowController(stateMachine: stateMachine)
        inputMonitor = GlobalInputMonitor(stateMachine: stateMachine)

        // Start listening for hotkey + clicks
        inputMonitor.start()

        print("[AppDelegate] OverlayGuide launched and ready.")
    }

    func applicationWillTerminate(_ notification: Notification) {
        inputMonitor.stop()
        overlayController.hideAll()
    }
}
