# TalkNest ESP32-S3 语音终端设计

## 背景与已确认资源

目标设备是 Waveshare ESP32-S3-Touch-AMOLED-2.06。开发环境使用 ESP-IDF v6.1，目标芯片为 esp32s3。

硬件为 ESP32-S3R8、8 MB Octal PSRAM、32 MB Flash，带 410×502 CO5300 AMOLED、FT3168 触摸、ES7210 双麦 ADC、ES8311 播放 codec、NS4150B 功放和 AXP2101。音频共用 I2S：MCLK 16、BCLK 41、WS 45、播放数据 40、采集数据 42、PA 46，I2C 为 14/15。官方 factory 固件已有 24 kHz、TX STD stereo、RX TDM 四槽及 ES8311 回采参考，可作为驱动依据。

TalkNest 现有 `/ws` 协议可以直接复用：上行是 24 kHz little-endian int16 mono PCM，连接后发送 `mode`、可选 `learner` 和 `start`；下行包含状态 JSON 与裸 PCM。每轮播放实际排空后必须恰好发送一次 `playback ended`。服务端已有 VAD、连续对话和 barge-in，不需要修改服务端。

## 目标

- 设备启动后自动连接 Wi-Fi 和 WSS，随后自动进入聆听，不要求用户操作屏幕。
- 用户说话后自动判句并播放回答；支持语音打断，播放中触摸屏幕可作为手动打断后备。
- PWR 短按进入 light sleep，再按唤醒并恢复连续对话；保留长按约 6 秒硬关机。
- Wi-Fi、服务器、学习者和模式只来自本地配置文件；屏幕面向使用者，不呈现网络与连接细节。
- 仅允许经过 CA 链和主机名校验的 WSS，不提供跳过证书验证的选项。
- 屏幕以对话驱动的互动形象为唯一常驻主角，随对话状态与播放音频连续变化。回复正文为中文而固件暂无 CJK 字体，句子文本层延后；错误等 ASCII 短消息可短暂覆盖。详细方案见仓库外的方向文档。

## 非目标

- 不增加唤醒词、本地 ASR/LLM/TTS、网页配网或屏幕设置菜单。
- 不修改 TalkNest 下行协议；上行允许通过 `start` 消息的 `channels` 字段声明双通道扩展（见 `docs/design/server-side-aec.md`）。不增加服务端鉴权，不承诺睡眠后恢复原会话。
- 不加入 OTA 或复杂电源轨调参；不做实时 3D 引擎、骨骼绑定或屏幕设置菜单。
- 不通过降低服务端 barge-in 阈值来掩盖回声消除问题。

## 软件架构

新工程放在顶层 `firmware/`，使用 C++ 和 ESP-IDF 组件，构建目录固定为 `firmware/build/`。

| 组件 | 职责 |
| --- | --- |
| `app_main` | 启动顺序、组件装配、致命错误处理 |
| `app_config` | 读取构建时嵌入的 JSON 和 CA，校验唯一 WSS 配置来源 |
| `board` | I2C、音频 codec、I2S、PA、AXP2101 最小初始化 |
| `audio_pipeline` | TDM 槽位提取、单/双通道上行排列、PCM 环形缓冲 |
| `talknest_client` | Wi-Fi、SNTP、WSS、JSON 事件、二进制帧、重连和播放确认 |
| `power_manager` | PWR 防抖、睡眠准备、GPIO10 唤醒和外设恢复 |
| `ui_avatar` | LVGL 互动形象：四帧表情、状态/音频/内容联动、当前句瞬时层、触摸打断 |

I2S 回调只搬运样本。采集/AFE 与播放放在高优先级音频任务，网络和控制任务独立，LVGL 低优先级。DMA、控制队列和实时任务栈使用内部 SRAM；AFE 工作区、显示 framebuffer 和有界网络缓冲使用 PSRAM。上行使用固定 PCM 块，网络阻塞时丢弃最旧音频，避免延迟无限累积。

播放使用 30 秒有界缓冲，攒够 `playback_prebuffer_ms` 指定的音频后立即开始输出，不等整段回答传完；默认 500 ms，若 `audio_end` 先到（短回复），则立即启播。这样首句延迟只取决于启播阈值和网络抖动，而不是整段 TTS 的生成与传输时间。2.4 GHz 链路抖动较大时，可调高该阈值。WebSocket 的二进制帧大于客户端缓冲时会拆成多个事件，必须按 `payload_offset` 重组，不能把每个事件都当作新帧。`stop_playback` 立即清空应用缓冲并停止本轮输出，随后只回一次 `playback ended`；正常 `audio_end` 则等应用缓冲和 I2S 尾时排空再确认。轮次编号使迟到的音频与回调失效，断线会清空上下行旧数据。

## 音频与打断策略

先打通 24 kHz 基础链路：RX 从 TDM 四槽提取 MIC1、ES8311 回采、MIC2，TX 将 mono 复制到左右声道，并直接调用 `esp_codec_dev_read()` 与 `esp_codec_dev_write()` 传输 16 位 PCM。基础链路不做采样率或位宽转换；`audio_start.sample_rate` 不是 24 kHz 时拒绝播放。基础链路在播放期间关闭上行门控，避免助手声音被误识别，但不发送 `stop` 切断回答；播放结束或被打断后，半双工路径会清空上行缓冲，避免把刚播出的回声当作下一句话。

