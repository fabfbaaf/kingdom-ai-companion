# Kingdom AI Companion

《王国：两位君主》的独立 AI 陪玩项目。**你控制 P1，AI 控制本地合作模式的 P2**；AI 通过游戏原生输入、交易与菜单回调行动，结合实时游戏状态、共享记忆和阶段计划持续决策，也能与你进行文字或语音交流。

**作者与维护：桔梗。** 自研部分采用 [MIT 许可证](LICENSE)，允许商业使用。上游引用及各自许可见 [第三方声明](THIRD_PARTY_NOTICES.md)。本项目与游戏开发商无隶属关系。

当前版本 **0.5.0 · 开发预览**。已完成离线测试、模组编译和本机部署，**尚未完成真实存档的长期陪玩或通关验收**。默认目标是推进当前战役，但不能保证 AI 能独立通关。

## 功能

| 模块 | 当前能力 |
| --- | --- |
| 游戏观测 | 当前加载岛屿的敌人、付款对象、掉落物、兵力、建筑、船只、技能、季节、任务和战役进度；不可读字段保留 null |
| P2 操作 | 左右移动、目的地移动、疾跑、原生完整付款、多币种丢币、可用技能与特殊单位操作、地图查看与选岛、出航 |
| 持续游玩 | 自由行动或协作陪玩；加载与跨岛后延续目标；普通聊天期间继续决策；用户停止或 F8 接管后不自动恢复 |
| 聊天与安排 | 并行聊天、网页与游戏内 P2 气泡；“先发展经济”“今晚守右边”等更新目标，留钱偏好可与主任务并存 |
| 本地语音 | Vosk 中文识别、输入设备选择与记忆、实时音量和错误诊断、完整语句顺序排队；可选 Windows 本地中文播报及主动提醒 |
| 记忆与计划 | 按战役保存已观测岛屿、建设、资源、任务和行动结果；聊天与决策共享上下文；经济、防御、进攻和出航阶段具有观测完成条件 |

游戏规则决定余额、耐力、冷却和岛屿是否可用。页面默认允许 AI 自主支配 **P2 钱包**，也可主动选择限定金币额度；跟随模式不付款。阶段计划是决策参考，不增加动作权限限制。控制不直接修改钱包、坐标或存档。

未加载岛屿没有实时对象数据；历史观测可能过时，未再次出现的建筑不等于被摧毁。人物与技能使用当前游戏实际提供的接口，尚不能保证全部战役的专属交互都覆盖。

## 环境

- Windows x64，合法安装的《王国：两位君主》**2.4.2 / 构建 116fe7e048**，Unity **6000.0.66**，IL2CPP 版本。
- 本地双人合作存档；P1 始终由玩家控制，不支持接管单人模式的唯一君主或联网合作。
- 源码运行需要 **Python 3.12 或更新版本**；构建模组需要 **.NET SDK 8**。Node.js 仅用于前端测试，PyInstaller 仅用于构建 EXE。
- 自主决策与聊天需要兼容 `/chat/completions` 的模型服务；跟随不需要模型。本地语音模型准备好后可离线识别。
- 首次准备加载器及工具需要联网。当前仓库提供源码；游戏更新后需要重新核对接口。

## 从源码安装

关闭游戏，克隆仓库并在项目根目录打开 PowerShell：

```powershell
git clone https://github.com/fabfbaaf/kingdom-ai-companion.git
cd kingdom-ai-companion

# 创建后台环境
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 使用已安装的 .NET SDK 8；也可填入 SDK 的绝对路径
$dotnetPath = (Get-Command dotnet -ErrorAction Stop).Source
.\scripts\build-mod.ps1 -Dotnet $dotnetPath

# 路径替换为你的游戏安装目录
$gamePath = 'D:\SteamLibrary\steamapps\common\Kingdom Two Crowns'
.\scripts\install-mod.ps1 -GameDir $gamePath -DryRun
.\scripts\install-mod.ps1 -GameDir $gamePath

# 启动后台和浏览器，不启动游戏或自动启用 AI
.\.venv\Scripts\python.exe launcher.py --game-dir $gamePath
```

`build-mod.ps1` 首次准备固定版本的 MelonLoader、带元数据修复的 Cpp2IL 和本机加载依赖，可能需要较长时间。默认 SDK 路径为 `.tools/dotnet/dotnet.exe`；使用系统安装时按上例传入 `-Dotnet`。准备完成后，安装器复用经过校验的缓存。

安装器核对游戏版本与 Unity 版本，备份检测到的本机存档和被覆盖文件。备份及安装清单位于游戏 `UserData/KingdomAI`。游戏运行时拒绝安装；`-DryRun` 只读检查，缓存齐全后可加 `-Offline`。安装不启动游戏或修改存档。

