// Networking/AgentNetworkClient.swift
// Owner: Eng 4 (State + Integration)
//
// HTTP client for communicating with the Python agent-server.
// Sends screenshots + goals, receives StepPlan JSON.
// Includes request IDs, timeouts, and retry logic.

import Foundation

class AgentNetworkClient {

    private let baseURL: URL
    private let session: URLSession
    private let timeout: TimeInterval = 10.0  // 10s for agent requests

    init(baseURL: URL = URL(string: "http://localhost:8000")!) {
        self.baseURL = baseURL
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = timeout
        self.session = URLSession(configuration: config)
    }

    /// POST /plan — send goal + screenshot, receive StepPlan
    func requestPlan(
        goal: String,
        screenshotData: Data,
        imageSize: ImageSize,
        learningProfile: LearningProfile? = nil,
        appContext: AppContext? = nil
    ) async throws -> StepPlan {
        let url = baseURL.appendingPathComponent("plan")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"

        // Request ID for debugging
        let requestId = UUID().uuidString
        request.setValue(requestId, forHTTPHeaderField: "X-Request-ID")
        print("[Network] POST /plan requestId=\(requestId)")

        // Build multipart form data
        let boundary = UUID().uuidString
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")

        var body = Data()

        // Goal field
        body.appendMultipart(name: "goal", value: goal, boundary: boundary)

        // Image size field
        let imageSizeJSON = try JSONEncoder().encode(imageSize)
        body.appendMultipart(name: "image_size", value: String(data: imageSizeJSON, encoding: .utf8)!, boundary: boundary)

        // Learning profile field (optional)
        if let profile = learningProfile {
            body.appendMultipart(name: "learning_profile", value: profile.text, boundary: boundary)
        }

        // App context field (optional)
        if let context = appContext {
            let contextJSON = try JSONEncoder().encode(context)
            body.appendMultipart(name: "app_context", value: String(data: contextJSON, encoding: .utf8)!, boundary: boundary)
        }

        // Screenshot file
        body.appendMultipartFile(name: "screenshot", filename: "screenshot.png", mimeType: "image/png", data: screenshotData, boundary: boundary)

        // Close boundary
        body.append("--\(boundary)--\r\n".data(using: .utf8)!)

        request.httpBody = body

        // Send request with 1 retry
        let data = try await sendWithRetry(request: request, maxRetries: 1)

        let plan = try JSONDecoder().decode(StepPlan.self, from: data)
        return plan
    }

    // MARK: - Private

    private func sendWithRetry(request: URLRequest, maxRetries: Int) async throws -> Data {
        var lastError: Error?

        for attempt in 0...maxRetries {
            do {
                let (data, response) = try await session.data(for: request)
                guard let httpResponse = response as? HTTPURLResponse,
                      (200...299).contains(httpResponse.statusCode) else {
                    throw NetworkError.badResponse
                }
                return data
            } catch {
                lastError = error
                if attempt < maxRetries {
                    // Brief backoff before retry
                    try? await Task.sleep(nanoseconds: 500_000_000)
                    print("[Network] Retrying... attempt \(attempt + 1)")
                }
            }
        }

        throw lastError ?? NetworkError.unknown
    }
}

// MARK: - Errors

enum NetworkError: Error, LocalizedError {
    case badResponse
    case timeout
    case unknown

    var errorDescription: String? {
        switch self {
        case .badResponse: return "Bad response from agent server"
        case .timeout: return "Agent server request timed out"
        case .unknown: return "Unknown network error"
        }
    }
}

// MARK: - Data Multipart Helpers

extension Data {
    mutating func appendMultipart(name: String, value: String, boundary: String) {
        append("--\(boundary)\r\n".data(using: .utf8)!)
        append("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n".data(using: .utf8)!)
        append("\(value)\r\n".data(using: .utf8)!)
    }

    mutating func appendMultipartFile(name: String, filename: String, mimeType: String, data: Data, boundary: String) {
        append("--\(boundary)\r\n".data(using: .utf8)!)
        append("Content-Disposition: form-data; name=\"\(name)\"; filename=\"\(filename)\"\r\n".data(using: .utf8)!)
        append("Content-Type: \(mimeType)\r\n\r\n".data(using: .utf8)!)
        append(data)
        append("\r\n".data(using: .utf8)!)
    }
}
