// Input/GlobalInputMonitor.swift
// Owner: Eng 2 (Capture + Input)
//
// Detects:
// 1. Global hotkey (e.g., Cmd+Shift+G) to launch/dismiss the overlay
// 2. Mouse clicks to check if user clicked inside a target rect
//
// Uses CGEventTap for mouse detection and Carbon/HotKey for the global shortcut.
// Notifies the state machine on relevant events.

import AppKit
import Carbon
import Foundation

class GlobalInputMonitor {

    private let stateMachine: GuidanceStateMachine
    private var eventTap: CFMachPort?
    private var runLoopSource: CFRunLoopSource?

    // Hotkey config — Cmd+Shift+G
    static let hotkeyModifiers: UInt32 = UInt32(cmdKey | shiftKey)
    static let hotkeyKeyCode: UInt32 = 5  // 'G' key

    private var hotkeyRef: EventHotKeyRef?

    init(stateMachine: GuidanceStateMachine) {
        self.stateMachine = stateMachine
    }

    // MARK: - Public

    func start() {
        registerHotkey()
        startMouseTap()
        print("[InputMonitor] Started listening for hotkey and clicks.")
    }

    func stop() {
        stopMouseTap()
        unregisterHotkey()
        print("[InputMonitor] Stopped.")
    }

    // MARK: - Hotkey Registration

    private func registerHotkey() {
        // TODO: Register global hotkey using Carbon API or KeyboardShortcuts package
        // On trigger, call stateMachine.toggleOverlay()
    }

    private func unregisterHotkey() {
        // TODO: Unregister the hotkey
    }

    // MARK: - Mouse Click Detection

    private func startMouseTap() {
        // TODO: Create CGEventTap for .leftMouseDown
        // In the callback:
        //   1. Get click location in screen coordinates
        //   2. Call stateMachine.handleClick(at: point)
    }

    private func stopMouseTap() {
        if let tap = eventTap {
            CGEvent.tapEnable(tap: tap, enable: false)
        }
        if let source = runLoopSource {
            CFRunLoopRemoveSource(CFRunLoopGetCurrent(), source, .commonModes)
        }
        eventTap = nil
        runLoopSource = nil
    }
}
