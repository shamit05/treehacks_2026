// App/OverlayGuideApp.swift
// Owner: Shared (App entry point)
//
// Main entry point for the OverlayGuide macOS app.
// Wires together the state machine, overlay controller, and input monitor.

import SwiftUI

@main
struct OverlayGuideApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        // We use AppDelegate to manage windows directly (NSPanel).
        // No SwiftUI WindowGroup needed for the overlay.
        Settings {
            Text("OverlayGuide Settings")
                .padding()
        }
    }
}
