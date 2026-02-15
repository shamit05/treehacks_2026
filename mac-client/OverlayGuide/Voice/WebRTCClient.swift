// Voice/WebRTCClient.swift
// Owner: Voice Pipeline
//
// WebRTC client for voice communication with the Modal OverlayGuide bot.
// Uses the real WebRTC framework (stasel/WebRTC) for proper peer connection.
//
// Flow:
//   1. Create RTCPeerConnection with audio transceiver
//   2. Create SDP offer
//   3. POST offer to Modal /offer endpoint
//   4. Receive SDP answer + session_id
//   5. Set remote description → ICE completes → audio flows
//
// The session_id is used by OverlaySyncClient to connect the control WebSocket.

import AVFoundation
import Combine
import Foundation
import WebRTC

// MARK: - WebRTCClient

class WebRTCClient: NSObject, ObservableObject {

    // MARK: - Published State

    @Published var connectionState: ConnectionState = .disconnected
    @Published var isListening: Bool = false
    @Published var sessionId: String?

    enum ConnectionState: Equatable {
        case disconnected
        case connecting
        case connected
        case reconnecting(attempt: Int)
        case failed(String)
    }

    // MARK: - Configuration

    private let botURL: URL
    private let maxReconnectAttempts = 3
    private let initialBackoff: TimeInterval = 1.0
    private let maxBackoff: TimeInterval = 8.0

    // MARK: - WebRTC

    private static let factory: RTCPeerConnectionFactory = {
        RTCInitializeSSL()
        let encoderFactory = RTCDefaultVideoEncoderFactory()
        let decoderFactory = RTCDefaultVideoDecoderFactory()
        return RTCPeerConnectionFactory(
            encoderFactory: encoderFactory,
            decoderFactory: decoderFactory
        )
    }()

    private var peerConnection: RTCPeerConnection?
    private var localAudioTrack: RTCAudioTrack?

    // MARK: - Session

    private var httpSession: URLSession
    private var reconnectTask: Task<Void, Never>?

    // MARK: - Init

    init(botURL: URL) {
        self.botURL = botURL
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 120
        self.httpSession = URLSession(configuration: config)
        super.init()
    }

    deinit {
        disconnect()
    }

    // MARK: - Connect

    /// Establish a WebRTC connection with the Modal bot via /offer signaling.
    func connect() async throws {
        guard connectionState == .disconnected || {
            if case .failed = connectionState { return true }
            return false
        }() else {
            print("[WebRTC] Already connected or connecting")
            return
        }

        await MainActor.run { connectionState = .connecting }

        do {
            // 1. Create peer connection
            let rtcConfig = RTCConfiguration()
            rtcConfig.iceServers = [
                RTCIceServer(urlStrings: ["stun:stun.l.google.com:19302"])
            ]
            rtcConfig.sdpSemantics = .unifiedPlan
            rtcConfig.continualGatheringPolicy = .gatherContinually

            let constraints = RTCMediaConstraints(
                mandatoryConstraints: nil,
                optionalConstraints: ["DtlsSrtpKeyAgreement": "true"]
            )

            guard let pc = WebRTCClient.factory.peerConnection(
                with: rtcConfig,
                constraints: constraints,
                delegate: self
            ) else {
                throw WebRTCError.signalFailed("Failed to create peer connection")
            }
            peerConnection = pc

            // 2. Add audio track (microphone → bot)
            let audioConstraints = RTCMediaConstraints(
                mandatoryConstraints: nil,
                optionalConstraints: nil
            )
            let audioSource = WebRTCClient.factory.audioSource(with: audioConstraints)
            let audioTrack = WebRTCClient.factory.audioTrack(
                with: audioSource,
                trackId: "audio0"
            )
            pc.add(audioTrack, streamIds: ["stream0"])
            localAudioTrack = audioTrack

            // Also add a receive-only audio transceiver so the bot can send TTS audio
            let transceiverInit = RTCRtpTransceiverInit()
            transceiverInit.direction = .sendRecv
            pc.addTransceiver(of: .audio, init: transceiverInit)

            // 3. Create SDP offer
            let offerConstraints = RTCMediaConstraints(
                mandatoryConstraints: [
                    "OfferToReceiveAudio": "true",
                    "OfferToReceiveVideo": "false",
                ],
                optionalConstraints: nil
            )

            let offer = try await withCheckedThrowingContinuation { (cont: CheckedContinuation<RTCSessionDescription, Error>) in
                pc.offer(for: offerConstraints) { sdp, error in
                    if let error = error {
                        cont.resume(throwing: error)
                    } else if let sdp = sdp {
                        cont.resume(returning: sdp)
                    } else {
                        cont.resume(throwing: WebRTCError.signalFailed("No SDP offer produced"))
                    }
                }
            }

            // Set local description
            try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
                pc.setLocalDescription(offer) { error in
                    if let error = error {
                        cont.resume(throwing: error)
                    } else {
                        cont.resume()
                    }
                }
            }

            // 4. POST offer to /offer endpoint
            let offerPayload: [String: Any] = [
                "sdp": offer.sdp,
                "type": "offer",
            ]

            let offerURL = botURL.appendingPathComponent("offer")
            var request = URLRequest(url: offerURL)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: offerPayload)

            print("[WebRTC] Sending SDP offer to \(offerURL)")

            let (data, response) = try await httpSession.data(for: request)
            guard let httpResponse = response as? HTTPURLResponse,
                  (200...299).contains(httpResponse.statusCode) else {
                let statusCode = (response as? HTTPURLResponse)?.statusCode ?? 0
                throw WebRTCError.signalFailed("Bad response from /offer: \(statusCode)")
            }

