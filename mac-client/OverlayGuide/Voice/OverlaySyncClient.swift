// Voice/OverlaySyncClient.swift
// Owner: Voice Pipeline
//
// WebSocket control channel between the Mac client and the Modal bot.
// Connects to /ws/{session_id} on the Modal ASGI app.
//
// Inbound messages (server → client):
//   - {"type": "step_plan", "plan": {...}}  → Full StepPlan for overlay
//   - {"type": "request_screenshot"}        → Bot needs a fresh screenshot
//
// Outbound messages (client → server):
//   - {"type": "screenshot", "data": "<base64>", "image_size": {"w": int, "h": int}}
//
// Uses Combine publishers so SwiftUI views and the state machine can
// react to plan updates on the main thread.

import Combine
import Foundation

// MARK: - OverlaySyncClient

class OverlaySyncClient: ObservableObject {

    // MARK: - Published State

    @Published var receivedPlan: StepPlan?
    @Published var connectionState: SyncConnectionState = .disconnected
    @Published var lastError: String?

    enum SyncConnectionState: Equatable {
        case disconnected
        case connecting
        case connected
        case reconnecting(attempt: Int)
        case failed(String)
    }

    // MARK: - Combine

    /// Fires when a complete plan is received from the bot
    let planReceivedSubject = PassthroughSubject<StepPlan, Never>()

    /// Fires when the bot requests a screenshot
    let screenshotRequestedSubject = PassthroughSubject<Void, Never>()

    // MARK: - Configuration

    private let maxReconnectAttempts = 3
    private let initialBackoff: TimeInterval = 1.0
    private let maxBackoff: TimeInterval = 4.0

    // MARK: - Session

    private var webSocket: URLSessionWebSocketTask?
    private var session: URLSession
    private var receiveTask: Task<Void, Never>?
    private var reconnectTask: Task<Void, Never>?
    private var syncURL: URL?

    // MARK: - Init

    init() {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 30
        self.session = URLSession(configuration: config)
    }

    deinit {
        disconnect()
    }

    // MARK: - Connect

    /// Connect to the control WebSocket at /ws/{session_id}
    /// - Parameter baseURL: The Modal ASGI app base URL (e.g. "wss://xxx.modal.run")
    /// - Parameter sessionId: The session ID returned from /offer
    func connect(baseURL: URL, sessionId: String) async throws {
        // Build WebSocket URL: wss://{host}/ws/{session_id}
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)
        components?.scheme = baseURL.scheme == "https" ? "wss" : "ws"
        components?.path = "/ws/\(sessionId)"

        guard let wsURL = components?.url else {
            throw OverlaySyncError.invalidURL(baseURL.absoluteString)
        }

        self.syncURL = wsURL
        await MainActor.run { connectionState = .connecting }

        webSocket = session.webSocketTask(with: wsURL)
        webSocket?.resume()

        await MainActor.run { connectionState = .connected }
        print("[OverlaySync] Connected to \(wsURL)")

        startReceiving()
    }

    // MARK: - Send screenshot to bot (via relay through ASGI)

    /// Send a screenshot to the bot via the control WebSocket.
    /// - Parameters:
    ///   - imageData: PNG image data
    ///   - imageSize: Screen dimensions {"w": width, "h": height}
    func sendScreenshot(_ imageData: Data, imageSize: (w: Int, h: Int)) async {
        guard let ws = webSocket else {
            print("[OverlaySync] Cannot send screenshot — not connected")
            return
        }

        let payload: [String: Any] = [
            "type": "screenshot",
            "data": imageData.base64EncodedString(),
            "image_size": ["w": imageSize.w, "h": imageSize.h],
        ]

        do {
            let jsonData = try JSONSerialization.data(withJSONObject: payload)
            try await ws.send(.data(jsonData))
            print("[OverlaySync] Screenshot sent (\(imageData.count) bytes)")
        } catch {
            print("[OverlaySync] Failed to send screenshot: \(error)")
        }
    }

    // MARK: - Receive messages

    private func startReceiving() {
        receiveTask = Task { [weak self] in
            await self?.receiveLoop()
        }
    }

    private func receiveLoop() async {
        while let ws = webSocket {
            do {
                let message = try await ws.receive()
                switch message {
                case .string(let text):
                    await handleMessage(text)
                case .data(let data):
                    if let text = String(data: data, encoding: .utf8) {
                        await handleMessage(text)
                    }
                @unknown default:
                    break
                }
            } catch {
                print("[OverlaySync] Receive error: \(error)")
                await MainActor.run { [weak self] in
                    self?.connectionState = .disconnected
                }
                reconnect()
                break
            }
        }
    }

    private func handleMessage(_ text: String) async {
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = json["type"] as? String else {
            print("[OverlaySync] Failed to parse message")
            return
        }

        switch type {
        case "step_plan":
            // Decode the plan from the "plan" key
            if let planData = json["plan"],
               let planJSON = try? JSONSerialization.data(withJSONObject: planData),
               let plan = try? JSONDecoder().decode(StepPlan.self, from: planJSON) {
                await MainActor.run { [weak self] in
                    self?.receivedPlan = plan
                    self?.planReceivedSubject.send(plan)
                }
                print("[OverlaySync] Received plan: \(plan.steps.count) steps")
            } else {
                print("[OverlaySync] Failed to decode step_plan")
            }

        case "request_screenshot":
            // Bot is asking for a fresh screenshot
            print("[OverlaySync] Screenshot requested by bot")
            await MainActor.run { [weak self] in
                self?.screenshotRequestedSubject.send()
            }

        default:
            print("[OverlaySync] Unknown message type: \(type)")
        }
    }

    // MARK: - Reconnection

    func reconnect() {
        guard let url = syncURL else { return }

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
                print("[OverlaySync] Reconnect attempt \(attempt) in \(backoff)s")
                try? await Task.sleep(nanoseconds: UInt64(backoff * 1_000_000_000))
                if Task.isCancelled { return }

                do {
                    // Reconstruct the connection (url already stored)
                    self.webSocket = self.session.webSocketTask(with: url)
                    self.webSocket?.resume()
                    await MainActor.run { self.connectionState = .connected }
                    self.startReceiving()
                    print("[OverlaySync] Reconnected on attempt \(attempt)")
                    return
                } catch {
                    print("[OverlaySync] Reconnect attempt \(attempt) failed: \(error)")
                }
            }

            await MainActor.run {
                self.connectionState = .failed(
                    "Overlay sync unavailable after \(self.maxReconnectAttempts) attempts."
                )
            }
        }
    }

    // MARK: - Disconnect

    func disconnect() {
        reconnectTask?.cancel()
        reconnectTask = nil
        receiveTask?.cancel()
        receiveTask = nil
        webSocket?.cancel(with: .goingAway, reason: nil)
        webSocket = nil
        connectionState = .disconnected
        print("[OverlaySync] Disconnected")
    }

    // MARK: - Reset

    func reset() {
        receivedPlan = nil
        lastError = nil
    }
}

// MARK: - Errors

enum OverlaySyncError: Error, LocalizedError {
    case invalidURL(String)
    case connectionFailed

    var errorDescription: String? {
        switch self {
        case .invalidURL(let url): return "Invalid overlay sync URL: \(url)"
        case .connectionFailed: return "Failed to connect to overlay sync"
        }
    }
}
