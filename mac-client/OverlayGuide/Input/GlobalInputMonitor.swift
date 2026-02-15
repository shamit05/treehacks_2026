// Input/GlobalInputMonitor.swift
// Owner: Eng 2 (Capture + Input)
//
// Detects:
// 1. Global hotkey (Cmd+Option+O) to launch/dismiss the overlay
// 2. Mouse clicks to check if user clicked inside a target rect
//
// Uses Carbon RegisterEventHotKey for reliable global hotkeys and
// CGEventTap for mouse detection.
// Accessibility permission is required for click detection, but not for hotkey registration.

import AppKit
import Carbon
import Carbon.HIToolbox
import Foundation

class GlobalInputMonitor {

    private let stateMachine: GuidanceStateMachine
    private var eventTap: CFMachPort?
    private var runLoopSource: CFRunLoopSource?
    private var eventHandlerRef: EventHandlerRef?
    private var hotkeyRefs: [EventHotKeyRef] = []
    private var globalKeyMonitor: Any?
    private var lastHotkeyAt: Date?

    /// Called when the user presses the preferences hotkey (Cmd+Option+,)
    var onOpenPreferences: (() -> Void)?

    // Hotkey configs
    private static let hotkeySignature: OSType = 0x4F564947  // "OVIG"
    private static let hotkeyPrimaryID: UInt32 = 1
    private static let hotkeySecondaryID: UInt32 = 2
    private static let hotkeyPrefsID: UInt32 = 3
    private static let primaryKeyCode: UInt16 = 31   // kVK_ANSI_O
    private static let primaryModifiers: UInt32 = UInt32(cmdKey | optionKey)
    private static let secondaryKeyCode: UInt16 = 31 // kVK_ANSI_O (Cmd+Shift+O)
    private static let secondaryModifiers: UInt32 = UInt32(cmdKey | shiftKey)
    private static let prefsKeyCode: UInt16 = 43     // kVK_ANSI_Comma
    private static let prefsModifiers: UInt32 = UInt32(cmdKey | optionKey)

    init(stateMachine: GuidanceStateMachine) {
        self.stateMachine = stateMachine
    }

    // MARK: - Public

    func start() {
        registerHotkey()
        registerFallbackGlobalKeyMonitor()
        startMouseTap()
        registerEscapeMonitor()
        print("[InputMonitor] Started. Hotkeys: Cmd+Option+O or Cmd+Shift+O (overlay), Cmd+Option+, (preferences). Escape to dismiss.")
    }

    func stop() {
        stopMouseTap()
        unregisterHotkey()
        unregisterFallbackGlobalKeyMonitor()
        unregisterEscapeMonitor()
        print("[InputMonitor] Stopped.")
    }

    // MARK: - Hotkey Registration (Carbon)

    private func registerHotkey() {
        var eventType = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: OSType(kEventHotKeyPressed)
        )

        let selfPtr = Unmanaged.passUnretained(self).toOpaque()
        var handlerRef: EventHandlerRef?
        let handlerStatus = InstallEventHandler(
            GetApplicationEventTarget(),
            { _, inEvent, userData -> OSStatus in
                guard let userData = userData, let inEvent = inEvent else { return noErr }
                let monitor = Unmanaged<GlobalInputMonitor>.fromOpaque(userData).takeUnretainedValue()

                // Read which hotkey was pressed
                var hotkeyID = EventHotKeyID()
                GetEventParameter(
                    inEvent,
                    EventParamName(kEventParamDirectObject),
                    EventParamType(typeEventHotKeyID),
                    nil,
                    MemoryLayout<EventHotKeyID>.size,
                    nil,
                    &hotkeyID
                )

                DispatchQueue.main.async {
                    if hotkeyID.id == GlobalInputMonitor.hotkeyPrefsID {
                        monitor.triggerPrefsHotkey()
                    } else {
                        monitor.triggerHotkey()
                    }
                }
                return noErr
            },
            1,
            &eventType,
            selfPtr,
            &handlerRef
        )
        guard handlerStatus == noErr else {
            print("[InputMonitor] Failed to install hotkey handler: \(handlerStatus)")
            return
        }
        eventHandlerRef = handlerRef