            guard let answerDict = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let answerSDP = answerDict["sdp"] as? String,
                  let answerType = answerDict["type"] as? String else {
                throw WebRTCError.signalFailed("Invalid SDP answer from server")
            }

            // Extract session_id for the control WebSocket
            let receivedSessionId = answerDict["session_id"] as? String
            print("[WebRTC] Got answer, session_id=\(receivedSessionId ?? "nil")")

            // 5. Set remote description
            let sdpType: RTCSdpType = answerType == "answer" ? .answer : .offer
            let remoteSDP = RTCSessionDescription(type: sdpType, sdp: answerSDP)

            try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
                pc.setRemoteDescription(remoteSDP) { error in
                    if let error = error {
                        cont.resume(throwing: error)
                    } else {
                        cont.resume()
                    }
                }
            }

            print("[WebRTC] Remote description set — ICE gathering in progress")

            await MainActor.run {
                self.sessionId = receivedSessionId
                self.connectionState = .connected
            }

        } catch {
            await MainActor.run {
                self.connectionState = .failed(error.localizedDescription)
            }
            throw error
        }
    }

    // MARK: - Reconnection

    func reconnect() {
        reconnectTask?.cancel()
        reconnectTask = Task { [weak self] in
            guard let self else { return }

            for attempt in 1...self.maxReconnectAttempts {
                await MainActor.run {
                    self.connectionState = .reconnecting(attempt: attempt)
                }

                let backoff = min(
                    self.initialBackoff * pow(2.0, Double(attempt - 1)),
                    self.maxBackoff
                )
                print("[WebRTC] Reconnect attempt \(attempt)/\(self.maxReconnectAttempts) in \(backoff)s")
                try? await Task.sleep(nanoseconds: UInt64(backoff * 1_000_000_000))
                if Task.isCancelled { return }

                do {
                    try await self.connect()
                    print("[WebRTC] Reconnected on attempt \(attempt)")
                    return
                } catch {
                    print("[WebRTC] Reconnect attempt \(attempt) failed: \(error)")
                }
            }

            await MainActor.run {
                self.connectionState = .failed(
                    "Voice unavailable after \(self.maxReconnectAttempts) attempts. Use text input."
                )
            }
        }
    }

    // MARK: - Listening control

    func startListening() {
        guard case .connected = connectionState else {
            print("[WebRTC] Cannot start listening — not connected")
            return
        }
        localAudioTrack?.isEnabled = true
        isListening = true
        print("[WebRTC] Listening started (audio track enabled)")
    }

    func stopListening() {
        localAudioTrack?.isEnabled = false
        isListening = false
        print("[WebRTC] Listening stopped (audio track disabled)")
    }

    // MARK: - Disconnect

    func disconnect() {
        reconnectTask?.cancel()
        reconnectTask = nil

        localAudioTrack?.isEnabled = false
        localAudioTrack = nil

        peerConnection?.close()
        peerConnection = nil

        connectionState = .disconnected
        sessionId = nil
        isListening = false
        print("[WebRTC] Disconnected and resources released")
    }
}

// MARK: - RTCPeerConnectionDelegate

extension WebRTCClient: RTCPeerConnectionDelegate {

    func peerConnection(_ peerConnection: RTCPeerConnection, didChange stateChanged: RTCSignalingState) {
        print("[WebRTC] Signaling state: \(stateChanged.rawValue)")
    }

    func peerConnection(_ peerConnection: RTCPeerConnection, didAdd stream: RTCMediaStream) {
        print("[WebRTC] Remote stream added with \(stream.audioTracks.count) audio tracks")
    }

    func peerConnection(_ peerConnection: RTCPeerConnection, didRemove stream: RTCMediaStream) {
        print("[WebRTC] Remote stream removed")
    }

    func peerConnectionShouldNegotiate(_ peerConnection: RTCPeerConnection) {
        print("[WebRTC] Negotiation needed")
    }

    func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceConnectionState) {
        print("[WebRTC] ICE connection state: \(newState.rawValue)")
        Task { @MainActor in
            switch newState {
            case .connected, .completed:
                self.connectionState = .connected
            case .disconnected:
                self.connectionState = .disconnected
                self.reconnect()
            case .failed:
                self.connectionState = .failed("ICE connection failed")
            default:
                break
            }
        }
    }

    func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceGatheringState) {
        print("[WebRTC] ICE gathering state: \(newState.rawValue)")
    }

    func peerConnection(_ peerConnection: RTCPeerConnection, didGenerate candidate: RTCIceCandidate) {
        // With SmallWebRTC, ICE candidates are bundled in the SDP,
        // so we don't need to send them separately (trickle ICE is not used).
        print("[WebRTC] ICE candidate generated: \(candidate.sdpMid ?? "nil")")
    }

    func peerConnection(_ peerConnection: RTCPeerConnection, didRemove candidates: [RTCIceCandidate]) {
        print("[WebRTC] ICE candidates removed")
    }

    func peerConnection(_ peerConnection: RTCPeerConnection, didOpen dataChannel: RTCDataChannel) {
        print("[WebRTC] Data channel opened: \(dataChannel.label)")
    }
}

// MARK: - Errors

enum WebRTCError: Error, LocalizedError {
    case signalFailed(String)
    case connectionLost
    case audioCaptureFailed

    var errorDescription: String? {
        switch self {
        case .signalFailed(let msg): return "WebRTC signaling failed: \(msg)"
        case .connectionLost: return "WebRTC connection lost"
        case .audioCaptureFailed: return "Failed to capture audio"
        }
    }
}
