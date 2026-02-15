// UI/OnboardingView.swift
// Owner: Eng 1 (Overlay UI)
//
// Onboarding pane (first launch multi-page flow) and settings panel
// (single page with permissions, system info, learning style).

import SwiftUI

struct OnboardingView: View {
    @ObservedObject var preferences: UserPreferences
    var settingsMode: Bool = false
    var onComplete: () -> Void

    @State private var currentPage: OnboardingPage = .welcome
    @State private var learningStyleText: String = ""
    @State private var detectedApps: [(name: String, version: String)] = []
    @State private var showApps = false
    @FocusState private var isTextFieldFocused: Bool
    @State private var showingOnboardingFlow = false

    // Permission states (polled on appear + timer)
    @State private var hasAccessibility = false
    @State private var hasScreenRecording = false
    @State private var hasMicrophone = false
    @State private var permissionTimer: Timer?

    enum OnboardingPage {
        case welcome
        case systemScan
        case learningStyle
    }

    var body: some View {
        Group {
            if settingsMode && !showingOnboardingFlow {
                settingsPage
            } else {
                onboardingFlow
            }
        }
        .animation(.easeInOut(duration: 0.3), value: showingOnboardingFlow)
        .onAppear {
            learningStyleText = preferences.learningStyle
            refreshPermissions()
            // Poll permissions every 2s (user may grant in System Settings)
            permissionTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { _ in
                refreshPermissions()
            }
        }
        .onDisappear {
            permissionTimer?.invalidate()
            permissionTimer = nil
        }
    }

    // ================================================================
    // MARK: - Settings Mode (single page, shown from gear / hotkey)
    // ================================================================

    private var settingsPage: some View {
        ScrollView {
            VStack(spacing: 16) {
                // Header
                HStack {
                    ZStack {
                        Circle()
                            .fill(
                                LinearGradient(
                                    colors: [.orange, .red, .pink],
                                    startPoint: .topLeading,
                                    endPoint: .bottomTrailing
                                )
                            )
                            .frame(width: 36, height: 36)

                        Image(systemName: "book.and.wrench.fill")
                            .font(.system(size: 16))
                            .foregroundColor(.white)
                    }

                    VStack(alignment: .leading, spacing: 2) {
                        Text("The Cookbook")
                            .font(.system(size: 18, weight: .bold, design: .rounded))
                        Text("Settings & Permissions")
                            .font(.system(size: 12))
                            .foregroundColor(.secondary)
                    }

                    Spacer()

                    Button(action: {
                        preferences.learningStyle = learningStyleText
                        onComplete()
                    }) {
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 16))
                            .foregroundColor(.secondary.opacity(0.6))
                    }
                    .buttonStyle(.plain)
                }
                .padding(.bottom, 4)

                // ── Permissions ──
                VStack(alignment: .leading, spacing: 8) {
                    Label("Permissions", systemImage: "lock.shield.fill")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(.primary)

                    permissionRow(
                        icon: "hand.tap.fill",
                        label: "Accessibility",
                        detail: "Click detection & hotkeys",
                        granted: hasAccessibility,
                        action: { preferences.openAccessibilitySettings() }
                    )

                    permissionRow(
                        icon: "rectangle.dashed.badge.record",
                        label: "Screen Recording",
                        detail: "Screenshot capture",
                        granted: hasScreenRecording,
                        action: {
                            preferences.requestScreenRecordingPermission()
                            preferences.openScreenRecordingSettings()
                        }
                    )

                    permissionRow(
                        icon: "mic.fill",
                        label: "Microphone",
                        detail: "Voice input (optional)",
                        granted: hasMicrophone,
                        action: {
                            Task {
                                let granted = await preferences.requestMicrophonePermission()
                                if !granted {
                                    preferences.openMicrophoneSettings()
                                }
                                refreshPermissions()
                            }
                        }
                    )
                }

                Divider()

                // ── System Info ──
                VStack(alignment: .leading, spacing: 8) {
                    Label("System", systemImage: "desktopcomputer")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(.primary)

                    HStack {
                        Text("macOS")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundColor(.secondary)
                        Spacer()
                        Text("\(preferences.macOSName) \(preferences.macOSVersion)")
                            .font(.system(size: 12))
                            .foregroundColor(.primary)
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .background(RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.06)))

                    HStack {
                        Text("Hardware")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundColor(.secondary)
                        Spacer()
                        Text(friendlyHardwareName(preferences.hardwareModel))
                            .font(.system(size: 12))
                            .foregroundColor(.primary)
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .background(RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.06)))

