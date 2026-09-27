import SwiftUI

struct LoadErrorView: View {
    let message: String
    let url: URL?
    let changeAddress: () -> Void
    let retry: () -> Void

    var body: some View {
        VStack(spacing: 20) {
            Image(systemName: "wifi.exclamationmark")
                .font(.system(size: 52))
                .foregroundColor(.secondary)
            Text("连不上 Talknest")
                .font(.title2)
            Text(message)
                .font(.body)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
            Text("请确认电脑已经启动对话服务，并且 iPad 和它在同一个 Wi-Fi 下。")
                .font(.footnote)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
            HStack(spacing: 16) {
                Button("重试", action: retry)
                    .buttonStyle(.borderedProminent)
                Button("修改地址", action: changeAddress)
                    .buttonStyle(.bordered)
            }
        }
        .padding(32)
        .frame(maxWidth: 560)
    }
}

struct LoadErrorView_Previews: PreviewProvider {
    static var previews: some View {
        LoadErrorView(
            message: "无法打开 https://192.168.1.10:8890：连接被拒绝",
            url: URL(string: "https://192.168.1.10:8890"),
            changeAddress: {}, retry: {})
    }
}
