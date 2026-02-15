// Targeting/MarkerGenerator.swift
// Owner: Eng 2 (Capture + Targeting)
//
// Generates a uniform grid of numbered markers on a screenshot.
// Returns:
//   - The marked-up screenshot as PNG Data (for sending to the agent)
//   - The markers array as JSON Data (for the markers_json form field)
//   - The original screenshot is preserved separately for crop/refine.

import AppKit
import Foundation

// MARK: - SoMMarker (client-side representation)

struct SoMMarker: Codable, Equatable {
    let id: Int
    let cx: Double  // normalized center x [0,1]
    let cy: Double  // normalized center y [0,1]
    let radius: Double  // normalized radius
}

// MARK: - MarkerGenerationResult

struct MarkerGenerationResult {
    let markedImageData: Data       // PNG with markers drawn on top
    let markersJSON: Data           // JSON array of SoMMarker
    let markers: [SoMMarker]        // parsed marker structs
}

// MARK: - MarkerGenerator

class MarkerGenerator {

    /// Grid dimensions — tune these for density vs readability.
    /// 16x10 = 160 markers balances coverage with readability and latency.
    let columns: Int
    let rows: Int

    /// Marker visual radius in pixels (for drawing on the image).
    let markerPixelRadius: CGFloat = 14.0

    /// Font size for marker IDs.
    let markerFontSize: CGFloat = 11.0

    init(columns: Int = 16, rows: Int = 10) {
        self.columns = columns
        self.rows = rows
    }

    /// Generate markers and draw them onto a copy of the screenshot.
    ///
    /// - Parameters:
    ///   - screenshotImage: The original NSImage (not modified)
    ///   - imageSize: Pixel dimensions of the screenshot
    /// - Returns: MarkerGenerationResult with marked image + markers JSON
    func generateMarkers(
        screenshotImage: NSImage,
        imageSize: ImageSize
    ) -> MarkerGenerationResult? {
        let imgW = CGFloat(imageSize.w)
        let imgH = CGFloat(imageSize.h)

        // Generate marker positions (normalized)
        var markers: [SoMMarker] = []
        let normalizedRadius = Double(markerPixelRadius) / Double(max(imgW, imgH))

        var markerId = 0
        for row in 0..<rows {
            for col in 0..<columns {
                // Center each marker in its grid cell
                let cx = (Double(col) + 0.5) / Double(columns)
                let cy = (Double(row) + 0.5) / Double(rows)
                markers.append(SoMMarker(
                    id: markerId,
                    cx: cx,
                    cy: cy,
                    radius: normalizedRadius
                ))
                markerId += 1
            }
        }

        // Draw markers onto a copy of the screenshot
        guard let markedImage = drawMarkers(
            on: screenshotImage,
            markers: markers,
            imageWidth: imgW,
            imageHeight: imgH
        ) else {
            return nil
        }

        // Encode marked image as PNG
        guard let tiffData = markedImage.tiffRepresentation,
              let bitmap = NSBitmapImageRep(data: tiffData),
              let pngData = bitmap.representation(using: .png, properties: [:]) else {
            return nil
        }

        // Encode markers as JSON
        guard let jsonData = try? JSONEncoder().encode(markers) else {
            return nil
        }

        return MarkerGenerationResult(
            markedImageData: pngData,
            markersJSON: jsonData,
            markers: markers
        )
    }

    // MARK: - Private Drawing

    private func drawMarkers(
        on original: NSImage,
        markers: [SoMMarker],
        imageWidth: CGFloat,
        imageHeight: CGFloat
    ) -> NSImage? {
        let size = NSSize(width: imageWidth, height: imageHeight)
        let image = NSImage(size: size)

        image.lockFocus()

        // Draw the original screenshot
        original.draw(
            in: NSRect(origin: .zero, size: size),
            from: NSRect(origin: .zero, size: original.size),
            operation: .copy,
            fraction: 1.0
        )

        // Set up drawing attributes for marker circles
        let circleColor = NSColor(red: 1.0, green: 0.2, blue: 0.2, alpha: 0.85)
        let fillColor = NSColor(red: 1.0, green: 1.0, blue: 1.0, alpha: 0.9)
        let textColor = NSColor.black
        let font = NSFont.systemFont(ofSize: markerFontSize, weight: .bold)

        let textAttributes: [NSAttributedString.Key: Any] = [
            .font: font,
            .foregroundColor: textColor,
        ]

        for marker in markers {
            // Convert normalized coords to pixel coords
            // Note: NSImage drawing uses bottom-left origin, so flip y
            let px = CGFloat(marker.cx) * imageWidth
            let py = imageHeight - CGFloat(marker.cy) * imageHeight  // flip for AppKit

            let r = markerPixelRadius

            // Draw filled circle background
            let circleRect = NSRect(
                x: px - r,
                y: py - r,
                width: r * 2,
                height: r * 2
            )

            // White fill
            fillColor.setFill()
            let fillPath = NSBezierPath(ovalIn: circleRect)
            fillPath.fill()

            // Red border
            circleColor.setStroke()
            let strokePath = NSBezierPath(ovalIn: circleRect)
            strokePath.lineWidth = 1.5
            strokePath.stroke()

            // Draw marker ID text centered in the circle
            let idString = "\(marker.id)" as NSString
            let textSize = idString.size(withAttributes: textAttributes)
            let textPoint = NSPoint(
                x: px - textSize.width / 2,
                y: py - textSize.height / 2
            )
            idString.draw(at: textPoint, withAttributes: textAttributes)
        }

        image.unlockFocus()
        return image
    }
}
