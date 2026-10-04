# 本地语音依赖来源

本项目语音识别代码由桔梗创作。下列识别库、模型和音频驱动保留上游作者与许可，不改署为本项目作者。

| 组件 | 来源 | 许可原文 |
| --- | --- | --- |
| Vosk API 0.3.45（Alpha Cephei / 上游贡献者） | <https://github.com/alphacep/vosk-api>，Windows PyPI wheel | `VOSK-COPYING`（Apache-2.0） |
| vosk-model-small-cn-0.22 | <https://alphacephei.com/vosk/models>、<https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip> | 官方模型列表注明 Apache-2.0，保留 `VOSK-COPYING` 与 `MODEL-README` |
| python-sounddevice 0.5.6（Matthias Geier） | <https://github.com/spatialaudio/python-sounddevice> | 安装 wheel 的原文 `SOUNDDEVICE-LICENSE`（MIT） |
| PortAudio（Ross Bencina、Phil Burk 及贡献者） | <https://github.com/PortAudio/portaudio> | `PORTAUDIO-LICENSE.txt`（MIT） |
| Vosk Windows wheel 内的 GCC 运行时（libgcc、libstdc++） | <https://gcc.gnu.org/>、<https://github.com/gcc-mirror/gcc> | `GCC-COPYING3` 与 `GCC-RUNTIME-EXCEPTION`；保留上游 GCC 运行时例外 |
| Vosk Windows wheel 内的 MinGW-w64 / winpthreads 运行时 | <https://github.com/mingw-w64/mingw-w64> | `MINGW-W64-COPYING`、`WINPTHREADS-COPYING` |
| Vosk 原生识别库的 Kaldi / OpenBLAS 来源 | <https://github.com/kaldi-asr/kaldi>、<https://github.com/OpenMathLib/OpenBLAS> | `KALDI-COPYING`（Apache-2.0）、`OPENBLAS-LICENSE.txt`（BSD） |

下载时固定模型 SHA-256：`3af8b0e7e0f835ae9d414ce5df580237a3cfb08d586c9fbbb0f7ff29ad5b14ba`；压缩包大小 43,898,754 字节。该校验值由 2026-10-03 从官方 HTTPS 地址获取的压缩包计算；上游没有另行提供签名校验值。程序仅接受该固定模型，不接受用户传入下载地址。模型按用户请求下载到 `%LOCALAPPDATA%/KingdomAICompanion/speech`，不打进源码 ZIP。

`LICENSE-SOURCES.json` 记录上述完整许可文件的来源及 SHA-256。未修改这些上游库。Windows 便携应用仅使用非 ASIO 的 64 位 PortAudio DLL；不用也不分发 Steinberg ASIO 变体。麦克风默认关闭；开启后音频仅在内存中传入本机识别器，不保存音频文件，不向模型聊天服务上传音频。
