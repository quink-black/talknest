import SwiftUI

struct SettingsView: View {
    @Binding var address: String
    var isModal: Bool

    @State private var draft: String
    @Environment(\.presentationMode) private var presentationMode

    init(address: Binding<String>, isModal: Bool) {
        _address = address
        self.isModal = isModal
        _draft = State(initialValue: address.wrappedValue)
    }

    var body: some View {
        NavigationView {
            Form {
                Section {
                    TextField(ServerAddress.defaultAddress, text: $draft)
                        .keyboardType(.URL)
                        .autocapitalization(.none)
                        .disableAutocorrection(true)
                        .font(.body.monospaced())
                } header: {
                    Text("服务器地址")
                } footer: {
                    Text("运行 Talknest 对话服务的电脑地址。不写端口就用 \(ServerAddress.defaultPort)，"
                         + "不写协议就用 https —— 麦克风只在 https 页面里可用。")
                }

                Section {
                    Button("开始对话") {
                        address = draft
                        if isModal { presentationMode.wrappedValue.dismiss() }
                    }
                    .disabled(ServerAddress.normalize(draft) == nil)
                }
            }
            .navigationTitle("Talknest")
            // ToolbarContent cannot branch before iOS 16, so the cancel
            // button is always present and hidden when this view is the
            // first-launch screen.
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) {
                    Button("取消") { presentationMode.wrappedValue.dismiss() }
                        .disabled(!isModal)
                        .opacity(isModal ? 1 : 0)
                }
            }
        }
        .navigationViewStyle(.stack)
    }
}

struct SettingsView_Previews: PreviewProvider {
    static var previews: some View {
        SettingsView(address: .constant("192.168.1.10"), isModal: false)
    }
}
