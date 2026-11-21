import Foundation
import ImageIO
import Vision

struct Line: Encodable {
    let text: String
    let x: Double
    let y: Double
}

func recognize(_ image: CGImage, within region: CGRect) throws -> [Line] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["en-US", "zh-Hans"]
    request.minimumTextHeight = 0.007
    try VNImageRequestHandler(cgImage: image).perform([request])
    return (request.results ?? []).compactMap { observation in
        guard let candidate = observation.topCandidates(1).first, candidate.confidence > 0.4 else {
            return nil
        }
        return Line(
            text: candidate.string,
            x: region.minX + observation.boundingBox.midX * region.width,
            y: region.minY + observation.boundingBox.midY * region.height
        )
    }
}

let frames = try CommandLine.arguments.dropFirst().map { pathname -> [Line] in
    let url = URL(fileURLWithPath: pathname)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw CocoaError(.fileReadCorruptFile)
    }
    let frame = try recognize(image, within: CGRect(x: 0, y: 0, width: 1, height: 1))
    let lowerCenter = CGRect(x: 0.2, y: 0, width: 0.6, height: 0.2)
    let crop = CGRect(
        x: lowerCenter.minX * Double(image.width), y: (1 - lowerCenter.maxY) * Double(image.height),
        width: lowerCenter.width * Double(image.width), height: lowerCenter.height * Double(image.height)
    )
    if let hud = image.cropping(to: crop.integral) {
        return frame + (try recognize(hud, within: lowerCenter))
    }
    return frame
}
FileHandle.standardOutput.write(try JSONEncoder().encode(frames))
