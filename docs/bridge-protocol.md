# 游戏桥接协议 · 0.5.0

目标为 Kingdom Two Crowns 2.4.2，Windows x64，本机合作，用户 P1、AI P2。
协议不改变存档、钱包或坐标；操作通过游戏本身输入链执行。

安装与使用见 [README](../README.md)，测试证据及实机范围见 [验证记录](validation.md)。本文描述当前源码契约，不代表所有接口均已通过真实游戏验收。

桥接地址默认为 `http://127.0.0.1:48861`。所有请求携带
`Authorization: Bearer <token>`，token 由安装/配置程序生成并保存在本机，不能打入公开包。
后台每 500 ms 调用 `POST /heartbeat`；失联 2500 ms 撤销控制。
心跳 JSON 必须携带 `{control_id, session_id, control_epoch}`：`control_id` 是每次跟随、自主或手动运行的新 UUID，`session_id` 和 `control_epoch` 固定为当前控制租约的游戏会话与控制代次。原生出航/加载结束后可为同一自主目标重新创建租约；旧租约不能换用停止之后的新代次。

首次合法心跳授予此运行的控制编号。停止、场景切换、失联或绑定失败会撤销编号并增加代次；旧指令、旧心跳和停止之前尚未到达的首次心跳均不能恢复控制。当前游戏会话内，已撤销编号不可复用；下一次运行必须重新读取状态并产生新 UUID。

## 状态：GET /state

```json
{
  "bridge_version": "0.5.0",
  "game_version": "2.4.2",
  "session_id": "每次启动、场景或受控角色更换产生的新标识",
  "observation_seq": 1,
  "captured_at": "UTC ISO8601",
  "scene": "当前场景",
  "ready": false,
  "reason": "尚未实机验证输入绑定",
  "coop": false,
  "controlled_player_id": 1,
  "control_epoch": 1,
  "input_released": true,
  "players": [],
  "capabilities": [],
  "diagnostics": []
}
```

玩家记录：`player_id`（游戏内部 ID：P1=0，P2=1）、`x`、`coins`、`stamina`、
`crown`、`current_payable`、`transaction_pending`、`can_sprint`。无法读取的事实为 null。
`stamina` 保留游戏原始值，不假定为百分比。`can_sprint` 根据坐骑的原生 `IsTired` 与可读取的正耐力计算；最终移动速度和疲劳仍由原游戏规则决定。
`current_payable` 为 null 或 `{target_id, name, x, price, currency, can_pay}`；currency 小写，金币为 `coins`。
目标和技能标识只在当前 session 内有效。玩家另提供 `currencies`、`steed_type`、`payment_timeout_ms`。`ui_ready=true` 表示 AI 打开的地图在世界暂停时仍可操作。

`world.campaign` 提供 `started_at`（原生 `CampaignSaveData.realStartDateTime`）、`reign` 和 `challenge_id`。后台用战役开始时间、主题、biome 和挑战标识分组持久记忆；缺少有效开始时间时不把当前会话误合并到磁盘历史。

后台共享上下文增加 `memory` 和 `plan`。历史资源与对象附观测时间；对象的 `present_in_last_observation=false` 不表示已经被摧毁。`explored` 是已观测 P2 位置的最小/最大范围，不能证明整个区间都走过。

模型动作输出可以包含可选 `plan`：`stage`、`objective`、`criteria` 与 `fallback`。`stage` 为 explore/economy/defense/attack/sail/complete；每项 criteria 使用有限指标、gte/lte/eq/neq 比较和数字阈值，由游戏新观测判断完成。未知指标保持 unknown；complete 必须引用 `campaign_completed == 1`。这个字段只更新后台规划，序列化游戏命令时始终排除；不增加动作或货币权限限制。

指标为 coins/workers/farmers/archers/damaged_walls/construction_remaining/portals/land/island_secured/campaign_completed/completed_islands/is_night/night_survived。`night_survived` 依据当前安排后的昼夜观测：先实际见到夜晚，再见到白天才为 1；没有可读昼夜状态时保持未知。“今晚守右边”在白天安排时不会立即完成；留钱修船等附加偏好不会单独改变主任务的阶段。

语音设备接口：需要页面会话的 `GET /api/voice/devices` 只枚举输入设备，`PUT /api/voice/device` 接受 `{"device":null}` 或非负整数设备编号。选择不打开麦克风，监听中禁止换设备。偏好按名称/音频后端保存，重新枚举时恢复新编号。`GET /api/voice/status` 额外返回 level、peak、last_audio_at、overflows、error_code 和 device_error。完整语音等待队列按顺序处理；队列满或事件丢失在 `/api/chat` 的 voice_error/voice_dropped 中报告，voice_queued 给出等待句数。

