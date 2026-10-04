# 第三方来源与许可

本项目的新增 AI 陪玩程序、桥接协议、界面和安装流程署名 **桔梗**，采用 [MIT](LICENSE)，允许商业使用。第三方代码、加载器、运行时和游戏本身分别遵守各自许可；本项目许可不授予游戏资源或第三方商标的权利。

| 来源 | 用途与引用范围 | 许可 |
| --- | --- | --- |
| [FredApps/KingdomMod](https://github.com/FredApps/KingdomMod/tree/c0f1fd9fdafc2b12b8680ebbd15eb2b2c9734b86) | 状态读取与 IL2CPP 模组接入参考；`scripts/prepare-loader.ps1` 中 Cpp2IL 剥离属性元数据修复函数基于其 `tools/install-patched-cpp2il.ps1` 改编。保留原贡献者署名。本项目不安装其作弊功能或示例模组。 | MIT；完整许可见 [third_party/KingdomMod/LICENSE](third_party/KingdomMod/LICENSE)，Copyright (c) 2026 KingdomMod contributors。 |
| [LavaGang/MelonLoader v0.7.3](https://github.com/LavaGang/MelonLoader/releases/tag/v0.7.3) | Windows IL2CPP 模组加载器；安装时从官方发布页下载，保留发行包内 `Documentation/LICENSE.md` 与 `NOTICE.txt`。 | [Apache-2.0](https://github.com/LavaGang/MelonLoader/blob/v0.7.3/LICENSE.md) 及其发行包列出的依赖许可。 |
| [SamboyCoding/Cpp2IL](https://github.com/SamboyCoding/Cpp2IL/tree/2022.1.0-pre-release.21) | 从用户本机游戏生成互操作数据。安装前下载固定版本源码，应用上述元数据修复并本地构建；完整 Cpp2IL MIT 许可与补丁许可随工具复制。 | [MIT](https://github.com/SamboyCoding/Cpp2IL/blob/2022.1.0-pre-release.21/LICENSE)，Copyright (c) 2020 Sam Byass；各依赖保留各自许可。 |
| [BepInEx/HarmonyX v2.10.2](https://github.com/BepInEx/HarmonyX/tree/v2.10.2) | 加载器附带的 `0Harmony.dll`；基于 Andreas Pardeike 的 Harmony。 | MIT，Copyright (c) 2020 BepInEx；完整许可见 [third_party/HarmonyX/LICENSE](third_party/HarmonyX/LICENSE)。 |
| [BepInEx/Il2CppInterop 1.5.1-ci.845](https://github.com/BepInEx/Il2CppInterop/tree/f03c8f4ae507d47ea814f3d11d1ec6b0391c1576) | 加载器实际 DLL 的 `AssemblyInformationalVersion` 为 `1.5.1-ci.845+f03c8f4ae507d47ea814f3d11d1ec6b0391c1576`，对应此固定提交；稳定版 `v1.5.1` 不同，不能代替对应源码。本项目作为独立 DLL 动态使用，未修改该库。 | **LGPL-3.0**；固定提交的原始 [LICENSE](third_party/Il2CppInterop/LICENSE.CI)、完整 [GPL 文本](third_party/Il2CppInterop/COPYING) 与 [LGPL 补充条款](third_party/Il2CppInterop/COPYING.LESSER) 随包保留；对应提交的源码 ZIP 随便携依赖提供。用户可按该许可替换、调试和重新链接该库，自研 MIT 不限制这些权利。 |
| [.NET](https://github.com/dotnet/runtime) | 项目构建和加载器私有运行时；不修改系统运行时。 | MIT 及分发包内第三方许可。 |
| [CPython](https://www.python.org/)、[FastAPI](https://github.com/fastapi/fastapi)、[Uvicorn](https://github.com/encode/uvicorn)、[HTTPX](https://github.com/encode/httpx)、[Pydantic](https://github.com/pydantic/pydantic) 及构建环境依赖 | 本地陪玩后台与便携 EXE。实际构建环境的版本、项目来源和原始许可证文件保存在 `third_party/python/LICENSES.json` 及对应子目录；记录也含开发工具，不表示全部打进 EXE。 | 分别保留 PSF、MIT、BSD 及各依赖原始许可，不以自研 MIT 替代。 |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | 将 Python 后台打包为便携 EXE。 | GPL-2.0-or-later，附用于应用分发的特殊许可例外；完整原始 `COPYING.txt` 随 `third_party/python` 保留。该例外允许本项目自研代码继续使用 MIT。 |
| [Vosk](https://github.com/alphacep/vosk-api) / [中文小模型 vosk-model-small-cn-0.22](https://alphacephei.com/vosk/models) | 本机中文语音转文字。模型从官方 HTTPS 地址下载并核对固定 SHA-256；模型单独存放在当前用户目录，不采集或打包用户音频。 | 识别程序与该模型为 Apache-2.0；完整原始许可和来源随 `third_party/vosk` 保留。 |
| [python-sounddevice](https://github.com/spatialaudio/python-sounddevice) / [PortAudio](https://www.portaudio.com/license.html) | 显式开启所选 Windows 输入设备，用内存中的短音频块供本地识别；便携后台只附带 Windows x64 的普通 PortAudio 后端。 | MIT 及原始 PortAudio 许可；完整许可随 `third_party/vosk` 保留，依赖版本清单见 `third_party/python`。 |
| [MelonLoader UnityDependencies](https://github.com/LavaGang/MelonLoader.UnityDependencies/releases/tag/6000.0.66) | 加载器生成互操作程序集所需的 Unity 6000.0.66 接口依赖；安装时获取，验证固定 SHA-512。 | 按原发行内容及 Unity 适用条款使用，不属于本项目 MIT 授权范围。当前 `-IncludeLoaderPayload` 为本机依赖归档；未经核实再分发权利，不将包含 Unity 接口依赖的归档公开发布。 |

上述引用区分自研代码和上游贡献，并不表示项目获得游戏开发商认可。游戏 DLL、IL2CPP 元数据、游戏美术音频、生成的游戏互操作程序集、真实存档和私密配置均不包含在本项目发布包内。

默认源码包与自研 Bridge DLL 不包含加载器二进制。`-IncludeLoaderPayload` 可将已准备的依赖做成本机便携归档；该归档包含加载器许可、Cpp2IL 与补丁许可、HarmonyX 许可、Il2CppInterop 完整 GPL/LGPL 及对应源码、.NET 许可和第三方声明。Unity 接口依赖的公开再分发仍需单独核实。
