// App/AppDelegate.swift
// Owner: Shared (App entry point)
//
// AppDelegate bootstraps the app: creates the state machine,
// starts the input monitor, prepares overlay windows, and
// optionally initialises the voice pipeline.

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

    // Voice pipeline configuration (set via environment or defaults)
    // This is the base URL of the Modal ASGI app (handles /offer + /ws/{session_id})
    private let voiceBotURLString = ProcessInfo.processInfo.environment["VOICE_BOT_URL"]
        ?? "https://overlay-voice-assistant--serve-frontend.modal.run"

    func applicationDidFinishLaunching(_ notification: Notification) {
        // TODO: Check permissions (Screen Recording, Microphone) and prompt if missing

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

        // Initialize voice pipeline if --enable-voice flag is set
        if CommandLine.arguments.contains("--enable-voice") {
            initializeVoicePipeline()
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

        // Clean up voice resources
        stateMachine.teardownVoice()
    }

    // MARK: - Voice Pipeline Initialization

    private func initializeVoicePipeline() {
        guard let botURL = URL(string: voiceBotURLString) else {
            print("[AppDelegate] Invalid VOICE_BOT_URL: \(voiceBotURLString)")
            return
        }

        print("[AppDelegate] Initializing voice pipeline...")
        print("[AppDelegate]   Bot URL: \(voiceBotURLString)")

        Task {
            do {
                try await stateMachine.initializeVoice(botURL: botURL)
                print("[AppDelegate] Voice pipeline ready.")
            } catch {
                print("[AppDelegate] Voice initialization failed: \(error.localizedDescription)")
                print("[AppDelegate] Continuing in text-only mode.")
            }
        }
    }
}
