import Foundation

struct BBoxStitcher {
    static func stitch(cropRect: CropRectNorm, cropBBox: TargetRect) -> TargetRect? {
        guard cropBBox.type == .bboxNorm,
              let x = cropBBox.x,
              let y = cropBBox.y,
              let w = cropBBox.w,
              let h = cropBBox.h else {
            return nil
        }

        let stitchedX = clamp01(cropRect.cx + x * cropRect.cw)
        let stitchedY = clamp01(cropRect.cy + y * cropRect.ch)
        let stitchedW = min(max(w * cropRect.cw, 0.0001), 1.0)
        let stitchedH = min(max(h * cropRect.ch, 0.0001), 1.0)

        let safeW = min(stitchedW, 1.0 - stitchedX)
        let safeH = min(stitchedH, 1.0 - stitchedY)

        return TargetRect(
            type: .bboxNorm,
            markerId: nil,
            x: stitchedX,
            y: stitchedY,
            w: safeW,
            h: safeH,
            confidence: cropBBox.confidence,
            label: cropBBox.label
        )
    }

    private static func clamp01(_ value: Double) -> Double {
        min(max(value, 0.0), 1.0)
    }
}