`world` 增加 `campaign`（主题、岛屿、通关/失败、解锁与已访问记录）、`environment`（季节、时间、边界）、`targets`、`enemies`、`dropped_items`、`units`、`structures`、`abilities`、`quests`、`ui` 和 `control`。全场对象来自当前加载岛屿的注册表；世界扫描缓存 250 ms，玩家及控制状态约每 50 ms 更新。原有 nearby 数组保留兼容，当前也返回全场记录。未加载岛屿不伪造实时对象；不可读字段为 null。
输入能力实际可用后才能出现在 `capabilities` 中：`move`、`move_long`、`sprint`、`stop`、`pay`、`pay_coin`。
`extended_world` 标识 0.4.0 扩展。增加 `move_to`、`drop`、`ability`、`map`、`sail`、`pay_currency`。`move_long` 保留兼容，0.4.0 不再有 5000 ms 上限。

## 请求：POST /command

```json
{
  "action_id": "UUID",
  "session_id": "当前会话",
  "control_id": "本次运行新 UUID",
  "control_epoch": 1,
  "player_id": 1,
  "operation": "move",
  "direction": "left",
  "duration_ms": 500,
  "sprint": true,
  "target_id": null
}
```

当前动作：

| operation | 参数与行为 |
| --- | --- |
| move | direction=left/right、duration_ms 正整数（至少 100，受 Int32 表示范围约束）、可选 sprint；无 5 秒策略上限 |
| move_to | target_x、可选 sprint；原生移动到位置，误差不超过 0.5 时结束；无位置进展时返回失败 |
| stop | 撤销控制并释放输入 |
| pay | target_id、可选 currency；按当前选中目标原生完整价格付款，兼容 max_coins 字段但不以其限制价格 |
| pay_coin | 兼容价格恰为 1 的目标；常规付款使用 pay |
| drop | currency、amount；通过 TryDropCurrency 逐枚执行，回执 effect 提供实际丢出数量 |
| ability | ability=当前 abilities 中的 ability_id；可选 ability_action=activate/deactivate/cancel/attack/release_attack/channel，实际动作随技能种类而定 |
| map | map_action=open/close/left/right/select/confirm；select 还需 land=游戏实际岛屿编号 |
| sail | P2 已登船且船只准备阶段允许时调用原生 SailAway，随后读取地图/加载状态 |

技能列表也提供 rear（原生坐骑后仰）及可用时的 formation、unit_action、release_unit。特殊单位操作仅沿游戏已分配给 P2 的控制通道，不重新分配 P1 单位。unit_action 激活后保持当前单位交互，deactivate 或停止释放。

支付检查当前目标标识、货币、余额与原生 CanPay，取消金币币种与 20 枚上限。支付耗时根据原生持币阈值、逐枚间隔、完整价格与交易动画计算，无固定 10 秒上限。保持输入直至原生完成阶段，再释放并等待 TransactionComplete、浮币归零和交易空闲；同期捡币不再使成功交易被净余额检查误判。

地图通过原生菜单、岛屿按钮与确认回调执行，不直接写土地/存档字段。AI 打开的地图由 ui_ready 管理，只允许地图命令；普通暂停不会允许移动。view-only 地图、不可选岛屿、不可确认按钮按原生状态报告失败。

操作参数不可混用。过期会话拒绝；同时只执行一个动作；重复相同 action_id 返回原回执，内容冲突拒绝。主线程执行技能、地图、丢币与出航，HTTP 线程只排队。所有动作仍服从 2500 ms 心跳看门狗和立即停止。失败或结果不明不要求人工核验，刷新状态后重新决策，不重发旧 action_id。

旧桥接未声明 extended_world 时仍按 1000/5000 ms 兼容，缺少新增能力时不派发相应动作。

自主移动中，Python 在本段临近结束时读取一帧新状态并准备下一次决策，最多保留一个候选；上一移动返回 completed 后即可派发，不核验位置变化。候选绑定当前控制编号和目标代次，采样超过 2.5 秒、局面变化、停止或目标更新时丢弃。候选支付不执行，待移动结束后重新读取状态与价格再决策。每次提前请求仍扣减一次决策预算。模型慢响应时当前移动按时结束，等待期间保持中性输入，不无限延长或自动重复旧动作。

返回与 `GET /commands/<action_id>` 格式相同：
`{action_id, session_id, status, message, before, after, effect}`。
状态包括 queued/running/completed/cancelled/failed/unknown。
`completed` 表示输入执行完成，不是到达目的地或购买成功的证明。后台将回执与后续实时观测交给模型，不再以位置或金币差值作为继续游玩的门槛。

## 持续自主与聊天气泡

后台 `POST /api/control/start` 接受 `continuous: true`，只在自主模式有效。界面默认勾选，旧客户端省略时仍使用有限 `decision_budget`。持续模式每次模型请求增加 `decisions_made`，`decisions_remaining` 为 null。模型 `stop` 在此模式仅表示本轮等待；用户停止与 F8 不受此语义影响。未派发的付款前提变化会重新观察；本轮 failed/unknown 自动结束旧输入并重新规划，普通取消和人工停止不恢复；0.4.0 在已识别的原生出航/加载过渡后，可创建新会话租约继续当前目标，F8 不恢复。

