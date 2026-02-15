// App/TheCookbookApp.swift
// Owner: Shared (App entry point)
//
// Main entry point for The Cookbook macOS app.
// Wires together the state machine, overlay controller, and input monitor.

import SwiftUI

@main
struct TheCookbookApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        // We use AppDelegate to manage windows directly (NSPanel).
        // No SwiftUI WindowGroup needed for the overlay.
        // The Settings scene powers the native "Settings..." (Cmd+,) menu item.
        Settings {
            OnboardingView(
                preferences: .shared,
                settingsMode: true,
                onComplete: {
                    // Close the settings window when the user saves/dismisses
                    NSApp.keyWindow?.close()
                }
            )
        }
    }
}