已有便携构建时可双击 `install.cmd` 安装，双击 `start.cmd` 启动；便携 EXE 不需要 Python。**GitHub 源码下载中没有预制 EXE、加载器缓存或游戏文件**，需先按上面的步骤构建。

## 开始陪玩

1. 后台默认地址为 `http://127.0.0.1:48860`。配置模型地址、模型名和密钥，再保存；“测试连接”会发送一条真实模型请求。
2. 从 Steam 或后台的“启动游戏”打开游戏。首次等待加载器生成本机接口，进入存档并开启本地合作，让 P2 加入。
3. 等后台显示 P1/P2 可操作，选择自由行动或协作玩法、目标及货币支配方式，然后点击“开始自主陪玩”。启动后台本身不会控制游戏。
4. 可在聊天中安排“先发展经济”“今晚守右边”“钱留着修船”。尚未启动自主模式时先记下目标；已运行时更新下一步安排，普通闲聊继续原任务。
5. 点击“立即停止 / 接管”、说“停止”，或在游戏内按 **F8** 接管。页面停止不等待普通聊天返回。F8 解除后也需要重新启用 AI。

后台和模组使用同一份游戏 `UserData/KingdomAI/bridge.local.json`。配置仅监听本机，包含随机令牌，不能提交到仓库。需要显式指定时：

```powershell
.\.venv\Scripts\python.exe launcher.py --bridge-config '<游戏目录>\UserData\KingdomAI\bridge.local.json'
```

持续自主会重复调用模型并按服务商规则计费。模型慢响应、游戏暂停或不可操作时可能等待；付款和技能是否达到预期效果仍需结合后续游戏状态判断。

## 语音与本地数据

首次点击“准备中文语音模型”下载约 42 MB 的 Vosk 中文模型。刷新输入设备、选择麦克风，再开启监听；无 Windows 默认输入时需手动选择。设备偏好按名称与音频后端恢复，原设备消失时提示重新选择。音量表显示 RMS 与过载提示，短暂输入溢出重置不完整句后继续识别，严重错误或积压会停止监听并显示诊断。

麦克风默认关闭，开启后切回游戏可继续收音。**关闭网页不等于关闭监听**，离开时使用“停止监听”或“退出后台”。播报与主动提醒也需手动开启；默认监听时暂停播报，耳机模式允许同时使用，目前没有回声消除。

| 本机文件 | 用途 |
| --- | --- |
| `%LOCALAPPDATA%\KingdomAICompanion\model.local.json` | 模型配置，密钥由当前 Windows 用户的 DPAPI 保护 |
| 同目录 `voice.local.json` | 输入设备偏好 |
| 同目录 `memory.local.json` | 按战役保存游戏事实、目标、计划和行动摘要 |
| 同目录 `speech` | Vosk 本地识别模型 |
| 游戏 `UserData/KingdomAI/bridge.local.json` | 仅本机使用的桥接地址与令牌 |

录音不保存、不上传；识别后的文字、相关历史和游戏状态会发给你配置的模型服务。完整对话只保留在本次后台进程中。没有可靠战役标识时仅保留会话内游戏记忆，避免误合并存档。

## 开发、打包与卸载

- 当前验证结果、测试复现及实机待验收项：[验证记录](docs/validation.md)。
- 状态、动作、共享上下文与语音接口：[桥接协议](docs/bridge-protocol.md)。
- 作者、商用许可及上游来源：[MIT](LICENSE) / [第三方声明](THIRD_PARTY_NOTICES.md)。

```powershell
# 构建便携后台（可选）
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\scripts\build-app.ps1
.\.venv\Scripts\python.exe scripts\collect-app-licenses.py

# 仅源码；或附自研 Bridge DLL，不附游戏/加载器文件
.\scripts\package.ps1 -Version '0.5.0-source' -SourceOnly
.\scripts\package.ps1 -Version '0.5.0-dev'

# 卸载前查看计划，再依据安装清单还原或移除本项目文件
.\scripts\uninstall-mod.ps1 -GameDir $gamePath -DryRun
.\scripts\uninstall-mod.ps1 -GameDir $gamePath
```

卸载保留存档、备份、日志及外部修改过的文件。`-IncludeLoaderPayload` 仅用于自行准备的本机依赖归档；其中 Unity 接口依赖的公开再分发权利尚未核实，不作为本项目公开附件发布。

源码目录：`companion` 为后台与界面，`mod/src` 为游戏桥接，`tests` 和 `mod/tests` 为测试，`scripts` 为构建/安装工具，`docs` 为协议与验证，`third_party` 保留上游许可证。
