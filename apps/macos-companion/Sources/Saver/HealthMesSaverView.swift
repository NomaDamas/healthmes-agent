import AppKit
import ScreenSaver

/// Full-screen ambient briefing (issue #11): big energy score, 24h curve,
/// next block, gentle alert count — or the honest "not paired" / "no data"
/// state. Pure AppKit drawing (SwiftUI hosting inside legacyScreenSaver is
/// known-fragile).
///
/// PLACEHOLDER VISUALS (docs/design/WATCH-NOTIFICATIONS.ko.md): type scale,
/// colors and layout are engineering placeholders; the *information* comes
/// from the tested SaverBriefing model, including the privacy toggle
/// (numbers hidden = absent, see SaverBriefing docs).
///
/// Data path: the shared on-disk glance snapshot only (see SaverDataSource —
/// no networking, no Keychain in the saver process). The menu bar app /
/// widget keep that snapshot <= 5 min fresh; the "Updated N min ago" line
/// keeps the age honest.
@objc(HealthMesSaverView)
public final class HealthMesSaverView: ScreenSaverView {
    private let dataSource = SaverDataSource()
    private var briefing = SaverBriefing(state: .notPaired)
    private var lastReload: Date?
    private lazy var configureController = SaverConfigureController()

    /// Reload the cache file at most this often (the endpoint freshness
    /// floor is 300 s; 60 s keeps the "Updated…" line honest).
    static let reloadInterval: TimeInterval = 60

    public override init?(frame: NSRect, isPreview: Bool) {
        super.init(frame: frame, isPreview: isPreview)
        animationTimeInterval = 30
        reload()
    }

    public required init?(coder: NSCoder) {
        super.init(coder: coder)
        animationTimeInterval = 30
        reload()
    }

    public override var hasConfigureSheet: Bool { true }

    public override var configureSheet: NSWindow? {
        configureController.prepare()
        return configureController.window
    }

    public override func startAnimation() {
        super.startAnimation()
        reload()
    }

    public override func animateOneFrame() {
        if lastReload.map({ Date().timeIntervalSince($0) >= Self.reloadInterval }) ?? true {
            reload()
        }
        needsDisplay = true
    }

    private func reload() {
        lastReload = Date()
        let hideNumbers = SaverDefaultsStore()?.hideNumbers ?? false
        briefing = dataSource.briefing(hideNumbers: hideNumbers)
    }

    // MARK: - Drawing

    public override func draw(_ rect: NSRect) {
        NSColor.black.setFill()
        bounds.fill()

        switch briefing.state {
        case .notPaired:
            drawCenteredMessage(
                title: saverLocalized("saver.notPaired.title"),
                body: saverLocalized("saver.notPaired.body")
            )
        case .noData:
            drawCenteredMessage(
                title: saverLocalized("saver.noData.title"),
                body: saverLocalized("saver.noData.body")
            )
        case .briefing(let content):
            drawBriefing(content)
        }
    }

    private struct Line {
        let text: String
        let font: NSFont
        let color: NSColor
        let spacingAfter: CGFloat
    }

    private func drawBriefing(_ content: SaverBriefing.Content) {
        // Type scale relative to screen height so preview thumbnails and
        // full screens both work.
        let unit = max(bounds.height, 200) / 100

        var lines: [Line] = []

        lines.append(
            Line(
                text: "HealthMes",
                font: .systemFont(ofSize: 2.2 * unit, weight: .medium),
                color: .tertiaryLabelColor,
                spacingAfter: 2 * unit
            )
        )

        if let score = content.scoreText {
            lines.append(
                Line(
                    text: score,
                    font: .monospacedDigitSystemFont(ofSize: 16 * unit, weight: .bold),
                    color: .white,
                    spacingAfter: 0.5 * unit
                )
            )
            if let confidence = content.confidenceRaw {
                lines.append(
                    Line(
                        text: saverLocalized("saver.confidence.\(confidence)"),
                        font: .systemFont(ofSize: 2 * unit, weight: .regular),
                        color: .secondaryLabelColor,
                        spacingAfter: 2 * unit
                    )
                )
            }
        } else if content.numbersHidden {
            lines.append(
                Line(
                    text: saverLocalized("saver.privacy.hidden"),
                    font: .systemFont(ofSize: 3 * unit, weight: .medium),
                    color: .secondaryLabelColor,
                    spacingAfter: 2 * unit
                )
            )
        }

        // Curve placeholder slot: drawn separately below the text stack.

        if content.hasNextBlock {
            let title = content.nextBlockTitle ?? saverLocalized("saver.block.untitled")
            let time = content.nextBlockTimeText ?? ""
            lines.append(
                Line(
                    text: String(format: saverLocalized("saver.nextBlock.format"), time, title),
                    font: .systemFont(ofSize: 3 * unit, weight: .regular),
                    color: .labelColor.withAlphaComponent(0.85),
                    spacingAfter: 1.2 * unit
                )
            )
        }

        if let count = content.alertCount, count > 0 {
            lines.append(
                Line(
                    text: String(format: saverLocalized("saver.alerts.format"), count),
                    font: .systemFont(ofSize: 2.4 * unit, weight: .regular),
                    color: .systemOrange,
                    spacingAfter: 1.2 * unit
                )
            )
        }

        if let minutes = content.updatedMinutesAgo {
            let text =
                minutes < 60
                ? String(format: saverLocalized("saver.updated.minutes"), minutes)
                : String(format: saverLocalized("saver.updated.hours"), minutes / 60)
            lines.append(
                Line(
                    text: text,
                    font: .systemFont(ofSize: 1.8 * unit, weight: .regular),
                    color: .tertiaryLabelColor,
                    spacingAfter: 0
                )
            )
        }

        // Vertical layout: text stack centered, curve slot inserted after
        // the score group when present.
        let curveHeight: CGFloat = content.curve != nil ? 12 * unit : 0
        let curveSpacing: CGFloat = content.curve != nil ? 2 * unit : 0
        let textHeight = lines.reduce(CGFloat(0)) { $0 + $1.font.capHeight * 1.6 + $1.spacingAfter }
        let totalHeight = textHeight + curveHeight + curveSpacing
        var y = bounds.midY + totalHeight / 2

        // Score group first (title + score + confidence), then curve, then rest.
        let scoreGroupCount = min(lines.count, content.scoreText != nil ? 3 : 2)
        for (index, line) in lines.enumerated() {
            y = drawLine(line, topY: y)
            if index == scoreGroupCount - 1, let curve = content.curve {
                y -= curveSpacing
                let curveWidth = min(bounds.width * 0.5, 60 * unit)
                let curveRect = NSRect(
                    x: bounds.midX - curveWidth / 2,
                    y: y - curveHeight,
                    width: curveWidth,
                    height: curveHeight
                )
                drawCurve(curve, currentHour: content.currentHour, in: curveRect)
                y -= curveHeight + curveSpacing
            }
        }
    }

