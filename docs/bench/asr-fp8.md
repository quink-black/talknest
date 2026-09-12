# ASR fp8 量化实测（2026-09-13）

动机：12G 卡上 ASR（vLLM 池）吃掉 5.1G，LLM 只剩约 3.1G，9B Q4 有
一半以上的层掉到 CPU，生成只有 8-14 tok/s。ASR 权重换成 fp8（1.7G，
原 3.9G）后把池缩到 0.36，多出的 1.5G 给 LLM 多压层进 GPU。

## 配置

| | 权重 dtype | ASR_GPU_UTIL | ASR 池 | ASR KV |
|---|---|---|---|---|
| before | bf16 | 0.50 | 5176 MiB | 0.81 GiB / 7552 tokens |
| after | fp8 (Marlin) | 0.36 | 3482 MiB | 0.50 GiB / 4624 tokens |

3060 是 SM86，没有原生 FP8，vLLM 走 Marlin 权重量化并警告
"may degrade performance for compute-heavy workloads"——ASR 是短句、
计算不重，实测没有变慢。

## 速度（scripts/bench_llm.py，3 轮中位数）

| | 128-token | 512-token | TTFT | 4 轮对话每轮 (s) |
|---|---|---|---|---|
| before | 7.5 tok/s | 11.2 tok/s | 186 ms | 3.42 / 3.41 / 2.77 / 2.59 |
| after | **18.9 tok/s** | **15.7 tok/s** | 118 ms | 1.44 / 1.16 / 1.12 / 1.40 |

## 准确率（合成语料 WER）

16 句（儿童会话常见句 + 数字/绕口令）× 4 个条件：clean、白噪声
SNR 20dB、SNR 10dB、1.25 倍速。TTS 合成 → ASR 转写 → 逐词编辑距离。
420 参考 token。

| 配置 | 各次采样（错误 token 数） | 中位 WER |
|---|---|---|
| bf16 | 12, 15, 17, 16 | 16/420 = 0.038 |
| fp8 | 18, 10, 16 | 16/420 = 0.038 |

**中位完全相同**，单次波动（12-18）大于配置间差异，即 fp8 在这套
语料上没有可测量的质量损失。残留错误几乎全是分词/书写差异
（"Mingming" → "Ming Ming"、"thirty three" → "33"），不是词义错误。

## 结论

保留 fp8。生成速度翻倍级别的提升，质量与延迟没有代价。注意 fp8
本身不省显存——必须同时调小 `ASR_GPU_UTIL`，否则 vLLM 只是把省下的
权重空间让给更大的 KV 池，LLM 一分钱都没拿到。

复现：

```bash
ASR_GPU_UTIL=0.36 ASR_QUANT=fp8 scripts/systemd/services.sh start
```