全双工语音打断不使用设备端 ESP-SR AFE：真机测量表明该路径单核接近满载（121344 样本 / 5 秒对实时的 120000），与播放任务互相挤占，且协议的 24 kHz 与 AFE 的 16 kHz 之间双向重采样是设备上的纯开销。取而代之的是服务端 AEC：`full_duplex=true` 时上行改为双通道交错 24 kHz——slot 0（MIC1）与 slot 1（ES8311 回采），两路同源同钟、样本级对齐，播放静音期间回采自然连续；服务端用 DTLN 消除回声后交给既有 VAD 与打断判定，打断仍通过 `stop_playback` 下发。协议扩展、服务端改动与验收标准见 `docs/design/server-side-aec.md`。

只有真机验证“纯助手语音不自我打断、真人插话能可靠停止、停止后第二问完整识别”后，才默认启用全双工语音打断。若服务端 AEC 未启用或验收不达标，则保留稳定半双工和触摸打断，并明确报告限制，不伪装为已完成。

## 配置与 TLS

唯一机器配置来源是 gitignore 的 `firmware/config/talknest*.json`（可包含多份环境配置，开机按可见 SSID 选择），字段仅包括 Wi-Fi SSID/密码、`wss://host:port/ws`、mode、learner 和必要的音频校准值，其中主机名可以是 `.local` 结尾的 mDNS 名称。`firmware/config/ca.pem` 是信任锚。仓库只提交无秘密的示例文件。

首版通过 `EMBED_FILES` 将 JSON 和 CA 编入固件，修改嵌入配置后需要重新构建烧录；但多份环境配置可以一次烧录，由设备在开机时按可见 SSID 选择，移动场所时无需重烧。不引入额外文件系统和配网工具。连接前用 SNTP 建立可信时间；证书链错误、主机名/SAN 不匹配和过期证书都必须连接失败并显示原因。README 只指向示例字段，不复制实际配置值。

## UX 与功耗

- 启动：配置或证书错误时短暂显示可诊断的短消息；连接进度不上屏，只写串口日志。
- 待机与对话：常驻主角是互动形象，通过表情、呼吸、侧头、状态光晕与播放音频包络连续变化；五种对话状态映射为不同姿态，闲置也保持生命感。
- 内容：句子文本层延后（需 CJK 字体与服务端结构化文本）；错误等 ASCII 短消息短暂覆盖，不常驻。
- 播放：轻触屏幕发送 `interrupt`；AEC 验收通过后也可直接语音插话。
- 网络与连接状态不占常驻层，错误只短暂覆盖。
- 睡眠：PWR 短按后发送 `stop`，关闭 WSS、音频、PA、屏幕和 Wi-Fi，再以 GPIO10 为唤醒源进入 light sleep。
- 唤醒：重新初始化外设并建立新 WSS 会话，发送配置和 `start` 后自动聆听；按键释放并防抖后才允许再次睡眠。
- AXP2101 首版沿用官方默认供电，仅做必要状态读取，不盲目改写电源轨。

## 实施步骤与文件

1. 本文记录架构与验收约束；核心架构变化需要同步更新本文。
2. 建立 `firmware/CMakeLists.txt`、`sdkconfig.defaults`、`partitions.csv` 和 `main/`，配置 ESP32-S3、32 MB Flash、Octal PSRAM，并锁定官方 BSP、LVGL、WebSocket、ESP-SR 等依赖。
3. 加入 `components/board` 和 `ui_avatar`，移植官方 factory 的 24 kHz 音频初始化，验证屏幕、触摸、I2S 采集与播放；形象表情帧由 `scripts/gen_avatar_frames.py` 离线生成。
4. 加入 `app_config` 和 `talknest_client`，实现严格 WSS、事件状态机、重连、实时上行、播放队列和精确播放确认。
5. 完成基础连续对话，再加入 `power_manager`，验证反复睡眠/唤醒后的资源回收与重连。
6. 在 `full_duplex=true` 时加入 `audio_pipeline` 的流式重采样和 AFE/AEC，进行参考延迟校准与语音打断验收；不达标时启用明确的半双工降级。
7. 增加 `firmware/tests/` 的纯逻辑测试和真机测试入口，覆盖槽位映射、增益饱和、缓冲溢出、协议状态迁移及 `playback ended` 唯一性。
8. 更新 `docs/architecture.md` 的目录说明，并在 `firmware/README.md` 记录构建、配置和测试命令；不提交凭据、CA、build 或依赖缓存。

## 验证

- 纯净构建和主机/设备逻辑测试通过，无新增服务端回归。
- 错误 CA、错误主机名和过期证书均被拒绝；正确自签 CA 可连接。
- 真机完成至少 10 轮连续问答，音频无持续卡顿，网络断开后能恢复且不播放旧回答。
- 连续 20 次 PWR 睡眠/唤醒后可重新对话，无任务、缓冲或 codec 状态泄漏。
- 主机纯逻辑测试覆盖形象的状态/包络/选帧映射；固件构建无警告。
- 形象动画与播放包络偏差 <100 ms，UI 不增加播放 underrun，闲置时形象持续微动；播放被取消后口型能自动收起。
- 语音打断按上述三项场景单独验收；若未通过，最终报告明确半双工限制。
- 烧录会覆盖设备当前固件，因此需要保留 2.06 官方恢复镜像用于回滚。