自主启动接受 `spending_mode: budgeted|wallet`。API 和页面默认 wallet。wallet 不设累计投币额度，`coin_budget_remaining=null`，实际钱包余额仍须足以支付；budgeted 按现有 `coin_budget` 扣减，等待、恢复与改目标不补充额度。跟随不付款；手动完整付款只执行当前目标的一笔原生交易。实际支配方式由控制状态与共享上下文返回，网页草稿和聊天不能修改本次方式。

后台不阻塞于旧动作日志，`needs_review` 保持 false，移除人工核验界面。恢复失败或未知动作时保留当前目标、玩法和支配方式，撤销旧控制编号并取得新编号；游戏真实取消、F8、会话切换或恢复期间再次接管不会被自动覆盖。既有 acknowledge API 保留兼容，不是继续游玩的必要步骤。

`dialogue_bubble` 能力声明只接受纯文字展示。授权 `POST /dialogue` 格式为 `{message_id, text, session_id}`，分别限制 1–100、1–600、1–200 个 UTF-16 字符；session 必须匹配当前观测。此接口不授予控制租约、不派发游戏操作、无需运行 AI。响应 `accepted` 或 `duplicate` 只表示接收；不代表已在屏幕呈现。

HTTP 线程保存最新一条，主线程在 P2 上方显示最多 180 个文字元素，约 10 秒后淡出，场景切换清空。纯文字关闭 richText，气泡只绘制，不处理输入。相同 session/message_id 去重、限频且不会把旧回复带入新会话。后台只转发可播放的助手回复，最新待发消息替换旧消息，失败不重试。`GET /api/dialogue/status` 只读展示可用性和转发情况。

自主启动另接受 `play_style: cooperate|independent`，省略保持旧版协作策略；仅自主模式生效。`independent` 表示合作存档中的 P2 独立行动，不接管 P1，也不是单人模式。选择风格与金币预算只在启动时应用，修改网页草稿不改变当前运行；聊天改目标保留风格和剩余额度，明确跟随指令才切换跟随。独立风格的移动预取不依赖 P1 钱包等变化，但仍复核 P2、敌情、会话、代次和能力。状态与共享上下文返回实际 `play_style`；不在自主模式时为 null。

控制状态提供 `phase` 与 `phase_seconds`，阶段为 idle / observing / deciding / moving / paying / recovering / waiting。持续模式对模型临时超时、HTTP 429/5xx、无效动作格式按决策间隔退避，最长等待 15 秒后重试；配置、鉴权等不可重试错误仍停止。停止、F8 和控制租约规则不变，重试不得延长当前输入。

失败或未知的自主付款对象在同一游戏会话内暂避 20 秒，共享上下文通过 `payment_cooldowns` 提供目标标识和剩余秒数。该限制不增加预算、不改变原生付款回执判断，也不自动重发旧 action_id。

聊天和动作解析允许完整 JSON 代码块与 `type`、`reason`、`explanation`、`confidence` 注释字段；重复键、多段 JSON 和额外控制字段仍拒绝。非目标聊天的空 `goal` 视为未设置。明确的“目标：…”直接设置目标，未运行时仅记录；运行时保留当前玩法和金币支配方式，并让旧目标回复失效。

## 接管

`POST /stop` 无需新决策，可立即请求取消所有 AI 输入。
该请求不需要控制编号，始终允许重复停止；HTTP 成功只表示接受请求。`input_released=false` 时仍在等待游戏主线程，后台必须读取接受请求之后的新鲜状态，确认 `input_released=true`，否则保留未确认警告。
游戏内 F8 立即接管，并保持 AI 不可操作；再次 F8 解除人工接管后，仍须在界面重新启动 AI。
场景切换、P2 离场、游戏暂停、后台心跳超时和退出均撤销控制。
游戏对象读取、控制输入与回执生成在主线程完成；HTTP 线程仅接受命令和返回快照。

`POST /coop/open` 在游戏主线程请求官方本地合作提示，返回 202 表示已排队。
状态 `coop_request_result` 显示实际拒绝原因或提示成功；需要在游戏内确认和选择 P2 外观。
此接口不自行创建角色、不绕过菜单流程。仅本机离线 Playing / Menu 状态允许请求。

P2 已有原生交易未结算时，不允许开始跟随或移动，避免接管时取消用户自己的付款。AI 自己正在执行的完整交易在同一控制编号内继续，到原生结算或明确中止为止。

模拟通过与 DLL 编译通过均不等于真实陪玩已验收。

## 聊天与语音调度

`POST /api/chat` 接受 `{"text":"消息"}`，`GET /api/chat` 返回消息、目标和共享上下文。普通聊天有独立模型通道，最多三条等待，不暂停动作循环。停止、跟随和常见安排走优先路径；复杂表达由模型读取游戏状态、近期对话、记忆和计划判断。新目标使旧目标的待发决策失效，保留本次玩法和货币支配设置。尚未启动自主模式时只记下目标，不自动启用控制。

语音 `POST /api/voice/start` / `POST /api/voice/stop` 显式开启或关闭；已完成语句按顺序处理，停止和新安排可越过慢聊天。关闭监听撤销尚未完成的语音请求，迟到回复不能恢复控制。音频只在内存中处理；完整聊天不写入持久记忆，游戏事实与安排写入 `memory.local.json`。
