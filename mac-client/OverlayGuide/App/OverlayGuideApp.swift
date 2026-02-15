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
        Settings {
            Text("The Cookbook Settings")
                .padding()
        }
    }
}
