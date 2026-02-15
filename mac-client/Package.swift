// swift-tools-version: 5.9
// The swift-tools-version declares the minimum version of Swift Package Manager required to build this package.

import PackageDescription

let package = Package(
    name: "OverlayGuide",
    platforms: [
        .macOS(.v13)
    ],
    dependencies: [
        .package(url: "https://github.com/stasel/WebRTC.git", .upToNextMajor(from: "125.0.0")),
    ],
    targets: [
        .executableTarget(
            name: "OverlayGuide",
            dependencies: [
                .product(name: "WebRTC", package: "WebRTC"),
            ],
            path: "OverlayGuide"
        ),
    ]
)
