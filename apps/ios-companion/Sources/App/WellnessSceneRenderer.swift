import SwiftUI

struct WellnessSceneRenderer: View {
    let scene: WellnessScene
    let maximumVisualizations: Int
    let busyProposalIDs: Set<UUID>
    let showsActions: Bool
    let onAction: (WellnessSceneAction) -> Void

    private let moss = HealthMesVisualStyle.capacity
    private let amber = HealthMesVisualStyle.proposal

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            sceneHeader

            ForEach(visibleModules) { module in
                moduleCard(module)
            }

            if showsActions, !scene.actions.isEmpty {
                actionBar
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("healthmes-generated-scene")
        .environment(\.timeZone, TimeZone(identifier: scene.timezone) ?? .current)
    }

    private var visibleModules: [WellnessSceneModule] {
        WellnessSceneDisplayPolicy.primaryInsightModules(
            in: scene,
            maximumInsights: maximumVisualizations
        )
    }

    private var sceneHeader: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack {
                Label(
                    sceneIsActionable ? "오늘의 판단" : "판단 보류",
                    systemImage: sceneIsActionable
                        ? "bolt.heart.fill"
                        : "exclamationmark.shield.fill"
                )
                    .font(.caption.weight(.bold))
                    .foregroundStyle(severityColor)
                Spacer()
                if !sceneIsActionable || scene.confidence.level == .low {
                    confidenceBadge
                }
            }
            Text(verbatim: sceneConclusion)
                .font(.system(.title3, design: .rounded).weight(.semibold))
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
            if !sceneIsActionable {
                Text(
                    verbatim:
                        "마지막 분석 \(WellnessDateFormat.abbreviatedDateTime(scene.generatedAt, timeZone: sceneTimeZone))"
                )
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            if let detail = scene.actions.first(where: { $0.kind == .openWebDetail }) {
                Button {
                    onAction(detail)
                } label: {
                    Label("근거 자세히 보기", systemImage: "safari")
                }
                .font(.caption.weight(.semibold))
                .accessibilityIdentifier(actionIdentifier(detail.kind))
            }
        }
        .padding(16)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .stroke(severityColor.opacity(0.22))
        }
    }

    private var sceneTimeZone: TimeZone {
        TimeZone(identifier: scene.timezone) ?? .autoupdatingCurrent
    }

    private var sceneIsActionable: Bool {
        scene.freshness == .current
            && scene.confidence.level != .insufficientData
    }

    private var sceneConclusion: String {
        guard sceneIsActionable else {
            return "최신 근거가 부족해 일정 변경 결론을 내리지 않습니다."
        }
        return scene.summary.isEmpty ? scene.title : scene.summary
    }

    private var confidenceBadge: some View {
        Text(verbatim: confidenceLabel)
            .font(.caption2.weight(.bold))
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(severityColor.opacity(0.12), in: Capsule())
            .foregroundStyle(severityColor)
            .accessibilityLabel(Text("데이터 신뢰도"))
            .accessibilityValue(Text(verbatim: scene.confidence.coverage))
    }

    private func moduleCard(_ module: WellnessSceneModule) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text(verbatim: module.title)
                    .font(.headline)
            }

            if module.kind == .proposalPreview {
                proposalPreview(module)
            } else if let visualization = module.visualization {
                visualizationView(visualization)
            } else if !module.items.isEmpty {
                ForEach(module.items) { item in
                    HStack(alignment: .top) {
                        Text(verbatim: item.label)
                            .foregroundStyle(.secondary)
                        Spacer()
                        VStack(alignment: .trailing, spacing: 2) {
                            Text(verbatim: item.value)
                                .fontWeight(.semibold)
                            if let detail = item.detail {
                                Text(verbatim: detail)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                    .font(.subheadline)
                }
            } else {
                Label("표시할 수 있는 데이터가 아직 충분하지 않습니다.", systemImage: "chart.bar.xaxis")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 20))
        .accessibilityElement(children: .contain)
        .accessibilityLabel(Text(verbatim: module.accessibilitySummary))
    }

    @ViewBuilder
    private func proposalPreview(_ module: WellnessSceneModule) -> some View {
        if let preview = WellnessProposalPreview(module: module) {
            VStack(alignment: .leading, spacing: 10) {
                Label("승인 전 미리보기", systemImage: "calendar.badge.clock")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(amber)
                Text(verbatim: preview.task)
                    .font(.headline)
                    .fixedSize(horizontal: false, vertical: true)
                Text(verbatim: preview.localizedWindow(timezone: scene.timezone))
                    .font(.subheadline.weight(.semibold).monospacedDigit())
                if let reason = preview.reason {
                    Label {
                        Text(verbatim: reason)
                            .fixedSize(horizontal: false, vertical: true)
                    } icon: {
                        Image(systemName: "waveform.path.ecg")
                    }
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                }
                Text("승인하기 전에는 캘린더가 바뀌지 않습니다.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        } else {
            Label("정확히 연결된 일정 제안이 없습니다.", systemImage: "calendar.badge.exclamationmark")
                .font(.footnote)
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
        return VStack(spacing: 10) {
            ForEach(entries.indices, id: \.self) { index in
                let seriesLabel = entries[index].0
                let point = entries[index].1
                VStack(alignment: .leading, spacing: 5) {
                    HStack {
                        Text(
                            verbatim: visualization.series.count > 1
                                ? "\(seriesLabel) · \(point.label)"
                                : point.label
                        )
                            .font(.subheadline.weight(.medium))
                        Spacer()
                        Text(
                            verbatim: point.annotation
                                ?? rangeText(point, unit: visualization.unit)
                        )
                            .font(.caption.weight(.bold).monospacedDigit())
                    }
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
                                    .fill(barColor(for: point.label))
                                    .frame(
                                        width: proxy.size.width * abs(position - zero)
                                    )
                                    .offset(x: proxy.size.width * min(position, zero))
                            }
                        }
                        .frame(height: 10)
                    } else {
                        Text("데이터 없음")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
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
        return VStack(spacing: 12) {
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
                        .font(.subheadline.weight(.medium))
                        Spacer()
                        Text(
                            verbatim: point.annotation
                                ?? rangeText(point, unit: visualization.unit)
                        )
                        .font(.caption.bold().monospacedDigit())
                    }
                    comparisonTrack(
                        value: point.value,
                        label: "현재",
                        color: moss,
                        lower: lower,
                        span: span
                    )
                    if point.secondaryValue != nil {
                        comparisonTrack(
                            value: point.secondaryValue,
                            label: "비교",
                            color: amber,
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
        label: LocalizedStringKey,
        color: Color,
        lower: Double,
        span: Double
    ) -> some View {
        HStack(spacing: 8) {
            Text(label)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(width: 30, alignment: .leading)
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
                .frame(height: 8)
            } else {
                Text("데이터 없음")
                    .font(.caption2)
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
        return VStack(spacing: 11) {
            ForEach(entries.indices, id: \.self) { index in
                let point = entries[index].1
                VStack(alignment: .leading, spacing: 5) {
                    HStack {
                        Text(verbatim: point.label)
                            .font(.subheadline.weight(.medium))
                        Spacer()
                        Text(
                            verbatim: point.annotation
                                ?? signedValueText(point.value, unit: visualization.unit)
                        )
                        .font(.caption.bold().monospacedDigit())
                    }
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
                                    .fill(value < 0 ? amber : moss)
                                    .frame(width: proxy.size.width * abs(position - zero))
                                    .offset(x: proxy.size.width * min(position, zero))
                            }
                        }
                        .frame(height: 10)
                    } else {
                        Text("데이터 없음")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
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
        return VStack(spacing: 12) {
            ForEach(entries.indices, id: \.self) { index in
                let point = entries[index].1
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text(verbatim: point.label)
                            .font(.subheadline.weight(.medium))
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
                                    .fill(moss.opacity(0.24))
                                    .frame(
                                        width: proxy.size.width * abs(current - baseline)
                                    )
                                    .offset(x: proxy.size.width * min(current, baseline))
                            }
                            if let baseline {
                                Rectangle()
                                    .fill(Color.secondary)
                                    .frame(width: 2, height: 18)
                                    .offset(x: proxy.size.width * baseline)
                            }
                            if let current {
                                Circle()
                                    .fill(amber)
                                    .frame(width: 12, height: 12)
                                    .offset(x: proxy.size.width * current - 6)
                            }
                        }
                    }
                    .frame(height: 18)
                    HStack {
                        Label("현재", systemImage: "circle.fill")
                            .foregroundStyle(amber)
                        Spacer()
                        Label("평소 기준", systemImage: "line.diagonal")
                            .foregroundStyle(.secondary)
                    }
                    .font(.caption2)
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
        return VStack(alignment: .leading, spacing: 10) {
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
                .frame(height: pointCount == 1 ? 72 : 112)
                .background(
                    LinearGradient(
                        colors: [moss.opacity(0.09), .clear],
                        startPoint: .top,
                        endPoint: .bottom
                    ),
                    in: RoundedRectangle(cornerRadius: 14)
                )
                HStack {
                    Text(verbatim: visualization.series.first?.points.first?.label ?? "")
                    Spacer()
                    Text(verbatim: visualization.series.first?.points.last?.label ?? "")
                }
                .font(.caption2.monospacedDigit())
                .foregroundStyle(.secondary)
                if pointCount == 1 {
                    Label("단일 관찰값입니다. 추세로 해석하지 않습니다.", systemImage: "circle.fill")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if dataIsFlat {
                    Label("표시된 구간의 값 변화가 없습니다.", systemImage: "equal")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if primaryMissingCount > 0 {
                    Label(
                        "주값 \(primaryMissingCount)개 시점은 데이터가 없어 선을 연결하지 않았습니다.",
                        systemImage: "circle.dashed"
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
                if secondaryMissingCount > 0 {
                    Label(
                        "비교값 \(secondaryMissingCount)개 시점은 데이터가 없어 점선으로 연결하지 않았습니다.",
                        systemImage: "circle.dashed"
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
            } else {
                Label("추이를 표시할 표본이 부족합니다.", systemImage: "chart.xyaxis.line")
                    .font(.footnote)
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
        let visibleEvents = Array(sortedEvents.prefix(8))
        return VStack(spacing: 0) {
            ForEach(Array(visibleEvents.enumerated()), id: \.element.id) { index, event in
                HStack(alignment: .top, spacing: 10) {
                    VStack(alignment: .leading, spacing: 1) {
                        if event.isAllDay {
                            Text("종일")
                                .font(.caption.weight(.bold))
                        } else {
                            Text(event.startsAt, format: .dateTime.hour().minute())
                                .font(.caption.weight(.bold).monospacedDigit())
                            Text(event.endsAt, format: .dateTime.hour().minute())
                                .font(.caption2.monospacedDigit())
                                .foregroundStyle(.secondary)
                        }
                        Text(event.startsAt, format: .dateTime.month(.abbreviated).day())
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    .frame(width: 52, alignment: .leading)

                    RoundedRectangle(cornerRadius: 2)
                        .fill(calendarColor(event))
                        .frame(width: 4, height: 38)

                    VStack(alignment: .leading, spacing: 3) {
                        Text(verbatim: event.title)
                            .font(.subheadline.weight(.semibold))
                            .lineLimit(2)
                        HStack(spacing: 5) {
                            Text(verbatim: event.calendarName)
                            if event.status == .proposed {
                                Text("· 제안")
                            }
                            if event.isHealthMesManaged {
                                Image(systemName: "bolt.heart.fill")
                            }
                            if event.isRecurring {
                                Image(systemName: "repeat")
                            }
                            if event.hasAttendees {
                                Image(systemName: "person.2")
                            }
                            if event.isLocked {
                                Image(systemName: "lock.fill")
                            }
                        }
                        .font(.caption)
                        .foregroundStyle(event.status == .proposed ? amber : .secondary)
                        HStack(spacing: 6) {
                            if let demand = event.energyDemand, !demand.isEmpty {
                                Label(
                                    energyDemandLabel(demand),
                                    systemImage: "bolt.fill"
                                )
                            }
                            if event.hasAttendees {
                                Text(event.organizerSelf ? "내가 주최" : "다른 주최자")
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
                    Spacer(minLength: 0)
                }
                .padding(.vertical, 8)
                .overlay(alignment: .bottom) {
                    if index < visibleEvents.count - 1 {
                        Divider()
                    }
                }
            }
            if sortedEvents.count > visibleEvents.count {
                Label(
                    "\(sortedEvents.count - visibleEvents.count)개 일정은 상세 화면에서 확인할 수 있습니다.",
                    systemImage: "ellipsis.circle"
                )
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(.top, 9)
            }
        }
    }

    private var actionBar: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let preview = scene.exactMutationPreview {
                VStack(alignment: .leading, spacing: 4) {
                    Text("승인하면")
                        .font(.caption.weight(.bold))
                        .foregroundStyle(.secondary)
                    Text(verbatim: preview.task)
                        .font(.headline)
                    Label {
                        Text(verbatim: preview.localizedWindow(timezone: scene.timezone))
                    } icon: {
                        Image(systemName: "calendar.badge.clock")
                    }
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(amber.opacity(0.08), in: RoundedRectangle(cornerRadius: 14))
                .accessibilityElement(children: .combine)
                .accessibilityLabel(
                    Text(
                        verbatim:
                            "승인할 정확한 일정 변경. \(preview.task). \(preview.localizedWindow(timezone: scene.timezone))"
                    )
                )
            }

            HStack(spacing: 10) {
                ForEach(visibleActions) { action in
                    if action.kind == .acceptProposal {
                        Button {
                            onAction(action)
                        } label: {
                            Text(verbatim: action.label)
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(moss)
                        .disabled(
                            action.proposalID.map(busyProposalIDs.contains) ?? false
                        )
                        .accessibilityIdentifier(actionIdentifier(action.kind))
                    } else {
                        Button {
                            onAction(action)
                        } label: {
                            Text(verbatim: action.label)
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.bordered)
                        .disabled(
                            action.proposalID.map(busyProposalIDs.contains) ?? false
                        )
                        .accessibilityIdentifier(actionIdentifier(action.kind))
                    }
                }
                if let detail = scene.actions.first(where: { $0.kind == .openWebDetail }) {
                    Button {
                        onAction(detail)
                    } label: {
                        Image(systemName: "info.circle")
                    }
                    .buttonStyle(.bordered)
                    .accessibilityLabel(Text(verbatim: detail.label))
                    .accessibilityIdentifier(actionIdentifier(detail.kind))
                }
            }
        }
    }

    private func actionIdentifier(_ kind: WellnessActionKind) -> String {
        switch kind {
        case .acceptProposal: return "healthmes-scene-action-accept"
        case .declineProposal: return "healthmes-scene-action-decline"
        case .modifyProposal: return "healthmes-scene-action-modify"
        case .createTask: return "healthmes-scene-action-create-task"
        case .createGoal: return "healthmes-scene-action-create-goal"
        case .openWebDetail: return "healthmes-scene-action-open-detail"
        case .refresh: return "healthmes-scene-action-refresh"
        case .switchLens: return "healthmes-scene-action-switch-lens"
        }
    }

    private var visibleActions: [WellnessSceneAction] {
        scene.actions.filter { action in
            switch action.kind {
            case .acceptProposal, .declineProposal:
                return scene.allowsProposalActions
            case .refresh, .switchLens:
                return true
            case .modifyProposal, .createTask, .createGoal, .openWebDetail:
                return false
            }
        }
    }

    private var confidenceLabel: String {
        switch scene.confidence.level {
        case .high: return "신뢰 높음"
        case .medium: return "신뢰 보통"
        case .low: return "신뢰 낮음"
        case .insufficientData: return "데이터 부족"
        }
    }

    private var severityColor: Color {
        switch scene.severity {
        case .action, .caution: return amber
        case .supportive: return moss
        case .neutral: return .secondary
        }
    }

    private func valueText(_ value: Double?, unit: String?) -> String {
        guard let value else { return "데이터 없음" }
        let rounded = Int(value.rounded())
        return unit == "percent" ? "\(rounded)%" : "\(rounded)"
    }

    private func signedValueText(_ value: Double?, unit: String?) -> String {
        guard let value else { return "데이터 없음" }
        let prefix = value > 0 ? "+" : ""
        return "\(prefix)\(valueText(value, unit: unit))"
    }

    private func rangeText(_ point: WellnessPoint, unit: String?) -> String {
        let primary = valueText(point.value, unit: unit)
        guard let secondary = point.secondaryValue else { return primary }
        return "\(primary) · \(valueText(secondary, unit: unit))"
    }

    private func seriesColor(_ index: Int) -> Color {
        [moss, amber, .blue, .teal][index % 4]
    }

    private func lineLegend(_ visualization: WellnessVisualization) -> some View {
        HStack(spacing: 12) {
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
                Label("비교값", systemImage: "line.diagonal")
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
        let seriesSummaries = visualization.series.map { series in
            var channels = [
                lineChannelAccessibilitySummary(
                    name: "주값",
                    points: series.points,
                    value: \.value,
                    unit: visualization.unit
                )
            ]
            if series.points.contains(where: { $0.secondaryValue != nil }) {
                channels.append(
                    lineChannelAccessibilitySummary(
                        name: "비교값",
                        points: series.points,
                        value: \.secondaryValue,
                        unit: visualization.unit
                    )
                )
            }
            return "\(series.label): \(channels.joined(separator: ", "))"
        }
        return seriesSummaries.joined(separator: ". ")
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
            return "\(name) 알려진 값 없음, 결측 \(missing)개"
        }
        let minimum = known.min { $0.1 < $1.1 }!
        let maximum = known.max { $0.1 < $1.1 }!
        let trend: String
        if known.count == 1 {
            trend = "\(first.0) 단일 값 \(valueText(first.1, unit: unit))"
        } else if first.1 == last.1, known.allSatisfy({ $0.1 == first.1 }) {
            trend = "변화 없음, \(valueText(first.1, unit: unit))"
        } else {
            trend =
                "\(first.0) \(valueText(first.1, unit: unit))에서 "
                + "\(last.0) \(valueText(last.1, unit: unit)), "
                + "최저 \(minimum.0) \(valueText(minimum.1, unit: unit)), "
                + "최고 \(maximum.0) \(valueText(maximum.1, unit: unit))"
        }
        return missing > 0 ? "\(name) \(trend), 결측 \(missing)개" : "\(name) \(trend)"
    }

    private func barColor(for label: String) -> Color {
        label.contains("부하") ? amber : moss
    }

    private func providerColor(_ provider: String) -> Color {
        switch provider.lowercased() {
        case "google": return Color(red: 0.20, green: 0.47, blue: 0.93)
        case "caldav", "icloud", "apple": return Color(red: 0.18, green: 0.66, blue: 0.39)
        case "healthmes": return amber
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
        case "high": return "높은 에너지"
        case "med", "medium": return "보통 에너지"
        case "low": return "낮은 에너지"
        default: return demand
        }
    }
}
