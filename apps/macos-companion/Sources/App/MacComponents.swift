import SwiftUI

enum MacHealthMesStyle {
    static let canvas = Color(red: 0.955, green: 0.955, blue: 0.935)
    static let graphite = Color(red: 0.10, green: 0.13, blue: 0.13)
    static let moss = Color(red: 0.02, green: 0.45, blue: 0.39)
    static let mossDeep = Color(red: 0.02, green: 0.27, blue: 0.25)
    static let calendar = Color(red: 0.16, green: 0.42, blue: 0.78)
    static let amber = Color(red: 0.59, green: 0.31, blue: 0.04)
    static let line = Color.black.opacity(0.09)
    static let sidebarTop = Color(red: 0.045, green: 0.105, blue: 0.10)
    static let sidebarBottom = Color(red: 0.07, green: 0.17, blue: 0.15)
}

struct MacPageHeader: View {
    let eyebrow: LocalizedStringKey
    let title: LocalizedStringKey
    let subtitle: LocalizedStringKey

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(eyebrow)
                .font(.caption.weight(.semibold))
                .textCase(.uppercase)
                .tracking(1.4)
                .foregroundStyle(MacHealthMesStyle.moss)
            Text(title)
                .font(.system(size: 30, weight: .semibold, design: .rounded))
                .foregroundStyle(MacHealthMesStyle.graphite)
            Text(subtitle)
                .font(.body)
                .foregroundStyle(.secondary)
                .lineLimit(2)
        }
    }
}

struct MacSurfaceCard<Content: View>: View {
    let label: LocalizedStringKey
    let systemImage: String
    @ViewBuilder let content: Content

    init(
        _ label: LocalizedStringKey,
        systemImage: String,
        @ViewBuilder content: () -> Content
    ) {
        self.label = label
        self.systemImage = systemImage
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label(label, systemImage: systemImage)
                .font(.caption.weight(.semibold))
                .textCase(.uppercase)
                .tracking(1.1)
                .foregroundStyle(.secondary)
            content
            Spacer(minLength: 0)
        }
        .padding(20)
        .frame(maxWidth: .infinity, minHeight: 188, alignment: .topLeading)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .stroke(MacHealthMesStyle.line)
        }
        .shadow(color: MacHealthMesStyle.mossDeep.opacity(0.055), radius: 18, y: 8)
    }
}

struct MacSectionHeader: View {
    let title: LocalizedStringKey
    let count: Int?

    init(_ title: LocalizedStringKey, count: Int? = nil) {
        self.title = title
        self.count = count
    }

    var body: some View {
        HStack {
            Text(title)
                .font(.headline)
            if let count {
                Text(verbatim: "\(count)")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background(.quaternary, in: Capsule())
            }
            Spacer()
        }
    }
}

struct MacPrivacyPill: View {
    let isPaired: Bool
    let isStale: Bool

    var body: some View {
        Label {
            Text(isPaired ? (isStale ? "Private · cached" : "Private · own instance") : "Not connected")
        } icon: {
            Image(systemName: isPaired ? "lock.shield.fill" : "link.badge.plus")
        }
        .font(.caption.weight(.medium))
        .foregroundStyle(isPaired ? MacHealthMesStyle.moss : .secondary)
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(.thinMaterial, in: Capsule())
        .accessibilityElement(children: .combine)
    }
}

struct MacEmptyState: View {
    let systemImage: String
    let title: LocalizedStringKey
    let message: LocalizedStringKey

    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: systemImage)
                .font(.system(size: 30))
                .foregroundStyle(MacHealthMesStyle.moss)
            Text(title)
                .font(.headline)
            Text(message)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 380)
        }
        .frame(maxWidth: .infinity, minHeight: 220)
        .padding(24)
    }
}

struct MacWebDetailLink: View {
    let url: URL?

    var body: some View {
        if let url {
            Link(destination: url) {
                Label("View details on web", systemImage: "arrow.up.right.square")
            }
            .buttonStyle(.bordered)
        }
    }
}

struct MacMetadataRow: View {
    let label: LocalizedStringKey
    let value: String

    var body: some View {
        LabeledContent {
            Text(verbatim: value)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.trailing)
        } label: {
            Text(label)
        }
        .font(.callout)
    }
}

extension Date {
    var healthMesShortTime: String {
        formatted(date: .omitted, time: .shortened)
    }

    var healthMesShortDateTime: String {
        formatted(date: .abbreviated, time: .shortened)
    }

    func healthMesShortTime(in timeZone: TimeZone) -> String {
        let formatter = DateFormatter()
        formatter.locale = .autoupdatingCurrent
        formatter.timeZone = timeZone
        formatter.dateStyle = .none
        formatter.timeStyle = .short
        return formatter.string(from: self)
    }

    func healthMesShortDateTime(in timeZone: TimeZone) -> String {
        WellnessDateFormat.abbreviatedDateTime(self, timeZone: timeZone)
    }
}
