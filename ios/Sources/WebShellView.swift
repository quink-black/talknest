import SwiftUI
import WebKit

struct WebShellView: UIViewRepresentable {
    let url: URL
    @Binding var error: String?

    func makeCoordinator() -> Coordinator { Coordinator(parent: self) }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        // The reply starts playing the moment TTS audio arrives; requiring
        // a tap to start playback would break hands-free conversation.
        config.mediaTypesRequiringUserActionForPlayback = []
        config.allowsInlineMediaPlayback = true

        let view = WKWebView(frame: .zero, configuration: config)
        view.isOpaque = false
        view.scrollView.contentInsetAdjustmentBehavior = .never
        view.uiDelegate = context.coordinator
        view.navigationDelegate = context.coordinator
        view.load(URLRequest(url: url))
        return view
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {
        context.coordinator.parent = self
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        var parent: WebShellView

        init(parent: WebShellView) { self.parent = parent }

        // The page opens the microphone by itself as soon as its socket
        // connects, with no tap behind it, so the request is granted here
        // instead of letting WebKit prompt. Only the configured host gets
        // it: the shell trusts that host's certificate without checking a
        // CA, so any host would otherwise be able to take the microphone
        // by the same route.
        func webView(
            _ webView: WKWebView,
            requestMediaCapturePermissionFor origin: WKSecurityOrigin,
            initiatedByFrame frame: WKFrameInfo,
            type: WKMediaCaptureType,
            decisionHandler: @escaping (WKPermissionDecision) -> Void
        ) {
            guard isConfiguredHost(origin.host) else {
                decisionHandler(.prompt)
                return
            }
            decisionHandler(.grant)
        }

        // The LAN server uses the self-signed cert from
        // scripts/gen_dev_cert.sh. There is no CA to validate it against,
        // so its server trust is accepted as it comes -- for the
        // configured host only. Every other host keeps the default
        // validation, which refuses an untrusted certificate.
        func webView(
            _ webView: WKWebView,
            didReceive challenge: URLAuthenticationChallenge,
            completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
        ) {
            guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
                  isConfiguredHost(challenge.protectionSpace.host),
                  let trust = challenge.protectionSpace.serverTrust
            else {
                completionHandler(.performDefaultHandling, nil)
                return
            }
            completionHandler(.useCredential, URLCredential(trust: trust))
        }

        /// Hosts are compared lowercased: WebKit reports a security origin
        /// in lowercase, while the address comes from what was typed into
        /// the settings field.
        private func isConfiguredHost(_ host: String?) -> Bool {
            guard let host = host?.lowercased(), !host.isEmpty else { return false }
            return host == parent.url.host?.lowercased()
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            parent.error = nil
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            report(error)
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            report(error)
        }

        private func report(_ error: Error) {
            let url = parent.url.absoluteString
            parent.error = "无法打开 \(url)：\(error.localizedDescription)"
        }
    }
}
