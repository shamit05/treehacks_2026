// Networking/AgentNetworkClient.swift
// Owner: Eng 3 (Agent Pipeline)
//
// HTTP client for communicating with the Python agent-server.
// Sends raw screenshots + goals, receives StepPlan JSON.
// Server handles all SoM marker generation and refinement.

import Foundation

class AgentNetworkClient {

    private let baseURL: URL
    private let session: URLSession
    // SoM two-pass pipeline can take 10-15s; use generous timeout.
    private let timeout: TimeInterval = 60.0

    init(baseURL: URL = URL(string: "http://localhost:8000")!) {
        self.baseURL = baseURL
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = timeout
        config.timeoutIntervalForResource = timeout
        self.session = URLSession(configuration: config)
    }

    /// POST /plan — send goal + raw screenshot, receive StepPlan with refined targets.
    /// Server handles SoM marker generation, model calls, and refinement.
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

        let requestId = UUID().uuidString
        request.setValue(requestId, forHTTPHeaderField: "X-Request-ID")
        print("[Network] POST /plan requestId=\(requestId)")

        let boundary = UUID().uuidString
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")

        var body = Data()

        body.appendMultipart(name: "goal", value: goal, boundary: boundary)

        let imageSizeJSON = try JSONEncoder().encode(imageSize)
        body.appendMultipart(name: "image_size", value: String(data: imageSizeJSON, encoding: .utf8)!, boundary: boundary)

        if let profile = learningProfile {
            body.appendMultipart(name: "learning_profile", value: profile.text, boundary: boundary)
        }

        if let context = appContext {
            let contextJSON = try JSONEncoder().encode(context)
            body.appendMultipart(name: "app_context", value: String(data: contextJSON, encoding: .utf8)!, boundary: boundary)
        }

        // Raw screenshot — server draws markers and handles refinement
        body.appendMultipartFile(name: "screenshot", filename: "screenshot.png", mimeType: "image/png", data: screenshotData, boundary: boundary)

        body.append("--\(boundary)--\r\n".data(using: .utf8)!)
        request.httpBody = body

        let data = try await sendWithRetry(request: request, maxRetries: 1)
        let plan = try JSONDecoder().decode(StepPlan.self, from: data)
        return plan
    }

    /// POST /next — send fresh screenshot + completed steps, receive updated StepPlan
    func requestNext(
        goal: String,
        screenshotData: Data,
        imageSize: ImageSize,
        completedSteps: [Step],
        totalSteps: Int,
        learningProfile: LearningProfile? = nil,
        appContext: AppContext? = nil
    ) async throws -> NextStepResponse {
        let url = baseURL.appendingPathComponent("next")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"

        let requestId = UUID().uuidString
        request.setValue(requestId, forHTTPHeaderField: "X-Request-ID")
        print("[Network] POST /next requestId=\(requestId)")

        let boundary = UUID().uuidString
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")

        var body = Data()

        body.appendMultipart(name: "goal", value: goal, boundary: boundary)

        let imageSizeJSON = try JSONEncoder().encode(imageSize)
        body.appendMultipart(name: "image_size", value: String(data: imageSizeJSON, encoding: .utf8)!, boundary: boundary)

        let completedStepsJSON = try JSONEncoder().encode(completedSteps)
        body.appendMultipart(name: "completed_steps", value: String(data: completedStepsJSON, encoding: .utf8)!, boundary: boundary)

        body.appendMultipart(name: "total_steps", value: String(totalSteps), boundary: boundary)

        if let profile = learningProfile {
            body.appendMultipart(name: "learning_profile", value: profile.text, boundary: boundary)
        }

        if let context = appContext {
            let contextJSON = try JSONEncoder().encode(context)
            body.appendMultipart(name: "app_context", value: String(data: contextJSON, encoding: .utf8)!, boundary: boundary)
        }

        body.appendMultipartFile(name: "screenshot", filename: "screenshot.png", mimeType: "image/png", data: screenshotData, boundary: boundary)

        body.append("--\(boundary)--\r\n".data(using: .utf8)!)
        request.httpBody = body

        let data = try await sendWithRetry(request: request, maxRetries: 1)
        let response = try JSONDecoder().decode(NextStepResponse.self, from: data)
        return response
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