                    // Detected apps compact
                    let apps = preferences.detectedApps
                    if !apps.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            HStack {
                                Text("Detected Apps")
                                    .font(.system(size: 12, weight: .medium))
                                    .foregroundColor(.secondary)
                                Spacer()
                                Text("\(apps.count) found")
                                    .font(.system(size: 11))
                                    .foregroundColor(.green)
                            }
                            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 2) {
                                ForEach(Array(apps.enumerated()), id: \.offset) { _, app in
                                    HStack(spacing: 3) {
                                        Text(app.name)
                                            .font(.system(size: 11, weight: .medium))
                                            .foregroundColor(.secondary)
                                        Text("v\(app.version)")
                                            .font(.system(size: 10))
                                            .foregroundColor(.secondary.opacity(0.6))
                                        Spacer()
                                    }
                                }
                            }
                        }
                        .padding(.horizontal, 12)
                        .padding(.vertical, 6)
                        .background(RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.06)))
                    }
                }

                Divider()

                // ── Learning Style ──
                VStack(alignment: .leading, spacing: 8) {
                    Label("Learning Style", systemImage: "brain.head.profile.fill")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(.primary)

                    // Presets
                    VStack(spacing: 6) {
                        HStack(spacing: 6) {
                            presetButton(icon: "bolt.fill", label: "Minimal", value: "Show me minimal steps, 5-8 words per instruction, no explanations")
                            presetButton(icon: "book.fill", label: "Explain why", value: "Explain why each step matters with brief rationale")
                        }
                        HStack(spacing: 6) {
                            presetButton(icon: "eye.fill", label: "Visual cues", value: "Emphasize visual cues like colors, icons, and positions in instructions")
                            presetButton(icon: "graduationcap.fill", label: "Teach me", value: "Teach me the concepts behind each action so I learn, not just follow")
                        }
                    }

                    // Custom input
                    HStack(spacing: 8) {
                        Image(systemName: "pencil.line")
                            .font(.system(size: 13, weight: .medium))
                            .foregroundStyle(LinearGradient(colors: [.purple, .pink], startPoint: .topLeading, endPoint: .bottomTrailing))

                        TextField("Describe your preferred style...", text: $learningStyleText)
                            .textFieldStyle(.plain)
                            .font(.system(size: 13))
                            .focused($isTextFieldFocused)
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 9)
                    .background(RoundedRectangle(cornerRadius: 8).fill(Color(nsColor: .controlBackgroundColor)))
                    .overlay(
                        RoundedRectangle(cornerRadius: 8)
                            .stroke(isTextFieldFocused ? Color.purple.opacity(0.5) : Color.secondary.opacity(0.2), lineWidth: 1)
                            .allowsHitTesting(false)
                    )

                    if !learningStyleText.isEmpty {
                        HStack {
                            Button("Clear") {
                                learningStyleText = ""
                                preferences.useDefaultLearningStyle()
                            }
                            .buttonStyle(.bordered)
                            .controlSize(.small)

                            Spacer()

                            Button("Save") {
                                preferences.learningStyle = learningStyleText
                                onComplete()
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(.purple)
                            .controlSize(.small)
                        }
                    }
                }

                Divider()

                // Bottom row: redo onboarding + hotkey reminder
                HStack {
                    Button(action: {
                        currentPage = .welcome
                        showingOnboardingFlow = true
                    }) {
                        HStack(spacing: 4) {
                            Image(systemName: "arrow.counterclockwise")
                                .font(.system(size: 11))
                            Text("Redo Onboarding")
                                .font(.system(size: 12))
                        }
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)

                    Spacer()

                    HStack(spacing: 4) {
                        Image(systemName: "keyboard")
                            .font(.system(size: 10))
                            .foregroundColor(.secondary)
                        Text("Cmd+Opt+O overlay · Cmd+Opt+, settings")
                            .font(.system(size: 10))
                            .foregroundColor(.secondary.opacity(0.7))
                    }
                }
            }
            .padding(24)
        }
        .frame(width: 460, height: 580)
        .background(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(Color(nsColor: .windowBackgroundColor))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .stroke(Color.secondary.opacity(0.2), lineWidth: 1)
                .allowsHitTesting(false)
        )
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .shadow(color: .black.opacity(0.2), radius: 20, y: 8)
    }

    // ================================================================
    // MARK: - First Launch Onboarding (multi-page flow)
    // ================================================================

    private var onboardingFlow: some View {
        VStack(spacing: 0) {
            // Progress dots
            HStack(spacing: 8) {
                ForEach(Array([OnboardingPage.welcome, .systemScan, .learningStyle].enumerated()), id: \.offset) { index, page in
                    Circle()
                        .fill(pageIndex(page) <= pageIndex(currentPage)
                            ? Color.accentColor
                            : Color.secondary.opacity(0.25))
                        .frame(width: 8, height: 8)
                        .animation(.easeInOut(duration: 0.3), value: currentPage)
                }
            }
            .padding(.top, 20)
            .padding(.bottom, 12)

            Group {
                switch currentPage {
                case .welcome:
                    welcomePage
                case .systemScan:
                    systemScanPage
                case .learningStyle:
                    learningStyleOnboardingPage
                }
            }
            .transition(.asymmetric(
                insertion: .move(edge: .trailing).combined(with: .opacity),
                removal: .move(edge: .leading).combined(with: .opacity)
            ))
            .animation(.easeInOut(duration: 0.35), value: currentPage)
        }
        .frame(width: 460, height: 480)
        .background(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(Color(nsColor: .windowBackgroundColor))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .stroke(Color.secondary.opacity(0.2), lineWidth: 1)
                .allowsHitTesting(false)
        )
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .shadow(color: .black.opacity(0.2), radius: 20, y: 8)
    }

    // MARK: - Page 1: Welcome

    private var welcomePage: some View {
        VStack(spacing: 16) {
            Spacer()

            ZStack {
                Circle()
                    .fill(
                        LinearGradient(colors: [.orange, .red, .pink], startPoint: .topLeading, endPoint: .bottomTrailing)
                    )
                    .frame(width: 80, height: 80)

                Image(systemName: "book.and.wrench.fill")
                    .font(.system(size: 36))
                    .foregroundColor(.white)
            }
            .shadow(color: .orange.opacity(0.3), radius: 12, y: 4)

            Text("Welcome to the Cookbook")
                .font(.system(size: 26, weight: .bold, design: .rounded))
                .foregroundColor(.primary)

            Text("Your AI-powered guide to mastering any app on your Mac. We'll walk you through anything, step by step.")
                .font(.system(size: 14))
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 40)
                .fixedSize(horizontal: false, vertical: true)

            Spacer()

            Button(action: {
                withAnimation { currentPage = .systemScan }
            }) {
                Text("Get Started")
            }
            .buttonStyle(.borderedProminent)
            .tint(.blue)
            .controlSize(.large)
            .padding(.horizontal, 40)
            .padding(.bottom, 24)
        }
    }

    // MARK: - Page 2: System Scan + Permissions

    private var systemScanPage: some View {
        VStack(spacing: 12) {
            Spacer()

            ZStack {
                Circle()
                    .fill(Color.blue.opacity(0.12))
                    .frame(width: 56, height: 56)

                Image(systemName: "gearshape.2.fill")
                    .font(.system(size: 26))
                    .foregroundStyle(
                        LinearGradient(colors: [.blue, .cyan], startPoint: .topLeading, endPoint: .bottomTrailing)
                    )
            }

            Text("Setting up the Cookbook")
                .font(.system(size: 20, weight: .bold, design: .rounded))
                .foregroundColor(.primary)

            // Permissions section
            VStack(spacing: 6) {
                permissionRow(
                    icon: "hand.tap.fill",
                    label: "Accessibility",
                    detail: "Click detection & hotkeys",
                    granted: hasAccessibility,
                    action: { preferences.openAccessibilitySettings() }
                )

                permissionRow(
                    icon: "rectangle.dashed.badge.record",
                    label: "Screen Recording",
                    detail: "Screenshot capture",
                    granted: hasScreenRecording,
                    action: {
                        preferences.requestScreenRecordingPermission()
                        preferences.openScreenRecordingSettings()
                    }
                )

                permissionRow(
                    icon: "mic.fill",
                    label: "Microphone",
                    detail: "Voice input (optional)",
                    granted: hasMicrophone,
                    action: {
                        Task {
                            let granted = await preferences.requestMicrophonePermission()
                            if !granted { preferences.openMicrophoneSettings() }
                            refreshPermissions()
                        }
                    }
                )
            }
            .padding(.horizontal, 30)

            // System info
            VStack(spacing: 6) {
                systemInfoRow(icon: "desktopcomputer", label: "macOS", value: "\(preferences.macOSName) \(preferences.macOSVersion)", status: .done)
                systemInfoRow(icon: "cpu", label: "Hardware", value: friendlyHardwareName(preferences.hardwareModel), status: .done)

                if showApps {
                    systemInfoRow(icon: "app.badge.checkmark.fill", label: "Apps", value: "\(detectedApps.count) detected", status: .done)
                } else {
                    systemInfoRow(icon: "app.badge.checkmark.fill", label: "Apps", value: "Scanning...", status: .loading)
                }
            }
            .padding(.horizontal, 30)

            Spacer()

            Button(action: {
                withAnimation { currentPage = .learningStyle }
            }) {
                Text("Continue")
            }
            .buttonStyle(.borderedProminent)
            .tint(.blue)
            .controlSize(.large)
            .padding(.horizontal, 40)
            .padding(.bottom, 24)
        }
        .onAppear {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) {
                withAnimation(.easeInOut(duration: 0.4)) {
                    detectedApps = preferences.detectedApps
                    showApps = true
                }
            }
        }
    }

    // MARK: - Page 3: Learning Style (onboarding version)

    private var learningStyleOnboardingPage: some View {
        VStack(spacing: 14) {
            Spacer()

            ZStack {
                Circle()
                    .fill(Color.purple.opacity(0.12))
                    .frame(width: 56, height: 56)

                Image(systemName: "brain.head.profile.fill")
                    .font(.system(size: 26))
                    .foregroundStyle(
                        LinearGradient(colors: [.purple, .pink], startPoint: .topLeading, endPoint: .bottomTrailing)
                    )
            }

            Text("How do you learn best?")
                .font(.system(size: 20, weight: .bold, design: .rounded))
                .foregroundColor(.primary)

            Text("We'll tailor every guide to your style.")
                .font(.system(size: 13))
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 30)

            // Presets
            VStack(spacing: 8) {
                HStack(spacing: 8) {
                    presetButton(icon: "bolt.fill", label: "Minimal steps", value: "Show me minimal steps, 5-8 words per instruction, no explanations")
                    presetButton(icon: "book.fill", label: "Explain why", value: "Explain why each step matters with brief rationale")
                }
                HStack(spacing: 8) {
                    presetButton(icon: "eye.fill", label: "Visual cues", value: "Emphasize visual cues like colors, icons, and positions in instructions")
                    presetButton(icon: "graduationcap.fill", label: "Teach me", value: "Teach me the concepts behind each action so I learn, not just follow")
                }
            }
            .padding(.horizontal, 30)

            // Custom input
            HStack(spacing: 10) {
                Image(systemName: "pencil.line")
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(LinearGradient(colors: [.purple, .pink], startPoint: .topLeading, endPoint: .bottomTrailing))

                TextField("Or describe your own style...", text: $learningStyleText)
                    .textFieldStyle(.plain)
                    .font(.system(size: 14))
                    .focused($isTextFieldFocused)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color(nsColor: .controlBackgroundColor)))
            .overlay(
                RoundedRectangle(cornerRadius: 10)
                    .stroke(isTextFieldFocused ? Color.purple.opacity(0.5) : Color.secondary.opacity(0.2), lineWidth: 1)
                    .allowsHitTesting(false)
            )
            .padding(.horizontal, 30)

            Spacer()

            HStack(spacing: 10) {
                Button("Use Default") {
                    learningStyleText = ""
                    preferences.useDefaultLearningStyle()
                    preferences.completeOnboarding()
                    onComplete()
                }
                .buttonStyle(.bordered)
                .controlSize(.large)

                Button(learningStyleText.isEmpty ? "Get Started" : "Save & Start") {
                    preferences.learningStyle = learningStyleText
                    preferences.completeOnboarding()
                    onComplete()
                }
                .buttonStyle(.borderedProminent)
                .tint(.purple)
                .controlSize(.large)
            }
            .padding(.horizontal, 40)
            .padding(.bottom, 24)
        }
        .onAppear {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                isTextFieldFocused = true
            }
        }
    }

    // ================================================================
    // MARK: - Shared Components
    // ================================================================

    private func permissionRow(icon: String, label: String, detail: String, granted: Bool, action: @escaping () -> Void) -> some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .font(.system(size: 13))
                .foregroundColor(granted ? .green : .orange)
                .frame(width: 22)

            VStack(alignment: .leading, spacing: 1) {
                Text(label)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundColor(.primary)
                Text(detail)
                    .font(.system(size: 10))
                    .foregroundColor(.secondary)
            }

            Spacer()

            if granted {
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 14))
                    .foregroundColor(.green)
            } else {
                Button("Grant Access") {
                    action()
                }
                .buttonStyle(.bordered)
                .tint(.orange)
                .controlSize(.small)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(granted ? Color.green.opacity(0.06) : Color.orange.opacity(0.06))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(granted ? Color.green.opacity(0.15) : Color.orange.opacity(0.15), lineWidth: 1)
                .allowsHitTesting(false)
        )
    }

    private func presetButton(icon: String, label: String, value: String) -> some View {
        let isSelected = learningStyleText == value

        return Button(action: {
            withAnimation(.easeInOut(duration: 0.2)) {
                learningStyleText = value
            }
        }) {
            HStack(spacing: 6) {
                Image(systemName: icon)
                    .font(.system(size: 12))
                Text(label)
                    .font(.system(size: 12, weight: .medium))
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(.bordered)
        .tint(isSelected ? .purple : .secondary)
        .controlSize(.regular)
    }

    enum ScanStatus {
        case loading, done
    }

    private func systemInfoRow(icon: String, label: String, value: String, status: ScanStatus) -> some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .font(.system(size: 13))
                .foregroundColor(status == .done ? .green : .blue)
                .frame(width: 22)

            Text(label)
                .font(.system(size: 12, weight: .medium))
                .foregroundColor(.primary)

            Spacer()

            if status == .loading {
                ProgressView()
                    .progressViewStyle(.circular)
                    .scaleEffect(0.45)
            } else {
                Text(value)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundColor(.secondary)
            }

            if status == .done {
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 11))
                    .foregroundColor(.green)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(status == .done ? Color.green.opacity(0.06) : Color.blue.opacity(0.06))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(status == .done ? Color.green.opacity(0.15) : Color.blue.opacity(0.15), lineWidth: 1)
                .allowsHitTesting(false)
        )
    }

    // ================================================================
    // MARK: - Helpers
    // ================================================================

    private func refreshPermissions() {
        hasAccessibility = preferences.hasAccessibilityPermission
        hasScreenRecording = preferences.hasScreenRecordingPermission
        hasMicrophone = preferences.hasMicrophonePermission
    }

    private func pageIndex(_ page: OnboardingPage) -> Int {
        switch page {
        case .welcome: return 0
        case .systemScan: return 1
        case .learningStyle: return 2
        }
    }

    private func friendlyHardwareName(_ model: String) -> String {
        if model.contains("Mac") { return model }
        if model.starts(with: "MacBookPro") { return "MacBook Pro" }
        if model.starts(with: "MacBookAir") { return "MacBook Air" }
        if model.starts(with: "Macmini") { return "Mac mini" }
        if model.starts(with: "MacPro") { return "Mac Pro" }
        if model.starts(with: "iMac") { return "iMac" }
        if model.starts(with: "Mac") { return "Mac" }
        return model
    }
}
