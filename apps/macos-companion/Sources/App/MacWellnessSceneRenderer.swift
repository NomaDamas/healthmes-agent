import SwiftUI

struct MacWellnessSceneRenderer: View {
    let scene: WellnessScene
    let resolvingProposalID: UUID?
    let onAction: (WellnessSceneAction) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            header

            LazyVGrid(
                columns: [
                    GridItem(.flexible(), spacing: 14),
                    GridItem(.flexible(), spacing: 14),
                ],
                alignment: .leading,
                spacing: 14
            ) {
                ForEach(scene.modules) { module in
                    moduleCard(module)
                        .gridCellColumns(
                            module.visualization?.kind == .calendarCanvas
                                || module.visualization?.kind == .scheduleComparison
                                ? 2
                                : 1
                        )
                }
            }

            actionBar
        }
        .environment(\.timeZone, TimeZone(identifier: scene.timezone) ?? .current)
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 20) {
            VStack(alignment: .leading, spacing: 7) {
                Text(verbatim: scene.title)
                    .font(.system(size: 30, weight: .bold, design: .rounded))
                Text(verbatim: scene.summary)
                    .font(.title3)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 5) {
                Text(verbatim: confidenceLabel)
                    .font(.caption.weight(.bold))
                    .foregroundStyle(severityColor)
                Text(verbatim: scene.confidence.coverage)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if !scene.confidence.limitations.isEmpty {
                    DisclosureGroup("Data limits") {
                        VStack(alignment: .trailing, spacing: 4) {
                            ForEach(scene.confidence.limitations, id: \.self) { limitation in
                                Text(verbatim: limitation)
                            }
                        }
                    }
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .background(severityColor.opacity(0.09), in: RoundedRectangle(cornerRadius: 12))
        }
        .padding(20)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 20))
    }

    private func moduleCard(_ module: WellnessSceneModule) -> some View {
        VStack(alignment: .leading, spacing: 13) {
            Text(verbatim: module.title)
                .font(.title3.weight(.semibold))
            if module.kind != .proposalPreview {
                Text(verbatim: module.summary)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if module.kind == .proposalPreview {
                proposalPreview(module)
            } else if let visualization = module.visualization {
                visualizationView(visualization)
            } else if module.items.isEmpty {
                Label("Not enough data to render this insight.", systemImage: "chart.bar.xaxis")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                itemGrid(module.items)
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, minHeight: 180, alignment: .topLeading)
        .background(Color.white.opacity(0.54), in: RoundedRectangle(cornerRadius: 18))
        .overlay {
            RoundedRectangle(cornerRadius: 18)
                .stroke(MacHealthMesStyle.line)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(Text(verbatim: module.accessibilitySummary))
    }

    @ViewBuilder
    private func proposalPreview(_ module: WellnessSceneModule) -> some View {
        if let preview = WellnessProposalPreview(module: module) {
            VStack(alignment: .leading, spacing: 10) {
                Label("Approval preview", systemImage: "calendar.badge.clock")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(MacHealthMesStyle.amber)
                Text(verbatim: preview.task)
                    .font(.title3.weight(.semibold))
                Text(verbatim: preview.localizedWindow(timezone: scene.timezone))
                    .font(.callout.weight(.semibold).monospacedDigit())
                if let reason = preview.reason {
                    Label {
                        Text(verbatim: reason)
                            .fixedSize(horizontal: false, vertical: true)
                    } icon: {
                        Image(systemName: "waveform.path.ecg")
                    }
                    .font(.callout)
                    .foregroundStyle(.secondary)
                }
                Text("Your calendar stays unchanged until you approve.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        } else {
            Label(
                "No exact schedule proposal is available.",
                systemImage: "calendar.badge.exclamationmark"
            )
            .font(.callout)
            .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func visualizationView(_ visualization: WellnessVisualization) -> some View {
        switch visualization.kind {
        case .calendarCanvas, .scheduleComparison:
            calendarCanvas(visualization)
        case .energyCurve, .timeSeries, .eventAlignedTrend, .decisionOutcome:
            lineChart(visualization)
        case .capacityBar, .goalTrajectory:
            barChart(visualization)
        case .comparisonBar:
            comparisonChart(visualization)
        case .factorContribution:
            factorContributionChart(visualization)
        case .baselineBand:
            baselineChart(visualization)
        }
    }

    private func barChart(_ visualization: WellnessVisualization) -> some View {
        let entries = visualization.series.flatMap { series in
            series.points.map { (series.label, $0) }
        }
        let values = entries.flatMap { [$0.1.value, $0.1.secondaryValue] }.compactMap { $0 }
        let lower = visualization.minimum ?? min(values.min() ?? 0, 0)
        let upper = visualization.maximum ?? max(values.max() ?? 1, 1)
        let span = max(upper - lower, 1)
        return VStack(spacing: 11) {
            ForEach(entries.indices, id: \.self) { index in
                let seriesLabel = entries[index].0
                let point = entries[index].1
                HStack(spacing: 12) {
                    Text(
                        verbatim: visualization.series.count > 1
                            ? "\(seriesLabel) · \(point.label)"
                            : point.label
                    )
                        .font(.callout.weight(.medium))
                        .frame(width: 130, alignment: .leading)
                        .lineLimit(2)
                    if let value = point.value {
                        GeometryReader { proxy in
                            let zero = CGFloat(min(max((0 - lower) / span, 0), 1))
                            let position = CGFloat(min(max((value - lower) / span, 0), 1))
                            ZStack(alignment: .leading) {
                                Capsule().fill(Color.primary.opacity(0.08))
                                Rectangle()
                                    .fill(Color.primary.opacity(0.18))
                                    .frame(width: 1)
                                    .offset(x: proxy.size.width * zero)
                                Capsule()
                                    .fill(
                                        point.label.contains("부하")
                                            ? MacHealthMesStyle.amber
                                            : MacHealthMesStyle.moss
                                    )
                                    .frame(
                                        width: proxy.size.width * abs(position - zero)
                                    )
                                    .offset(x: proxy.size.width * min(position, zero))
                            }
                        }
                        .frame(height: 11)
                    } else {
                        Text("No data")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    Text(
                        verbatim: point.annotation
                            ?? rangeText(point, unit: visualization.unit)
                    )
                        .font(.caption.bold().monospacedDigit())
                        .frame(width: 78, alignment: .trailing)
                }
            }
        }
    }

    private func comparisonChart(_ visualization: WellnessVisualization) -> some View {
        let entries = visualization.series.flatMap { series in
            series.points.map { (series.label, $0) }
        }
        let values = entries.flatMap { [$0.1.value, $0.1.secondaryValue] }.compactMap { $0 }
        let lower = visualization.minimum ?? min(values.min() ?? 0, 0)
        let upper = visualization.maximum ?? max(values.max() ?? 1, 1)
        let span = max(upper - lower, 1)
        return VStack(spacing: 13) {
            ForEach(entries.indices, id: \.self) { index in
                let seriesLabel = entries[index].0
                let point = entries[index].1
                VStack(alignment: .leading, spacing: 7) {
                    HStack {
                        Text(
                            verbatim: visualization.series.count > 1
                                ? "\(seriesLabel) · \(point.label)"
                                : point.label
                        )
                        .font(.callout.weight(.medium))
                        Spacer()
                        Text(
                            verbatim: point.annotation
                                ?? rangeText(point, unit: visualization.unit)
                        )
                        .font(.caption.bold().monospacedDigit())
                    }
                    comparisonTrack(
                        value: point.value,
                        label: "Current",
                        color: MacHealthMesStyle.moss,
                        lower: lower,
                        span: span
                    )
                    if point.secondaryValue != nil {
                        comparisonTrack(
                            value: point.secondaryValue,
                            label: "Compare",
                            color: MacHealthMesStyle.amber,
                            lower: lower,
                            span: span
                        )
                    }
                }
            }
        }
    }

    private func comparisonTrack(
        value: Double?,
        label: String,
        color: Color,
        lower: Double,
        span: Double
    ) -> some View {
        HStack(spacing: 9) {
            Text(verbatim: label)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(width: 52, alignment: .leading)
            if let value {
                GeometryReader { proxy in
                    let width = CGFloat(min(max((value - lower) / span, 0), 1))
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color.primary.opacity(0.07))
                        Capsule()
                            .fill(color)
                            .frame(width: proxy.size.width * width)
                    }
                }
                .frame(height: 9)
            } else {
                Text("No data")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func factorContributionChart(
        _ visualization: WellnessVisualization
    ) -> some View {
        let entries = visualization.series.flatMap { series in
            series.points.map { (series.label, $0) }
        }
        let values = entries.compactMap { $0.1.value }
        let lower = visualization.minimum ?? min(values.min() ?? -1, 0)
        let upper = visualization.maximum ?? max(values.max() ?? 1, 0)
        let span = max(upper - lower, 1)
        return VStack(spacing: 12) {
            ForEach(entries.indices, id: \.self) { index in
                let point = entries[index].1
                HStack(spacing: 12) {
                    Text(verbatim: point.label)
                        .font(.callout.weight(.medium))
                        .frame(width: 130, alignment: .leading)
                        .lineLimit(2)
                    if let value = point.value {
                        GeometryReader { proxy in
                            let zero = CGFloat(min(max((0 - lower) / span, 0), 1))
                            let position = CGFloat(min(max((value - lower) / span, 0), 1))
                            ZStack(alignment: .leading) {
                                Capsule().fill(Color.primary.opacity(0.07))
                                Rectangle()
                                    .fill(Color.primary.opacity(0.28))
                                    .frame(width: 1)
                                    .offset(x: proxy.size.width * zero)
                                Capsule()
                                    .fill(
                                        value < 0
                                            ? MacHealthMesStyle.amber
                                            : MacHealthMesStyle.moss
                                    )
                                    .frame(width: proxy.size.width * abs(position - zero))
                                    .offset(x: proxy.size.width * min(position, zero))
                            }
                        }
                        .frame(height: 11)
                    } else {
                        Text("No data")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    Text(
                        verbatim: point.annotation
                            ?? signedValueText(point.value, unit: visualization.unit)
                    )
                    .font(.caption.bold().monospacedDigit())
                    .frame(width: 78, alignment: .trailing)
                }
            }
        }
    }

    private func baselineChart(_ visualization: WellnessVisualization) -> some View {
        let entries = visualization.series.flatMap { series in
            series.points.map { (series.label, $0) }
        }
        let values = entries.flatMap { [$0.1.value, $0.1.secondaryValue] }.compactMap { $0 }
        let lower = visualization.minimum ?? min(values.min() ?? 0, 0)
        let upper = visualization.maximum ?? max(values.max() ?? 1, 1)
        let span = max(upper - lower, 1)
        return VStack(spacing: 13) {
            ForEach(entries.indices, id: \.self) { index in
                let point = entries[index].1
                VStack(alignment: .leading, spacing: 7) {
                    HStack {
                        Text(verbatim: point.label)
                            .font(.callout.weight(.medium))
                        Spacer()
                        Text(
                            verbatim: point.annotation
                                ?? rangeText(point, unit: visualization.unit)
                        )
                        .font(.caption.bold().monospacedDigit())
                    }
                    GeometryReader { proxy in
                        let current = point.value.map {
                            CGFloat(min(max(($0 - lower) / span, 0), 1))
                        }
                        let baseline = point.secondaryValue.map {
                            CGFloat(min(max(($0 - lower) / span, 0), 1))
                        }
                        ZStack(alignment: .leading) {
                            Capsule().fill(Color.primary.opacity(0.07))
                            if let current, let baseline {
                                Capsule()
                                    .fill(MacHealthMesStyle.moss.opacity(0.24))
                                    .frame(
                                        width: proxy.size.width * abs(current - baseline)
                                    )
                                    .offset(x: proxy.size.width * min(current, baseline))
                            }
                            if let baseline {
                                Rectangle()
                                    .fill(Color.secondary)
                                    .frame(width: 2, height: 20)
                                    .offset(x: proxy.size.width * baseline)
                            }
                            if let current {
                                Circle()
                                    .fill(MacHealthMesStyle.amber)
                                    .frame(width: 13, height: 13)
                                    .offset(x: proxy.size.width * current - 6.5)
                            }
                        }
                    }
                    .frame(height: 20)
                    HStack {
                        Label("Current", systemImage: "circle.fill")
                            .foregroundStyle(MacHealthMesStyle.amber)
                        Spacer()
                        Label("Personal baseline", systemImage: "line.diagonal")
                            .foregroundStyle(.secondary)
                    }
                    .font(.caption)
                }
            }
        }
    }

    private func lineChart(_ visualization: WellnessVisualization) -> some View {
        let allValues = visualization.series
            .flatMap(\.points)
            .flatMap { [$0.value, $0.secondaryValue] }
            .compactMap { $0 }
        let dataLower = allValues.min() ?? 0
        let dataUpper = allValues.max() ?? 100
        let dataIsFlat = !allValues.isEmpty && dataLower == dataUpper
        let flatPadding = max(abs(dataLower) * 0.08, 1)
        let lower =
            visualization.minimum
            ?? (dataIsFlat ? dataLower - flatPadding : dataLower)
        let upper =
            visualization.maximum
            ?? (dataIsFlat ? dataUpper + flatPadding : dataUpper)
        let pointCount = visualization.series.map(\.points.count).max() ?? 0
        let primaryMissingCount = visualization.series
            .flatMap(\.points)
            .filter { $0.value == nil }
            .count
        let secondaryMissingCount = visualization.series.reduce(into: 0) { count, series in
            guard series.points.contains(where: { $0.secondaryValue != nil }) else { return }
            count += series.points.filter { $0.secondaryValue == nil }.count
        }
        return VStack(alignment: .leading, spacing: 9) {
            if !allValues.isEmpty {
                lineLegend(visualization)
                GeometryReader { proxy in
                    Canvas { context, size in
                        for (seriesIndex, series) in visualization.series.enumerated() {
                            let color = seriesColor(seriesIndex)
                            for path in lineSegments(
                                points: series.points,
                                values: series.points.map(\.value),
                                size: size,
                                lower: lower,
                                upper: upper
                            ) {
                                context.stroke(
                                    path,
                                    with: .color(color),
                                    style: StrokeStyle(
                                        lineWidth: 3,
                                        lineCap: .round,
                                        lineJoin: .round
                                    )
                                )
                            }
                            drawPoints(
                                context: &context,
                                points: series.points,
                                values: series.points.map(\.value),
                                size: size,
                                lower: lower,
                                upper: upper,
                                color: color,
                                radius: 3
                            )
                            if series.points.contains(where: { $0.secondaryValue != nil }) {
                                for path in lineSegments(
                                    points: series.points,
                                    values: series.points.map(\.secondaryValue),
                                    size: size,
                                    lower: lower,
                                    upper: upper
                                ) {
                                    context.stroke(
                                        path,
                                        with: .color(color.opacity(0.55)),
                                        style: StrokeStyle(
                                            lineWidth: 2,
                                            lineCap: .round,
                                            lineJoin: .round,
                                            dash: [5, 4]
                                        )
                                    )
                                }
                                drawPoints(
                                    context: &context,
                                    points: series.points,
                                    values: series.points.map(\.secondaryValue),
                                    size: size,
                                    lower: lower,
                                    upper: upper,
                                    color: color.opacity(0.55),
                                    radius: 2.5
                                )
                            }
                        }
                    }
                }
                .frame(height: pointCount == 1 ? 82 : 124)
                .background(MacHealthMesStyle.moss.opacity(0.06), in: RoundedRectangle(cornerRadius: 12))
                HStack {
                    Text(verbatim: visualization.series.first?.points.first?.label ?? "")
                    Spacer()
                    Text(verbatim: visualization.series.first?.points.last?.label ?? "")
                }
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
                if pointCount == 1 {
                    Label(
                        "Single observation. HealthMes does not label it as a trend.",
                        systemImage: "circle.fill"
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                } else if dataIsFlat {
                    Label("No change across the displayed samples.", systemImage: "equal")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if primaryMissingCount > 0 {
                    Label(
                        "\(primaryMissingCount) primary samples are shown as gaps.",
                        systemImage: "circle.dashed"
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
                if secondaryMissingCount > 0 {
                    Label(
                        "\(secondaryMissingCount) comparison samples are shown as dashed gaps.",
                        systemImage: "circle.dashed"
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
            } else {
                Text("Not enough samples for a trend.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Text(verbatim: lineAccessibilitySummary(visualization)))
    }

    private func calendarCanvas(_ visualization: WellnessVisualization) -> some View {
        let sortedEvents = visualization.events.sorted {
            if $0.startsAt == $1.startsAt {
                return $0.id < $1.id
            }
            return $0.startsAt < $1.startsAt
        }
        let visibleEvents = Array(sortedEvents.prefix(10))
        return LazyVGrid(
            columns: [
                GridItem(.flexible(), spacing: 10),
                GridItem(.flexible(), spacing: 10),
            ],
            spacing: 9
        ) {
            ForEach(visibleEvents) { event in
                HStack(alignment: .top, spacing: 10) {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(calendarColor(event))
                        .frame(width: 4, height: 42)
                    VStack(alignment: .leading, spacing: 3) {
                        HStack {
                            if event.isAllDay {
                                Text(event.startsAt, format: .dateTime.weekday(.abbreviated))
                                    .font(.caption.bold())
                                Text("ALL-DAY")
                                    .font(.caption2.bold())
                                    .foregroundStyle(.secondary)
                            } else {
                                Text(
                                    event.startsAt,
                                    format: .dateTime.weekday(.abbreviated).hour().minute()
                                )
                                .font(.caption.bold().monospacedDigit())
                                Text("–")
                                    .foregroundStyle(.secondary)
                                Text(event.endsAt, format: .dateTime.hour().minute())
                                    .font(.caption.monospacedDigit())
                            }
                            Spacer()
                            if event.status == .proposed {
                                Text("PROPOSED")
                                    .font(.caption2.bold())
                                    .foregroundStyle(MacHealthMesStyle.amber)
                            }
                        }
                        Text(verbatim: event.title)
                            .font(.callout.weight(.semibold))
                            .lineLimit(2)
                        Text(verbatim: event.calendarName)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        HStack(spacing: 5) {
                            if event.isRecurring { Image(systemName: "repeat") }
                            if event.hasAttendees { Image(systemName: "person.2") }
                            if event.isLocked { Image(systemName: "lock.fill") }
                            if let demand = event.energyDemand, !demand.isEmpty {
                                Label(
                                    energyDemandLabel(demand),
                                    systemImage: "bolt.fill"
                                )
                            }
                        }
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        HStack(spacing: 6) {
                            if event.hasAttendees {
                                Text(event.organizerSelf ? "You organize" : "External organizer")
                            }
                            if let providerStatus = event.providerStatus,
                                !providerStatus.isEmpty
                            {
                                Text(verbatim: providerStatus.replacingOccurrences(
                                    of: "_",
                                    with: " "
                                ))
                            }
                        }
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    }
                }
                .padding(11)
                .background(
                    event.status == .proposed
                        ? MacHealthMesStyle.amber.opacity(0.08)
                        : Color.primary.opacity(0.035),
                    in: RoundedRectangle(cornerRadius: 12)
                )
            }
            if sortedEvents.count > visibleEvents.count {
                Label(
                    "\(sortedEvents.count - visibleEvents.count) more events are available in detail.",
                    systemImage: "ellipsis.circle"
                )
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(11)
            }
        }
    }

    private func itemGrid(_ items: [WellnessSceneItem]) -> some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 170), spacing: 10)], spacing: 10) {
            ForEach(items) { item in
                VStack(alignment: .leading, spacing: 4) {
                    Text(verbatim: item.label)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                    Text(verbatim: item.value)
                        .font(.body.weight(.medium))
                    if let detail = item.detail {
                        Text(verbatim: detail)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(11)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 11))
            }
        }
    }

    @ViewBuilder
    private var actionBar: some View {
        if !visibleActions.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                if let preview = scene.exactMutationPreview {
                    VStack(alignment: .leading, spacing: 5) {
                        Text("Approval will apply this exact block")
                            .font(.caption.weight(.bold))
                            .foregroundStyle(.secondary)
                        Text(verbatim: preview.task)
                            .font(.headline)
                        Label {
                            Text(verbatim: preview.localizedWindow(timezone: scene.timezone))
                        } icon: {
                            Image(systemName: "calendar.badge.clock")
                        }
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    }
                    .padding(12)
                    .background(
                        MacHealthMesStyle.amber.opacity(0.08),
                        in: RoundedRectangle(cornerRadius: 12)
                    )
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel(
                        Text(
                            verbatim:
                                "Exact schedule action for approval. \(preview.task). \(preview.localizedWindow(timezone: scene.timezone))"
                        )
                    )
                }

                HStack(spacing: 10) {
                    ForEach(visibleActions) { action in
                        if action.kind == .openWebDetail {
                            Button {
                                onAction(action)
                            } label: {
                                Label(action.label, systemImage: "arrow.up.right.square")
                            }
                            .buttonStyle(.bordered)
                        } else if action.kind == .acceptProposal {
                            Button(action.label) { onAction(action) }
                                .buttonStyle(.borderedProminent)
                                .tint(MacHealthMesStyle.moss)
                                .disabled(isActionBusy(action))
                        } else {
                            Button(action.label) { onAction(action) }
                                .buttonStyle(.bordered)
                                .disabled(isActionBusy(action))
                        }
                    }
                    Spacer()
                }
                .controlSize(.large)
            }
        }
    }

    private var visibleActions: [WellnessSceneAction] {
        scene.actions.filter { action in
            switch action.kind {
            case .acceptProposal, .declineProposal:
                return scene.allowsProposalActions
            case .openWebDetail, .refresh, .switchLens:
                return true
            case .modifyProposal, .createTask, .createGoal:
                return false
            }
        }
    }

    private func isActionBusy(_ action: WellnessSceneAction) -> Bool {
        guard let proposalID = action.proposalID else { return false }
        return proposalID == resolvingProposalID
    }

    private var confidenceLabel: String {
        switch scene.confidence.level {
        case .high: return "High confidence"
        case .medium: return "Medium confidence"
        case .low: return "Low confidence"
        case .insufficientData: return "Insufficient data"
        }
    }

    private var severityColor: Color {
        switch scene.severity {
        case .neutral: return .secondary
        case .supportive: return MacHealthMesStyle.moss
        case .caution, .action: return MacHealthMesStyle.amber
        }
    }

    private func valueText(_ value: Double?, unit: String?) -> String {
        guard let value else { return "—" }
        let rounded = Int(value.rounded())
        return unit == "percent" ? "\(rounded)%" : "\(rounded)"
    }

    private func signedValueText(_ value: Double?, unit: String?) -> String {
        guard let value else { return "—" }
        let prefix = value > 0 ? "+" : ""
        return "\(prefix)\(valueText(value, unit: unit))"
    }

    private func rangeText(_ point: WellnessPoint, unit: String?) -> String {
        let primary = valueText(point.value, unit: unit)
        guard let secondary = point.secondaryValue else { return primary }
        return "\(primary) · \(valueText(secondary, unit: unit))"
    }

    private func seriesColor(_ index: Int) -> Color {
        [
            MacHealthMesStyle.moss,
            MacHealthMesStyle.amber,
            .blue,
            .teal,
        ][index % 4]
    }

    private func lineLegend(_ visualization: WellnessVisualization) -> some View {
        HStack(spacing: 14) {
            ForEach(Array(visualization.series.enumerated()), id: \.element.id) {
                index,
                series in
                Label {
                    Text(verbatim: series.label)
                } icon: {
                    Circle()
                        .fill(seriesColor(index))
                        .frame(width: 7, height: 7)
                }
                .font(.caption)
            }
            if visualization.series.contains(where: {
                $0.points.contains(where: { $0.secondaryValue != nil })
            }) {
                Label("Comparison", systemImage: "line.diagonal")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func lineSegments(
        points: [WellnessPoint],
        values: [Double?],
        size: CGSize,
        lower: Double,
        upper: Double
    ) -> [Path] {
        var paths: [Path] = []
        var current: Path?
        let span = max(upper - lower, 1)
        for index in points.indices {
            guard let value = values[index] else {
                if let current {
                    paths.append(current)
                }
                current = nil
                continue
            }
            let x = size.width * CGFloat(index) / CGFloat(max(points.count - 1, 1))
            let ratio = (value - lower) / span
            let y = size.height * (1 - CGFloat(min(max(ratio, 0), 1)))
            let point = CGPoint(x: x, y: y)
            if current == nil {
                var path = Path()
                path.move(to: point)
                current = path
            } else {
                current?.addLine(to: point)
            }
        }
        if let current {
            paths.append(current)
        }
        return paths
    }

    private func drawPoints(
        context: inout GraphicsContext,
        points: [WellnessPoint],
        values: [Double?],
        size: CGSize,
        lower: Double,
        upper: Double,
        color: Color,
        radius: CGFloat
    ) {
        let span = max(upper - lower, 1)
        for index in points.indices {
            guard let value = values[index] else { continue }
            let x = size.width * CGFloat(index) / CGFloat(max(points.count - 1, 1))
            let ratio = (value - lower) / span
            let y = size.height * (1 - CGFloat(min(max(ratio, 0), 1)))
            let rect = CGRect(
                x: x - radius,
                y: y - radius,
                width: radius * 2,
                height: radius * 2
            )
            context.fill(Path(ellipseIn: rect), with: .color(color))
        }
    }

    private func lineAccessibilitySummary(
        _ visualization: WellnessVisualization
    ) -> String {
        visualization.series.map { series in
            var channels = [
                lineChannelAccessibilitySummary(
                    name: "primary",
                    points: series.points,
                    value: \.value,
                    unit: visualization.unit
                )
            ]
            if series.points.contains(where: { $0.secondaryValue != nil }) {
                channels.append(
                    lineChannelAccessibilitySummary(
                        name: "comparison",
                        points: series.points,
                        value: \.secondaryValue,
                        unit: visualization.unit
                    )
                )
            }
            return "\(series.label): \(channels.joined(separator: ", "))"
        }
        .joined(separator: ". ")
    }

    private func lineChannelAccessibilitySummary(
        name: String,
        points: [WellnessPoint],
        value: KeyPath<WellnessPoint, Double?>,
        unit: String?
    ) -> String {
        let known = points.compactMap { point -> (String, Double)? in
            guard let value = point[keyPath: value] else { return nil }
            return (point.label, value)
        }
        let missing = points.count - known.count
        guard let first = known.first, let last = known.last else {
            return "\(name) has no known values, \(missing) missing"
        }
        let minimum = known.min { $0.1 < $1.1 }!
        let maximum = known.max { $0.1 < $1.1 }!
        let trend: String
        if known.count == 1 {
            trend = "\(first.0) single value \(valueText(first.1, unit: unit))"
        } else if first.1 == last.1, known.allSatisfy({ $0.1 == first.1 }) {
            trend = "no change at \(valueText(first.1, unit: unit))"
        } else {
            trend =
                "\(first.0) \(valueText(first.1, unit: unit)) to "
                + "\(last.0) \(valueText(last.1, unit: unit)), "
                + "minimum \(minimum.0) \(valueText(minimum.1, unit: unit)), "
                + "maximum \(maximum.0) \(valueText(maximum.1, unit: unit))"
        }
        return missing > 0 ? "\(name) \(trend), \(missing) missing" : "\(name) \(trend)"
    }

    private func providerColor(_ provider: String) -> Color {
        switch provider.lowercased() {
        case "google": return Color(red: 0.20, green: 0.47, blue: 0.93)
        case "caldav", "icloud", "apple": return Color(red: 0.18, green: 0.66, blue: 0.39)
        case "healthmes": return MacHealthMesStyle.amber
        default: return .secondary
        }
    }

    private func calendarColor(_ event: WellnessCalendarEvent) -> Color {
        let hex = event.calendarColor.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
        guard hex.count == 6, let value = UInt64(hex, radix: 16) else {
            return providerColor(event.provider)
        }
        return Color(
            red: Double((value >> 16) & 0xFF) / 255,
            green: Double((value >> 8) & 0xFF) / 255,
            blue: Double(value & 0xFF) / 255
        )
    }

    private func providerName(_ provider: String) -> String {
        switch provider.lowercased() {
        case "google": return "Google Calendar"
        case "caldav", "icloud", "apple": return "Apple Calendar"
        case "healthmes": return "HealthMes"
        default: return provider
        }
    }

    private func energyDemandLabel(_ demand: String) -> String {
        switch demand.lowercased() {
        case "high": return "High energy"
        case "med", "medium": return "Medium energy"
        case "low": return "Low energy"
        default: return demand
        }
    }
}
