// Input/VoiceInputService.swift
// Owner: Eng 2 (Capture + Input)
//
// Isolated voice-input service using Apple Speech framework.
// Uses nonisolated audio plumbing to avoid main-actor data races
// between the audio tap (background thread) and the recognizer.

import AVFoundation
import Foundation
import Speech

@MainActor
protocol VoiceInputProviding: AnyObject {
    var transcript: String { get }
    var isListening: Bool { get }
    var isAvailable: Bool { get }
    func requestPermissions() async -> Bool
    func startListening() throws
    func stopListening()
}

enum VoiceInputError: LocalizedError {
    case recognizerUnavailable
    case permissionsDenied
    case audioEngineStartFailed

    var errorDescription: String? {
        switch self {
        case .recognizerUnavailable:
            return "Speech recognition is unavailable on this device."
        case .permissionsDenied:
            return "Microphone or speech recognition permission is denied."
        case .audioEngineStartFailed:
            return "Could not start the microphone audio engine."
        }
    }
}

/// Speech-to-text service.
///
/// **Threading model**: The audio engine tap fires on a realtime audio thread
/// and must call `recognitionRequest.append(buffer)` synchronously there —
/// hopping to `@MainActor` would introduce latency and risk dropping frames.
/// So the engine, request, and task are **not** actor-isolated. Only the
/// `@Published` properties that the UI observes are updated on the main actor.
final class AppleVoiceInputService: NSObject, ObservableObject {

    // MARK: – Published (main-actor, observed by SwiftUI)

    @MainActor @Published private(set) var transcript: String = ""
    @MainActor @Published private(set) var isListening: Bool = false
    @MainActor @Published private(set) var isAvailable: Bool = true

    // MARK: – Audio / recognition (nonisolated, used from audio thread)

    private let audioEngine = AVAudioEngine()
    private let recognizer: SFSpeechRecognizer?
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?

    override init() {
        self.recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
        super.init()
        let avail = recognizer?.isAvailable ?? false
        print("[Voice] init: recognizer available=\(avail), locale=\(recognizer?.locale.identifier ?? "nil")")
        Task { @MainActor in self.isAvailable = avail }
    }

    // MARK: – Permissions

    @MainActor
    func requestPermissions() async -> Bool {
        let speechAuth = await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status)
            }
        }
        print("[Voice] speechAuth=\(speechAuth.rawValue)")

        let micStatus = AVCaptureDevice.authorizationStatus(for: .audio)
        let micAuth: Bool
        if micStatus == .authorized {
            micAuth = true
        } else if micStatus == .notDetermined {
            micAuth = await AVCaptureDevice.requestAccess(for: .audio)
        } else {
            micAuth = false
        }
        print("[Voice] micAuth=\(micAuth)")
        return speechAuth == .authorized && micAuth
    }

    // MARK: – Start / Stop

    @MainActor
    func startListening() throws {
        guard let recognizer, recognizer.isAvailable else {
            isAvailable = false
            throw VoiceInputError.recognizerUnavailable
        }

        guard SFSpeechRecognizer.authorizationStatus() == .authorized,
              AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
            throw VoiceInputError.permissionsDenied
        }

        // Clean up any previous session
        stopListeningInternal()

        // Set up recognition request
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        // Allow server-side recognition (better accuracy, needs network)
        if #available(macOS 13, *) {
            request.requiresOnDeviceRecognition = false
        }
        recognitionRequest = request

        // Set up audio tap
        let inputNode = audioEngine.inputNode
        let recordingFormat = inputNode.outputFormat(forBus: 0)
        print("[Voice] audio format: \(recordingFormat.sampleRate)Hz, \(recordingFormat.channelCount)ch")

        guard recordingFormat.channelCount > 0, recordingFormat.sampleRate > 0 else {
            throw VoiceInputError.audioEngineStartFailed
        }

        inputNode.removeTap(onBus: 0)
        // The tap fires on a realtime audio thread — append directly (no actor hop)
        inputNode.installTap(onBus: 0, bufferSize: 4096, format: recordingFormat) { [weak self] buffer, _ in
            self?.recognitionRequest?.append(buffer)
        }

        audioEngine.prepare()
        do {
            try audioEngine.start()
            print("[Voice] audio engine started")
        } catch {
            print("[Voice] audio engine start failed: \(error)")
            inputNode.removeTap(onBus: 0)
            throw VoiceInputError.audioEngineStartFailed
        }

        // Start recognition task
        recognitionTask = recognizer.recognitionTask(with: request) { [weak self] result, error in
            guard let self else { return }

            if let error {
                print("[Voice] recognition error: \(error.localizedDescription)")
            }

            if let result {
                let text = result.bestTranscription.formattedString
                let isFinal = result.isFinal
                print("[Voice] transcript: \"\(text)\" final=\(isFinal)")
                Task { @MainActor in
                    self.transcript = text
                }
                if isFinal {
                    Task { @MainActor in
                        self.stopListening()
                    }
                }
            } else if error != nil {
                // Error with no result — recognition failed
                Task { @MainActor in
                    self.stopListening()
                }
            }
        }

        transcript = ""
        isListening = true
        print("[Voice] listening started")
    }

    @MainActor
    func stopListening() {
        stopListeningInternal()
        isListening = false
        print("[Voice] listening stopped, transcript=\"\(transcript)\"")
    }

    /// Non-actor-isolated teardown (safe to call from any context).
    private func stopListeningInternal() {
        recognitionTask?.cancel()
        recognitionTask = nil
        recognitionRequest?.endAudio()
        recognitionRequest = nil
        if audioEngine.isRunning {
            audioEngine.stop()
        }
        audioEngine.inputNode.removeTap(onBus: 0)
    }
}
