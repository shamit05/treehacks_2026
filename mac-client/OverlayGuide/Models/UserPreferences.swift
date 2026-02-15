// Models/UserPreferences.swift
// Owner: Eng 4 (State Machine)
//
// Persists user preferences (learning style, onboarding state) via UserDefaults.
// Observable so SwiftUI views can react to changes.

import AppKit
import AVFoundation
import Combine
import Foundation

class UserPreferences: ObservableObject {

    static let shared = UserPreferences()

    // MARK: - Keys

    private enum Keys {
        static let learningStyle = "cookbook_learningStyle"
        static let hasCompletedOnboarding = "cookbook_hasCompletedOnboarding"
    }

    // MARK: - Published Properties

    /// The user's preferred learning style text (sent to Gemini as prompt injection).
    @Published var learningStyle: String {
        didSet { UserDefaults.standard.set(learningStyle, forKey: Keys.learningStyle) }
    }

    /// Whether the user has completed first-launch onboarding.
    @Published var hasCompletedOnboarding: Bool {
        didSet { UserDefaults.standard.set(hasCompletedOnboarding, forKey: Keys.hasCompletedOnboarding) }
    }

    // MARK: - Computed

    /// Returns the LearningProfile to inject into network requests.
    var learningProfile: LearningProfile {
        LearningProfile(
            text: learningStyle.isEmpty ? "default" : learningStyle,
            presets: nil
        )
    }

    // MARK: - System Info

    /// Current macOS version string (e.g. "15.3.1")
    var macOSVersion: String {
        let v = ProcessInfo.processInfo.operatingSystemVersion
        return "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)"
    }

    /// macOS version name (e.g. "Sequoia", "Sonoma")
    var macOSName: String {
        let major = ProcessInfo.processInfo.operatingSystemVersion.majorVersion
        switch major {
        case 15: return "Sequoia"
        case 14: return "Sonoma"
        case 13: return "Ventura"
        case 12: return "Monterey"
        default: return "macOS \(major)"
        }
    }

    /// Hardware model identifier
    var hardwareModel: String {
        var size = 0
        sysctlbyname("hw.model", nil, &size, nil, 0)
        var model = [CChar](repeating: 0, count: size)
        sysctlbyname("hw.model", &model, &size, nil, 0)
        return String(cString: model)
    }

    /// Installed apps we detect for contextual guidance
    var detectedApps: [(name: String, version: String)] {
        let appsToCheck = [
            ("/Applications/Safari.app", "Safari"),
            ("/Applications/Google Chrome.app", "Chrome"),
            ("/Applications/Visual Studio Code.app", "VS Code"),
            ("/Applications/Xcode.app", "Xcode"),
            ("/System/Applications/System Settings.app", "System Settings"),
            ("/Applications/Slack.app", "Slack"),
            ("/Applications/Figma.app", "Figma"),
            ("/System/Applications/Finder.app", "Finder"),
        ]

        return appsToCheck.compactMap { (path, name) in
            guard let bundle = Bundle(path: path),
                  let version = bundle.infoDictionary?["CFBundleShortVersionString"] as? String else {
                return nil
            }
            return (name: name, version: version)
        }
    }

    // MARK: - Permission Checks

    /// Check if Accessibility permission is granted (needed for click detection)
    var hasAccessibilityPermission: Bool {
        AXIsProcessTrusted()
    }

    /// Check if Screen Recording permission is granted (needed for screenshots)
    var hasScreenRecordingPermission: Bool {
        CGPreflightScreenCaptureAccess()
    }

    /// Check Microphone permission status
    var microphonePermissionStatus: AVAuthorizationStatus {
        AVCaptureDevice.authorizationStatus(for: .audio)
    }

    var hasMicrophonePermission: Bool {
        microphonePermissionStatus == .authorized
    }

    /// Open System Settings to the Accessibility pane
    func openAccessibilitySettings() {
        let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!
        NSWorkspace.shared.open(url)
    }

    /// Open System Settings to the Screen Recording pane
    func openScreenRecordingSettings() {
        let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")!
        NSWorkspace.shared.open(url)
    }

    /// Open System Settings to the Microphone pane
    func openMicrophoneSettings() {
        let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")!
        NSWorkspace.shared.open(url)
    }

    /// Request Screen Recording permission (triggers the system prompt)
    func requestScreenRecordingPermission() {
        CGRequestScreenCaptureAccess()
    }

    /// Request Microphone permission
    func requestMicrophonePermission() async -> Bool {
        await AVCaptureDevice.requestAccess(for: .audio)
    }

    // MARK: - Init

    private init() {
        self.learningStyle = UserDefaults.standard.string(forKey: Keys.learningStyle) ?? ""
        self.hasCompletedOnboarding = UserDefaults.standard.bool(forKey: Keys.hasCompletedOnboarding)
    }

    // MARK: - Actions

    /// Reset onboarding (for re-showing it)
    func resetOnboarding() {
        hasCompletedOnboarding = false
    }

    /// Mark onboarding as completed
    func completeOnboarding() {
        hasCompletedOnboarding = true
    }

    /// Set to default learning style
    func useDefaultLearningStyle() {
        learningStyle = ""
    }
}
