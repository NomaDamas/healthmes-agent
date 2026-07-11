import AppKit
import ScreenSaver

/// The saver's options sheet (System Settings → Screen Saver → Options…):
/// one checkbox — the issue-#11 PRIVACY TOGGLE ("hide health numbers", for
/// shared spaces / screen sharing) — persisted via ScreenSaverDefaults.
/// Programmatic AppKit; the checkbox title doubles as its accessibility
/// label, plus an explicit description for VoiceOver.
final class SaverConfigureController: NSObject {
    private(set) lazy var window: NSWindow = makeWindow()
    private let checkbox = NSButton(
        checkboxWithTitle: saverLocalized("saver.config.hideNumbers"),
        target: nil,
        action: nil
    )

    /// Sync the checkbox with the persisted value each time the sheet opens.
    func prepare() {
        _ = window
        checkbox.state = (SaverDefaultsStore()?.hideNumbers ?? false) ? .on : .off
    }

    private func makeWindow() -> NSWindow {
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 440, height: 170),
            styleMask: [.titled],
            backing: .buffered,
            defer: true
        )
        panel.title = saverLocalized("saver.config.title")

        let title = NSTextField(labelWithString: saverLocalized("saver.config.title"))
        title.font = .boldSystemFont(ofSize: 13)

        checkbox.setAccessibilityLabel(saverLocalized("saver.config.hideNumbers"))

        let note = NSTextField(wrappingLabelWithString: saverLocalized("saver.config.note"))
        note.font = .systemFont(ofSize: 11)
        note.textColor = .secondaryLabelColor
        note.preferredMaxLayoutWidth = 380

        let done = NSButton(
            title: saverLocalized("saver.config.done"),
            target: self,
            action: #selector(doneTapped)
        )
        done.keyEquivalent = "\r"

        let buttonRow = NSStackView(views: [NSView(), done])
        buttonRow.orientation = .horizontal

        let stack = NSStackView(views: [title, checkbox, note, buttonRow])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.edgeInsets = NSEdgeInsets(top: 16, left: 20, bottom: 16, right: 20)
        stack.translatesAutoresizingMaskIntoConstraints = false

        let content = NSView()
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: content.topAnchor),
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            buttonRow.trailingAnchor.constraint(equalTo: stack.trailingAnchor, constant: -20),
        ])
        panel.contentView = content
        return panel
    }

    @objc private func doneTapped() {
        SaverDefaultsStore()?.hideNumbers = (checkbox.state == .on)
        if let parent = window.sheetParent {
            parent.endSheet(window, returnCode: .OK)
        } else {
            window.close()
        }
    }
}
