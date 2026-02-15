// Targeting/CropStitcher.swift
// Owner: Eng 2 (Capture + Targeting)
//
// Utilities for the zoom-refinement step of the SoM pipeline:
//   1. Compute a crop rectangle around a marker center
//   2. Crop the original screenshot to that rectangle
//   3. Stitch a crop-local bounding box back to full-image coordinates

import AppKit
import Foundation

// MARK: - CropRect (client-side)

struct CropRectNorm: Codable, Equatable {
    /// Top-left x of the crop in full-image normalized coords
    let cx: Double
    /// Top-left y of the crop in full-image normalized coords
    let cy: Double
    /// Width of the crop in full-image normalized coords
    let cw: Double
    /// Height of the crop in full-image normalized coords
    let ch: Double
}

// MARK: - CropStitcher

class CropStitcher {

    /// Default normalized crop size (square).
    /// 0.18 means the crop covers 18% of the image width and height.
    let defaultCropSize: Double

    init(cropSize: Double = 0.18) {
        self.defaultCropSize = cropSize
    }

    // MARK: - Crop Rect Computation

    /// Compute a crop rectangle centered on a marker, clamped to image bounds.
    ///
    /// - Parameters:
    ///   - markerCx: Marker center x (normalized 0..1)
    ///   - markerCy: Marker center y (normalized 0..1)
    ///   - cropSize: Normalized size of the crop (default: `defaultCropSize`)
    /// - Returns: CropRectNorm with top-left corner and dimensions
    func computeCropRect(
        markerCx: Double,
        markerCy: Double,
        cropSize: Double? = nil
    ) -> CropRectNorm {
        let size = cropSize ?? defaultCropSize
        let half = size / 2.0

        // Start with center-based rect
        var cx = markerCx - half
        var cy = markerCy - half
        var cw = size
        var ch = size

        // Clamp to [0, 1]
        if cx < 0.0 {
            cx = 0.0
        }
        if cy < 0.0 {
            cy = 0.0
        }
        if cx + cw > 1.0 {
            cw = 1.0 - cx
        }
        if cy + ch > 1.0 {
            ch = 1.0 - cy
        }

        return CropRectNorm(cx: cx, cy: cy, cw: cw, ch: ch)
    }

    // MARK: - Image Cropping

    /// Crop the original screenshot using a normalized crop rect.
    ///
    /// - Parameters:
    ///   - screenshot: Original NSImage
    ///   - cropRect: Normalized crop rectangle
    ///   - imageSize: Pixel dimensions of the screenshot
    /// - Returns: PNG Data of the cropped region, or nil on failure
    func cropImage(
        screenshot: NSImage,
        cropRect: CropRectNorm,
        imageSize: ImageSize
    ) -> Data? {
        let imgW = CGFloat(imageSize.w)
        let imgH = CGFloat(imageSize.h)

        // Convert normalized crop to pixel coordinates
        let pixelX = CGFloat(cropRect.cx) * imgW
        let pixelY = CGFloat(cropRect.cy) * imgH
        let pixelW = CGFloat(cropRect.cw) * imgW
        let pixelH = CGFloat(cropRect.ch) * imgH

        // NSImage's draw(from:) uses bottom-left origin, so flip y
        let flippedY = imgH - pixelY - pixelH

        let sourceRect = NSRect(
            x: pixelX,
            y: flippedY,
            width: pixelW,
            height: pixelH
        )

        // Create a new image for the crop
        let cropSize = NSSize(width: pixelW, height: pixelH)
        let croppedImage = NSImage(size: cropSize)

        croppedImage.lockFocus()
        screenshot.draw(
            in: NSRect(origin: .zero, size: cropSize),
            from: sourceRect,
            operation: .copy,
            fraction: 1.0
        )
        croppedImage.unlockFocus()

        // Encode as PNG
        guard let tiffData = croppedImage.tiffRepresentation,
              let bitmap = NSBitmapImageRep(data: tiffData),
              let pngData = bitmap.representation(using: .png, properties: [:]) else {
            return nil
        }

        return pngData
    }

    // MARK: - Stitch Back

    /// Convert a bounding box in crop-normalized coordinates to full-image normalized coordinates.
    ///
    /// The math:
    ///   x' = cx + x * cw
    ///   y' = cy + y * ch
    ///   w' = w  * cw
    ///   h' = h  * ch
    ///
    /// - Parameters:
    ///   - cropBbox: Bounding box in crop-normalized coords (from /refine)
    ///   - cropRect: The crop rectangle in full-image normalized coords
    /// - Returns: TargetRect in full-image normalized coords
    func stitchBack(
        cropBbox: TargetRect,
        cropRect: CropRectNorm
    ) -> TargetRect {
        let x = cropRect.cx + cropBbox.x * cropRect.cw
        let y = cropRect.cy + cropBbox.y * cropRect.ch
        let w = cropBbox.w * cropRect.cw
        let h = cropBbox.h * cropRect.ch

        // Clamp to [0, 1]
        let clampedX = max(0.0, min(x, 1.0))
        let clampedY = max(0.0, min(y, 1.0))
        let clampedW = max(0.001, min(w, 1.0 - clampedX))
        let clampedH = max(0.001, min(h, 1.0 - clampedY))

        return TargetRect(
            x: clampedX,
            y: clampedY,
            w: clampedW,
            h: clampedH,
            confidence: cropBbox.confidence,
            label: cropBbox.label
        )
    }
}
