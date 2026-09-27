import SwiftUI

struct RootView: View {
    @AppStorage("serverAddress") private var serverAddress = ServerAddress.defaultAddress
    @State private var settingsShown = false
    @State private var reloadToken = 0
    @State private var loadError: String?

    var body: some View {
        content
            .sheet(isPresented: $settingsShown) {
                SettingsView(address: $serverAddress, isModal: true)
            }
            .onReceive(NotificationCenter.default.publisher(for: .shellShaken)) { _ in
                settingsShown = true
            }
            .onChange(of: serverAddress) { _ in
                // A saved address has to take effect immediately, and the
                // web view only reloads when it is rebuilt.
                loadError = nil
                reloadToken += 1
            }
    }

    @ViewBuilder
    private var content: some View {
        if let error = loadError {
            LoadErrorView(message: error, url: url) {
                loadError = nil
                settingsShown = true
            } retry: {
                loadError = nil
                reloadToken += 1
            }
        } else if let url = url {
            WebShellView(url: url, error: $loadError)
                .id(reloadToken)
                .overlay(alignment: .topTrailing) {
                    Button {
                        settingsShown = true
                    } label: {
                        Image(systemName: "gearshape")
                            .font(.title3)
                            .padding(14)
                            .foregroundColor(.secondary)
                    }
                    .opacity(0.45)
                }
        } else {
            SettingsView(address: $serverAddress, isModal: false)
        }
    }

    private var url: URL? { ServerAddress.normalize(serverAddress) }
}

struct RootView_Previews: PreviewProvider {
    static var previews: some View { RootView() }
}
