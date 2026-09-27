import Foundation

enum ServerAddress {
    static let defaultPort = 8890

    /// The machine the orchestrator runs on. Written as a host, so the
    /// port follows the service rather than this string.
    static let defaultAddress = "https://black.local"

    /// Accepts `192.168.1.10`, `192.168.1.10:8890` or a full URL, and
    /// defaults to https: a LAN host served over plain http is not a secure
    /// context, and then the page has no `navigator.mediaDevices` at all.
    static func normalize(_ raw: String) -> URL? {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if !text.contains("://") {
            text = "https://" + text
        }
        guard var components = URLComponents(string: text) else { return nil }
        if components.port == nil {
            components.port = defaultPort
        }
        return components.url
    }
}
