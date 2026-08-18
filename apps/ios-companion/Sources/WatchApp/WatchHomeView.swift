import SwiftUI

/// Series 10 42 mm home: the pending decision is the product. Glance content
/// appears only when no Yes/No action is waiting.
struct WatchHomeView: View {
    @StateObject private var decisionInbox = WatchDecisionInbox.shared
    @StateObject private var model = WatchDecisionRemoteModel()

    var body: some View {
        WatchDecisionRemoteView(model: model)
            .padding(.horizontal, 2)
            .task { await model.refresh() }
            .onReceive(
                NotificationCenter.default.publisher(for: .healthmesPairingChanged)
            ) { _ in
                model.pairingDidChange()
                Task { await model.refresh() }
            }
        .sheet(item: $decisionInbox.detail) { detail in
            WatchDecisionDetailView(detail: detail)
        }
    }
}
