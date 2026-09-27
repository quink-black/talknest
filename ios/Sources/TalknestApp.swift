import AVFoundation
import SwiftUI
import UIKit

// The shell is one full-screen WKWebView, so the classic UIKit entry point
// is used rather than the SwiftUI App lifecycle: owning the root window is
// what lets a shake gesture reopen the settings screen after first launch.
@main
final class TalknestApp: UIResponder, UIApplicationDelegate {
    var window: UIWindow?

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
    ) -> Bool {
        // A conversation ends if the iPad locks mid-sentence.
        UIApplication.shared.isIdleTimerDisabled = true
        AudioSession.configure()
        AudioSession.requestMicrophoneAccess()

        let window = ShellWindow(frame: UIScreen.main.bounds)
        window.rootViewController = ShellHostingController(rootView: RootView())
        window.makeKeyAndVisible()
        self.window = window
        return true
    }
}

enum AudioSession {
    // playAndRecord with defaultToSpeaker: the page records and plays at
    // the same time, and without defaultToSpeaker the reply would come out
    // of the earpiece while the microphone is open.
    static func configure() {
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(
            .playAndRecord, mode: .voiceChat,
            options: [.defaultToSpeaker, .allowBluetoothHFP, .mixWithOthers])
        try? session.setActive(true)
    }

    // The page calls getUserMedia on its own as soon as the WebSocket
    // opens. Asking here first means that call does not stop on a prompt.
    static func requestMicrophoneAccess() {
        AVAudioSession.sharedInstance().requestRecordPermission { _ in }
    }
}

final class ShellWindow: UIWindow {
    override func motionEnded(_ motion: UIEvent.EventSubtype, with event: UIEvent?) {
        if motion == .motionShake {
            NotificationCenter.default.post(name: .shellShaken, object: nil)
        }
        super.motionEnded(motion, with: event)
    }
}

final class ShellHostingController: UIHostingController<RootView> {
    override var prefersStatusBarHidden: Bool { true }
}

extension Notification.Name {
    static let shellShaken = Notification.Name("TalknestShellShaken")
}
