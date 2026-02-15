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

    // Hotkey configs
    private static let hotkeySignature: OSType = 0x4F564947  // "OVIG"
    private static let hotkeyPrimaryID: UInt32 = 1
    private static let hotkeySecondaryID: UInt32 = 2
    private static let primaryKeyCode: UInt16 = 31   // kVK_ANSI_O
    private static let primaryModifiers: UInt32 = UInt32(cmdKey | optionKey)
    private static let secondaryKeyCode: UInt16 = 5  // kVK_ANSI_G
    private static let secondaryModifiers: UInt32 = UInt32(cmdKey | shiftKey)

    init(stateMachine: GuidanceStateMachine) {
        self.stateMachine = stateMachine
    }

    // MARK: - Public

    func start() {
        registerHotkey()
        registerFallbackGlobalKeyMonitor()
        startMouseTap()
        print("[InputMonitor] Started. Hotkeys: Cmd+Option+O or Cmd+Shift+G.")
    }

    func stop() {
        stopMouseTap()
        unregisterHotkey()
        unregisterFallbackGlobalKeyMonitor()
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
            { _, _, userData -> OSStatus in
                guard let userData = userData else { return noErr }
                let monitor = Unmanaged<GlobalInputMonitor>.fromOpaque(userData).takeUnretainedValue()
                DispatchQueue.main.async {
                    monitor.triggerHotkey()
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
            if primaryMatch || secondaryMatch {
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

    private func triggerHotkey() {
        if let last = lastHotkeyAt, Date().timeIntervalSince(last) < 0.35 {
            return
        }
        lastHotkeyAt = Date()
        // Do NOT activate OverlayGuide — the target app should stay active
        // so its menu bar remains visible and screenshots capture it correctly.
        stateMachine.toggleOverlay()
    }

    // MARK: - Mouse Click Detection

    private func startMouseTap() {
        guard AXIsProcessTrusted() else {
            print("[InputMonitor] Accessibility permission required for click detection. Grant access in System Settings > Privacy & Security > Accessibility.")
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
                DispatchQueue.main.async {
                    if case .guiding = monitor.stateMachine.phase {
                        monitor.stateMachine.handleClick(at: location)
                    }
                }
                return Unmanaged.passRetained(event)
            },
            userInfo: selfPtr
        ) else {
            print("[InputMonitor] Failed to create event tap. Check Accessibility permission.")
            return
        }

        eventTap = tap
        runLoopSource = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), runLoopSource, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
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
