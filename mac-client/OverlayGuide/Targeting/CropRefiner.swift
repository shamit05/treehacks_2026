import AppKit
import Foundation

struct CropRectNorm: Codable, Equatable {
    let cx: Double
    let cy: Double
    let cw: Double
    let ch: Double
}

enum CropRefinerError: LocalizedError {
    case invalidImage
    case cropFailed
    case encodeFailed

    var errorDescription: String? {
        switch self {
        case .invalidImage:
            return "Failed to decode screenshot image for crop."
        case .cropFailed:
            return "Failed to crop screenshot around marker."
        case .encodeFailed:
            return "Failed to encode crop image as PNG."
        }
    }
}

struct CropRefiner {
    static let defaultCropSize: Double = 0.18
    static let defaultMarkerBBoxSize: Double = 0.06

    static func makeCropRect(around marker: SOMMarker, cropSize: Double = defaultCropSize) -> CropRectNorm {
        let clampedSize = min(max(cropSize, 0.05), 0.7)
        let half = clampedSize / 2.0
        var cx = marker.cx - half
        var cy = marker.cy - half
        cx = min(max(cx, 0.0), 1.0 - clampedSize)
        cy = min(max(cy, 0.0), 1.0 - clampedSize)
        return CropRectNorm(cx: cx, cy: cy, cw: clampedSize, ch: clampedSize)
    }

    static func cropImage(pngData: Data, cropRect: CropRectNorm) throws -> Data {
        guard let source = NSImage(data: pngData),
              let tiff = source.tiffRepresentation,
              let bitmap = NSBitmapImageRep(data: tiff),
              let cgImage = bitmap.cgImage else {
            throw CropRefinerError.invalidImage
        }

        let imageWidth = CGFloat(cgImage.width)
        let imageHeight = CGFloat(cgImage.height)

        let px = Int((cropRect.cx * Double(imageWidth)).rounded(.down))
        let pyTop = Int((cropRect.cy * Double(imageHeight)).rounded(.down))
        let pw = max(1, Int((cropRect.cw * Double(imageWidth)).rounded(.toNearestOrAwayFromZero)))
        let ph = max(1, Int((cropRect.ch * Double(imageHeight)).rounded(.toNearestOrAwayFromZero)))

        // CGImage crop rectangle uses bottom-left origin.
        let pyBottom = Int(imageHeight) - pyTop - ph
        let cropPxRect = CGRect(
            x: max(0, px),
            y: max(0, pyBottom),
            width: min(pw, Int(imageWidth) - max(0, px)),
            height: min(ph, Int(imageHeight) - max(0, pyBottom))
        )

        guard cropPxRect.width > 0,
              cropPxRect.height > 0,
              let cropped = cgImage.cropping(to: cropPxRect) else {
            throw CropRefinerError.cropFailed
        }

        let result = NSImage(cgImage: cropped, size: NSSize(width: cropPxRect.width, height: cropPxRect.height))
        guard let outTiff = result.tiffRepresentation,
              let outBitmap = NSBitmapImageRep(data: outTiff),
              let outPng = outBitmap.representation(using: .png, properties: [:]) else {
            throw CropRefinerError.encodeFailed
        }

        return outPng
    }

    static func defaultBBox(around marker: SOMMarker, size: Double = defaultMarkerBBoxSize, confidence: Double? = nil, label: String? = nil) -> TargetRect {
        let clampedSize = min(max(size, 0.02), 0.25)
        let half = clampedSize / 2.0
        let x = min(max(marker.cx - half, 0.0), 1.0 - clampedSize)
        let y = min(max(marker.cy - half, 0.0), 1.0 - clampedSize)
        return TargetRect(
            type: .bboxNorm,
            markerId: nil,
            x: x,
            y: y,
            w: clampedSize,
            h: clampedSize,
            confidence: confidence,
            label: label
        )
    }
}
