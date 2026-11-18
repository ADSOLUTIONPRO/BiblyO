// SPDX-License-Identifier: EUPL-1.2
// Relecture indépendante des codes d'un PDF (lot L8), sous macOS : chaque page est rendue à DPI points par pouce par
// PDFKit, puis Vision cherche tous les codes-barres, par bandes qui se recouvrent. Aucun code de biblyO n'est utilisé.
//   swift scripts/verifier_codes_vision.swift <fichier.pdf> [dpi, 600 par défaut]
import AppKit
import Foundation
import PDFKit
import Vision

let path = CommandLine.arguments[1]
let dpi = CGFloat(Double(CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "600")!)
guard let document = PDFDocument(url: URL(fileURLWithPath: path)) else { print("PDF illisible"); exit(1) }
for index in 0..<document.pageCount {
    let page = document.page(at: index)!
    let box = page.bounds(for: .mediaBox)
    let scale = dpi / 72
    let width = Int(box.width * scale), height = Int(box.height * scale)
    let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                            space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue)!
    context.setFillColor(gray: 1, alpha: 1)
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    context.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: context)
    let image = context.makeImage()!
    var found = Set<String>()
    let band = height / 6
    var top = 0
    while top < height {
        if let crop = image.cropping(to: CGRect(x: 0, y: top, width: width, height: min(band * 3 / 2, height - top))) {
            let request = VNDetectBarcodesRequest()
            try? VNImageRequestHandler(cgImage: crop, options: [:]).perform([request])
            for result in request.results ?? [] {
                let kind = result.symbology.rawValue.replacingOccurrences(of: "VNBarcodeSymbology", with: "")
                found.insert("\(kind):\(result.payloadStringValue ?? "?")")
            }
        }
        top += band
    }
    print("page \(index + 1) (\(box.width) × \(box.height) pt) : \(found.count) codes : \(found.sorted().joined(separator: " "))")
}
