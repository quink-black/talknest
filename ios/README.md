# Talknest iPad app

一个只做壳的原生 app：启动后全屏加载 `https://<主机>:8890`，也就是仓库
`web/` 下的同一套页面，不做任何业务逻辑。壳负责的是浏览器在 iPad 上做不
到或做不好的四件事：麦克风授权、音频免点击自动播放、信任局域网自签证书、
记住服务地址。

页面本身在 WebSocket 连上之后会自己开麦，所以正常流程是打开 app 就能说话。
app 使用期间 iPad 不会自动锁屏。

## 目录结构

```
ios/
  project.yml          # xcodegen 规格：部署目标 iOS 15、Info.plist、签名方式
  Info.plist           # 由 xcodegen 生成，随仓库提交
  Talknest.xcodeproj/  # 由 xcodegen 生成，不提交
  Sources/             # SwiftUI + WKWebView，全部逻辑
  Resources/           # 图标资源
  scripts/
    make_icons.sh      # 从仓库根 whale.webp 生成 AppIcon
    build.sh           # 命令行构建
```

`Sources/TalknestApp.swift` 是入口：配置音频会话、预先请求麦克风权限、
创建窗口。`Sources/WebShellView.swift` 是 WKWebView 的包装，麦克风授权与
自签证书都在这里放行。`Sources/RootView.swift` 决定显示设置页还是网页。

## 前置条件

1. **服务端必须是 https。** iPad 的 WKWebView 只在安全上下文里暴露
   `navigator.mediaDevices`，走 http 时页面拿不到麦克风。按主 README 的
   「浏览器访问（安全上下文）」一节生成证书并以 TLS 启动：
   `scripts/gen_dev_cert.sh`，然后 `scripts/start_web.sh`。
2. **xcodegen**：`brew install xcodegen`。Xcode 工程由 `project.yml` 生成，
   不手写、不提交。
3. iPad 与运行服务的电脑在同一个 Wi-Fi 下。

## 生成工程

```bash
cd ios
xcodegen generate      # 写出 Talknest.xcodeproj 与 Info.plist
```

改了 `project.yml`（比如部署目标或 Info.plist 键）之后重新跑一次。

## 编译

```bash
./scripts/build.sh                        # 任意已连接的 iOS 设备
DEVICE_ID=<udid> ./scripts/build.sh       # 指定设备
DEVELOPMENT_TEAM=<teamid> ./scripts/build.sh
```

首次编译时 Xcode 会提示下载 iOS 26.5 组件（Settings → Components），需要
下载，否则真机目的地不可用。

**AppIcon 编译报错**：若出现 `No simulator runtime version [...] available
to use with iphonesimulator SDK version 23F73`，说明本机没装与 SDK 匹配的
iOS 26.5 模拟器运行时，而 actool 编译 AppIcon 时不论目标是真机还是模拟器
都要用到它。两个办法：

- 在 Xcode 的 Settings → Components 里安装 iOS 26.5 Simulator（需要管理员
  密码，之后图标与模拟器调试都能用）；
- 或者跳过图标编译：`SKIP_APP_ICON=1 ./scripts/build.sh`，此时 iPad 上显示
  默认图标，图标资源仍然留在仓库里。

## 安装到 iPad

免费 Apple ID 走自动签名，装到设备上 7 天过期，过期后重新编译安装一次即可。

1. Xcode → Settings → Accounts 登录 Apple ID。
2. 打开 `Talknest.xcodeproj`，target 的 Signing 里选自己的 Personal Team。
3. iPad 用数据线连上，信任这台电脑，首次连接时让 Xcode 下载设备支持。
4. 选中 iPad 作为目的地，Run。

也可以只用命令行（需要先在 Xcode 里登录过账号）：

```bash
DEVELOPMENT_TEAM=<teamid> DEVICE_ID=<udid> ./scripts/build.sh
xcrun devicectl device install app --device <udid> build/Debug-iphoneos/Talknest.app
```

## 使用

- 默认地址写在 `Sources/AppState.swift` 的 `defaultAddress`，打开 app 直接
  加载它。不写端口默认 8890，不写协议默认 https；换机器时在设置页改地址，
  地址存在本机。
- 右上角半透明齿轮，或者摇一摇 iPad，可以打开设置页。
- 连不上服务器时显示中文提示页，可以重试或改地址。
- iPad 首次访问局域网会弹「本地网络」授权，必须允许，否则 WebSocket 连不上。

## 部署目标

iOS 15.0，因为目标设备是 iPad 5 代（iPad6,11，最高 iPadOS 16）。麦克风
授权用的 `WKUIDelegate` 回调正好也是 iOS 15 引入的。
