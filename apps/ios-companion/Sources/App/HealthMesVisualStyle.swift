import SwiftUI
import UIKit

enum HealthMesVisualStyle {
    // Sunrise: warm brand expression, blue measured data, neutral surfaces.
    static let brand = Color(red: 0.89, green: 0.29, blue: 0.15)
    static let brandDeep = dynamicColor(
        light: UIColor(red: 0.72, green: 0.20, blue: 0.10, alpha: 1),
        dark: UIColor(red: 1.0, green: 0.53, blue: 0.38, alpha: 1)
    )
    static let capacity = Color(red: 0.24, green: 0.44, blue: 0.84)
    static let capacityDeep = dynamicColor(
        light: UIColor(red: 0.16, green: 0.31, blue: 0.67, alpha: 1),
        dark: UIColor(red: 0.47, green: 0.66, blue: 1.0, alpha: 1)
    )
    static let calendar = Color(red: 0.20, green: 0.40, blue: 0.82)
    static let proposal = Color(red: 0.78, green: 0.36, blue: 0.08)
    static let recovery = Color(red: 0.39, green: 0.56, blue: 0.86)
    static let graphite = Color(red: 0.12, green: 0.14, blue: 0.17)
    static let line = dynamicColor(
        light: UIColor(red: 0.31, green: 0.27, blue: 0.22, alpha: 0.12),
        dark: UIColor.white.withAlphaComponent(0.12)
    )

    static var canvas: LinearGradient {
        LinearGradient(
            colors: [
                dynamicColor(
                    light: UIColor(red: 0.98, green: 0.965, blue: 0.94, alpha: 1),
                    dark: UIColor(red: 0.075, green: 0.085, blue: 0.11, alpha: 1)
                ),
                dynamicColor(
                    light: UIColor(red: 0.965, green: 0.94, blue: 0.91, alpha: 1),
                    dark: UIColor(red: 0.105, green: 0.115, blue: 0.15, alpha: 1)
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
                Color(red: 0.11, green: 0.125, blue: 0.16),
                Color(red: 0.16, green: 0.18, blue: 0.23),
            ],
            startPoint: .top,
            endPoint: .bottom
        )
    }

    static var surfaceFill: Color {
        dynamicColor(
            light: UIColor(red: 1.0, green: 0.992, blue: 0.976, alpha: 0.94),
            dark: UIColor(red: 0.13, green: 0.145, blue: 0.18, alpha: 0.92)
        )
    }

    static func capacityColor(_ score: Int?) -> Color {
        switch score {
        case .some(70...): return capacity
        case .some(45..<70): return Color(red: 0.40, green: 0.50, blue: 0.74)
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
            .overlay {
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .stroke(HealthMesVisualStyle.line)
            }
            .shadow(color: HealthMesVisualStyle.graphite.opacity(0.06), radius: 14, y: 6)
    }
}

extension View {
    func healthMesSurface(radius: CGFloat = 20) -> some View {
        modifier(HealthMesSurfaceModifier(radius: radius))
    }
}
