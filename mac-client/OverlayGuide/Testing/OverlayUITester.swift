import Foundation

struct OverlayUITestConfig {
    let enabled: Bool
    let goal: String
    let steps: Int
    let x: Double
    let y: Double
    let w: Double
    let h: Double
    let nextAfterSeconds: Double?

    static func fromCommandLine(_ args: [String]) -> OverlayUITestConfig {
        let enabled = args.contains("--ui-test")
        let goal = value(for: "--goal", in: args) ?? "UI test goal"
        let steps = max(1, Int(value(for: "--steps", in: args) ?? "") ?? 3)
        let x = clamp(Double(value(for: "--x", in: args) ?? "") ?? 0.3, min: 0.0, max: 1.0)
        let y = clamp(Double(value(for: "--y", in: args) ?? "") ?? 0.2, min: 0.0, max: 1.0)
        let w = clamp(Double(value(for: "--w", in: args) ?? "") ?? 0.2, min: 0.01, max: 1.0)
        let h = clamp(Double(value(for: "--h", in: args) ?? "") ?? 0.06, min: 0.01, max: 1.0)
        let nextAfter = Double(value(for: "--next-after", in: args) ?? "")

        return OverlayUITestConfig(
            enabled: enabled,
            goal: goal,
            steps: steps,
            x: x,
            y: y,
            w: w,
            h: h,
            nextAfterSeconds: nextAfter
        )
    }

    func makeInitialPlan() -> StepPlan {
        StepPlan(
            version: "v1",
            goal: goal,
            appContext: nil,
            imageSize: ImageSize(w: 1920, h: 1080),
            steps: makeSteps(startIndex: 1, count: steps, baseY: y)
        )
    }

    func makeNextPlan() -> StepPlan {
        let nextY = clamp(y + 0.12, min: 0.0, max: 0.9)
        return StepPlan(
            version: "v1",
            goal: goal,
            appContext: nil,
            imageSize: ImageSize(w: 1920, h: 1080),
            steps: makeSteps(startIndex: 1, count: 2, baseY: nextY)
        )
    }

    private func makeSteps(startIndex: Int, count: Int, baseY: Double) -> [Step] {
        (0..<count).map { idx in
            let shiftedY = clamp(baseY + (Double(idx) * 0.08), min: 0.0, max: 0.92)
            return Step(
                id: "s\(startIndex + idx)",
                instruction: "Test step \(startIndex + idx): interact with highlighted area.",
                targets: [
                    TargetRect(
                        x: x,
                        y: shiftedY,
                        w: w,
                        h: h,
                        confidence: 0.95,
                        label: "Test target \(startIndex + idx)"
                    )
                ],
                advance: Advance(type: .clickInTarget, notes: "UI tester generated step"),
                safety: nil
            )
        }
    }
}

enum OverlayUITester {
    static func runIfEnabled(
        stateMachine: GuidanceStateMachine,
        args: [String]
    ) {
        let config = OverlayUITestConfig.fromCommandLine(args)
        guard config.enabled else { return }

        let initialPlan = config.makeInitialPlan()
        stateMachine.applyInitialPlan(initialPlan)
        printPlan(initialPlan, label: "initial")

        guard let nextAfterSeconds = config.nextAfterSeconds else { return }
        guard nextAfterSeconds >= 0 else { return }

        Task { @MainActor in
            try? await Task.sleep(nanoseconds: UInt64(nextAfterSeconds * 1_000_000_000))
            let nextPlan = config.makeNextPlan()
            stateMachine.applyNextPlan(nextPlan)
            printPlan(nextPlan, label: "next")
        }
    }

    static func printUsage() {
        let usage = """
        Overlay UI Tester usage:
          swift run OverlayGuide --ui-test [options]

        Options:
          --goal <text>          Goal text shown in plan (default: "UI test goal")
          --steps <int>          Number of initial steps (default: 3)
          --x <0..1>             Target x normalized (default: 0.3)
          --y <0..1>             Target y normalized (default: 0.2)
          --w <0..1>             Target width normalized (default: 0.2)
          --h <0..1>             Target height normalized (default: 0.06)
          --next-after <sec>     Auto-apply synthetic next plan after delay
        """
        print(usage)
    }

    private static func printPlan(_ plan: StepPlan, label: String) {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        if let data = try? encoder.encode(plan),
           let text = String(data: data, encoding: .utf8) {
            print("[ui-test] \(label) plan:")
            print(text)
        } else {
            print("[ui-test] \(label) plan could not be encoded")
        }
    }
}

private func value(for flag: String, in args: [String]) -> String? {
    guard let idx = args.firstIndex(of: flag), idx + 1 < args.count else { return nil }
    return args[idx + 1]
}

private func clamp<T: Comparable>(_ value: T, min minValue: T, max maxValue: T) -> T {
    Swift.min(maxValue, Swift.max(minValue, value))
}
