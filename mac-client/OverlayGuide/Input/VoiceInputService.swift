// Input/VoiceInputService.swift
// Owner: Eng 2 (Capture + Input)
//
// Isolated voice-input framework so UI and state-machine changes stay minimal.
// This file can evolve independently to support other STT providers.

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

@MainActor
final class AppleVoiceInputService: NSObject, ObservableObject, VoiceInputProviding {
    @Published private(set) var transcript: String = ""
    @Published private(set) var isListening: Bool = false
    @Published private(set) var isAvailable: Bool = true

    private let audioEngine = AVAudioEngine()
    private let recognizer: SFSpeechRecognizer? = SFSpeechRecognizer(locale: Locale.current)
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?

    override init() {
        super.init()
        isAvailable = recognizer?.isAvailable ?? false
    }

    func requestPermissions() async -> Bool {
        let speechAuth = await requestSpeechAuthorization()
        let micAuth = await requestMicrophoneAuthorization()
        return speechAuth && micAuth
    }

    func startListening() throws {
        guard let recognizer, recognizer.isAvailable else {
            isAvailable = false
            throw VoiceInputError.recognizerUnavailable
        }

        guard SFSpeechRecognizer.authorizationStatus() == .authorized,
              AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
            throw VoiceInputError.permissionsDenied
        }

        stopListening()

        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        recognitionRequest = request

        let inputNode = audioEngine.inputNode
        let recordingFormat = inputNode.outputFormat(forBus: 0)
        inputNode.removeTap(onBus: 0)
        inputNode.installTap(onBus: 0, bufferSize: 1024, format: recordingFormat) { [weak self] buffer, _ in
            self?.recognitionRequest?.append(buffer)
        }

        audioEngine.prepare()
        do {
            try audioEngine.start()
        } catch {
            throw VoiceInputError.audioEngineStartFailed
        }

        recognitionTask = recognizer.recognitionTask(with: request) { [weak self] result, error in
            guard let self else { return }
            if let result {
                Task { @MainActor in
                    self.transcript = result.bestTranscription.formattedString
                }
            }
            if error != nil || (result?.isFinal ?? false) {
                Task { @MainActor in
                    self.stopListening()
                }
            }
        }

        isListening = true
    }

    func stopListening() {
        if audioEngine.isRunning {
            audioEngine.stop()
        }
        audioEngine.inputNode.removeTap(onBus: 0)
        recognitionRequest?.endAudio()
        recognitionTask?.cancel()
        recognitionRequest = nil
        recognitionTask = nil
        isListening = false
    }

    private func requestSpeechAuthorization() async -> Bool {
        let status = await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { authStatus in
                continuation.resume(returning: authStatus)
            }
        }
        return status == .authorized
    }

    private func requestMicrophoneAuthorization() async -> Bool {
        let current = AVCaptureDevice.authorizationStatus(for: .audio)
        if current == .authorized {
            return true
        }
        if current == .denied || current == .restricted {
            return false
        }
        return await AVCaptureDevice.requestAccess(for: .audio)
    }
}