    /// Draws one centered line whose TOP is at `topY`; returns the next topY.
    @discardableResult
    private func drawLine(_ line: Line, topY: CGFloat) -> CGFloat {
        let attributes: [NSAttributedString.Key: Any] = [
            .font: line.font,
            .foregroundColor: line.color,
        ]
        let size = line.text.size(withAttributes: attributes)
        let origin = NSPoint(x: bounds.midX - size.width / 2, y: topY - size.height)
        line.text.draw(at: origin, withAttributes: attributes)
        return topY - size.height - line.spacingAfter
    }

    /// Same honest geometry as every other surface: CurveGeometry gaps/dots
    /// + current-hour marker, rendered with NSBezierPath.
    private func drawCurve(_ curve: [GlanceCurvePoint], currentHour: Int?, in rect: NSRect) {
        // Baseline.
        NSColor.tertiaryLabelColor.withAlphaComponent(0.4).setStroke()
        let baseline = NSBezierPath()
        baseline.move(to: NSPoint(x: rect.minX, y: rect.minY))
        baseline.line(to: NSPoint(x: rect.maxX, y: rect.minY))
        baseline.lineWidth = 1
        baseline.stroke()

        // Current-hour marker (AppKit y grows upward — flip vs SwiftUI).
        if let currentHour {
            let x = rect.minX + CGFloat(CurveGeometry.xPosition(forHour: currentHour)) * rect.width
            let marker = NSBezierPath()
            marker.move(to: NSPoint(x: x, y: rect.minY))
            marker.line(to: NSPoint(x: x, y: rect.maxY))
            marker.setLineDash([3, 3], count: 2, phase: 0)
            marker.lineWidth = 1
            NSColor.secondaryLabelColor.setStroke()
            marker.stroke()
        }

        NSColor.white.setStroke()
        for segment in CurveGeometry.segments(curve) {
            guard let first = segment.first else { continue }
            let path = NSBezierPath()
            path.lineWidth = 2
            path.lineCapStyle = .round
            path.lineJoinStyle = .round
            path.move(to: point(first, in: rect))
            for p in segment.dropFirst() {
                path.line(to: point(p, in: rect))
            }
            path.stroke()
        }

        NSColor.white.setFill()
        for isolated in CurveGeometry.isolatedPoints(curve) {
            let center = point(isolated, in: rect)
            let dot = NSRect(x: center.x - 2.5, y: center.y - 2.5, width: 5, height: 5)
            NSBezierPath(ovalIn: dot).fill()
        }
    }

    private func point(_ p: CurveGeometry.Point, in rect: NSRect) -> NSPoint {
        NSPoint(
            x: rect.minX + CGFloat(p.x) * rect.width,
            y: rect.minY + CGFloat(p.y) * rect.height
        )
    }

    private func drawCenteredMessage(title: String, body: String) {
        let unit = max(bounds.height, 200) / 100
        let titleAttributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 4 * unit, weight: .semibold),
            .foregroundColor: NSColor.white,
        ]
        let bodyAttributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 2.4 * unit, weight: .regular),
            .foregroundColor: NSColor.secondaryLabelColor,
        ]
        let titleSize = title.size(withAttributes: titleAttributes)
        let bodySize = body.size(withAttributes: bodyAttributes)
        let spacing = 1.5 * unit
        let totalHeight = titleSize.height + spacing + bodySize.height
        let titleOrigin = NSPoint(
            x: bounds.midX - titleSize.width / 2,
            y: bounds.midY + totalHeight / 2 - titleSize.height
        )
        title.draw(at: titleOrigin, withAttributes: titleAttributes)
        let bodyOrigin = NSPoint(
            x: bounds.midX - bodySize.width / 2,
            y: titleOrigin.y - spacing - bodySize.height
        )
        body.draw(at: bodyOrigin, withAttributes: bodyAttributes)
    }
}