        hotkeyRefs.removeAll()
        registerHotKey(
            id: Self.hotkeyPrimaryID,
            keyCode: Self.primaryKeyCode,
            modifiers: Self.primaryModifiers
        )
        registerHotKey(
            id: Self.hotkeySecondaryID,
            keyCode: Self.secondaryKeyCode,
            modifiers: Self.secondaryModifiers
        )
        registerHotKey(
            id: Self.hotkeyPrefsID,
            keyCode: Self.prefsKeyCode,
            modifiers: Self.prefsModifiers
        )
    }

    private func unregisterHotkey() {
        for ref in hotkeyRefs {
            UnregisterEventHotKey(ref)
        }
        hotkeyRefs.removeAll()
        if let handler = eventHandlerRef {
            RemoveEventHandler(handler)
            eventHandlerRef = nil
        }
    }

    private func registerHotKey(id: UInt32, keyCode: UInt16, modifiers: UInt32) {
        var hotKeyID = EventHotKeyID()
        hotKeyID.signature = Self.hotkeySignature
        hotKeyID.id = id
        var ref: EventHotKeyRef?
        let status = RegisterEventHotKey(
            UInt32(keyCode),
            modifiers,
            hotKeyID,
            GetApplicationEventTarget(),
            0,
            &ref
        )
        guard status == noErr, let ref else {
            print("[InputMonitor] Failed to register hotkey id=\(id), status=\(status)")
            return
        }
        hotkeyRefs.append(ref)
    }

    private func registerFallbackGlobalKeyMonitor() {
        // Fallback path in case Carbon hotkeys are blocked in some environments.
        globalKeyMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self else { return }
            let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
            let primaryMatch = event.keyCode == Self.primaryKeyCode &&
                flags.contains(.command) && flags.contains(.option)
            let secondaryMatch = event.keyCode == Self.secondaryKeyCode &&
                flags.contains(.command) && flags.contains(.shift)
            let prefsMatch = event.keyCode == Self.prefsKeyCode &&
                flags.contains(.command) && flags.contains(.option)
            if prefsMatch {
                DispatchQueue.main.async {
                    self.triggerPrefsHotkey()
                }
            } else if primaryMatch || secondaryMatch {
                DispatchQueue.main.async {
                    self.triggerHotkey()
                }
            }
        }
    }

    private func unregisterFallbackGlobalKeyMonitor() {
        if let globalKeyMonitor {
            NSEvent.removeMonitor(globalKeyMonitor)
            self.globalKeyMonitor = nil
        }
    }

    // MARK: - Escape Key (always dismisses)

    private var escapeMonitor: Any?
    private var localEscapeMonitor: Any?

    private func registerEscapeMonitor() {
        // Global monitor (when The Cookbook is not focused)
        escapeMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            if event.keyCode == 53 { // kVK_Escape
                DispatchQueue.main.async {
                    self?.handleEscape()
                }
            }
        }
        // Local monitor (when The Cookbook panel is focused)
        localEscapeMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            if event.keyCode == 53 { // kVK_Escape
                DispatchQueue.main.async {
                    self?.handleEscape()
                }
                return nil // consume the event
            }
            return event
        }
    }

    private func unregisterEscapeMonitor() {
        if let escapeMonitor {
            NSEvent.removeMonitor(escapeMonitor)
            self.escapeMonitor = nil
        }
        if let localEscapeMonitor {
            NSEvent.removeMonitor(localEscapeMonitor)
            self.localEscapeMonitor = nil
        }
    }

    private func handleEscape() {
        // Escape always resets the overlay, regardless of current phase
        let phase = stateMachine.phase
        guard phase != .idle else { return }
        print("[InputMonitor] Escape pressed — resetting from phase: \(phase)")
        stateMachine.reset()
    }

    private func triggerHotkey() {
        if let last = lastHotkeyAt, Date().timeIntervalSince(last) < 0.35 {
            return
        }
        lastHotkeyAt = Date()
        // Do NOT activate The Cookbook — the target app should stay active
        // so its menu bar remains visible and screenshots capture it correctly.
        stateMachine.toggleOverlay()
    }

    private func triggerPrefsHotkey() {
        if let last = lastHotkeyAt, Date().timeIntervalSince(last) < 0.35 {
            return
        }
        lastHotkeyAt = Date()
        print("[InputMonitor] Preferences hotkey pressed (Cmd+Option+,)")
        onOpenPreferences?()
    }

    // MARK: - Mouse Click Detection

    /// Write debug info to a file (since print() is lost when launched via `open`)
    private static func debugLog(_ msg: String) {
        let line = "[\(ISO8601DateFormatter().string(from: Date()))] \(msg)\n"
        if let data = line.data(using: .utf8) {
            let url = URL(fileURLWithPath: "/tmp/overlayguide_clicks.log")
            if let handle = try? FileHandle(forWritingTo: url) {
                handle.seekToEndOfFile()
                handle.write(data)
                handle.closeFile()
            } else {
                try? data.write(to: url)
            }
        }
    }

    private func startMouseTap() {
        let trusted = AXIsProcessTrusted()
        Self.debugLog("startMouseTap: AXIsProcessTrusted=\(trusted)")
        guard trusted else {
            Self.debugLog("ERROR: Accessibility permission NOT granted. Click detection disabled.")
            print("[InputMonitor] Accessibility permission required for click detection.")
            return
        }

        let eventMask = (1 << CGEventType.leftMouseDown.rawValue)
        let selfPtr = Unmanaged.passUnretained(self).toOpaque()

        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .defaultTap,
            eventsOfInterest: CGEventMask(eventMask),
            callback: { proxy, type, event, refcon -> Unmanaged<CGEvent>? in
                guard let refcon = refcon else { return Unmanaged.passRetained(event) }
                let monitor = Unmanaged<GlobalInputMonitor>.fromOpaque(refcon).takeUnretainedValue()
                let location = event.location

                // Capture phase NOW (on the event tap thread) — don't wait for main queue
                // because the phase might change between now and the async dispatch.
                let currentPhase = monitor.stateMachine.phase
                GlobalInputMonitor.debugLog("CLICK at (\(location.x), \(location.y)) phase=\(currentPhase)")

                if case .guiding = currentPhase {
                    DispatchQueue.main.async {
                        monitor.stateMachine.handleClick(at: location)
                    }
                } else {
                    GlobalInputMonitor.debugLog("  -> ignored (not in .guiding phase)")
                }
                return Unmanaged.passRetained(event)
            },
            userInfo: selfPtr
        ) else {
            Self.debugLog("ERROR: Failed to create event tap")
            print("[InputMonitor] Failed to create event tap. Check Accessibility permission.")
            return
        }

        eventTap = tap
        runLoopSource = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), runLoopSource, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
        Self.debugLog("Event tap created and enabled successfully")
    }

    private func stopMouseTap() {
        if let tap = eventTap {
            CGEvent.tapEnable(tap: tap, enable: false)
        }
        if let source = runLoopSource {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), source, .commonModes)
        }
        eventTap = nil
        runLoopSource = nil
    }
}
