import SwiftUI
import UIKit

enum HealthMesVisualStyle {
    static let capacity = Color(red: 0.02, green: 0.47, blue: 0.42)
    static let capacityDeep = dynamicColor(
        light: UIColor(red: 0.02, green: 0.29, blue: 0.27, alpha: 1),
        dark: UIColor(red: 0.44, green: 0.91, blue: 0.79, alpha: 1)
    )
    static let calendar = Color(red: 0.16, green: 0.42, blue: 0.78)
    static let proposal = Color(red: 0.83, green: 0.48, blue: 0.12)
    static let recovery = Color(red: 0.35, green: 0.57, blue: 0.38)
    static let graphite = Color(red: 0.10, green: 0.13, blue: 0.13)
    static let line = dynamicColor(
        light: UIColor.black.withAlphaComponent(0.09),
        dark: UIColor.white.withAlphaComponent(0.12)
    )

    static var canvas: LinearGradient {
        LinearGradient(
            colors: [
                dynamicColor(
                    light: UIColor(red: 0.965, green: 0.965, blue: 0.945, alpha: 1),
                    dark: UIColor(red: 0.045, green: 0.075, blue: 0.07, alpha: 1)
                ),
                dynamicColor(
                    light: UIColor(red: 0.925, green: 0.955, blue: 0.945, alpha: 1),
                    dark: UIColor(red: 0.055, green: 0.105, blue: 0.095, alpha: 1)
                ),
                Color(uiColor: .systemGroupedBackground),
            ],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
    }

    static var drawer: LinearGradient {
        LinearGradient(
            colors: [
                Color(red: 0.055, green: 0.12, blue: 0.115),
                Color(red: 0.075, green: 0.18, blue: 0.165),
            ],
            startPoint: .top,
            endPoint: .bottom
        )
    }

    static var surfaceFill: Color {
        dynamicColor(
            light: UIColor.white.withAlphaComponent(0.76),
            dark: UIColor.secondarySystemGroupedBackground.withAlphaComponent(0.78)
        )
    }

    static func capacityColor(_ score: Int?) -> Color {
        switch score {
        case .some(70...): return capacity
        case .some(45..<70): return Color(red: 0.58, green: 0.57, blue: 0.20)
        case .some: return proposal
        case .none: return .secondary.opacity(0.3)
        }
    }

    private static func dynamicColor(light: UIColor, dark: UIColor) -> Color {
        Color(
            uiColor: UIColor { traits in
                traits.userInterfaceStyle == .dark ? dark : light
            }
        )
    }
}

struct HealthMesSurfaceModifier: ViewModifier {
    var radius: CGFloat = 20

    func body(content: Content) -> some View {
        content
            .background(
                HealthMesVisualStyle.surfaceFill,
                in: RoundedRectangle(cornerRadius: radius, style: .continuous)
            )
            .background(
                .regularMaterial,
                in: RoundedRectangle(cornerRadius: radius, style: .continuous)
            )
            .overlay {
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .stroke(HealthMesVisualStyle.line)
            }
            .shadow(color: HealthMesVisualStyle.capacityDeep.opacity(0.055), radius: 18, y: 8)
    }
}

extension View {
    func healthMesSurface(radius: CGFloat = 20) -> some View {
        modifier(HealthMesSurfaceModifier(radius: radius))
    }
}
