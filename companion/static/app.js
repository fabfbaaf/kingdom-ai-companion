"use strict";

const $ = (id) => document.getElementById(id);
let session = "", latest = null, refreshing = false, busy = false, modelDirty = false;
let backendConnected = false, shuttingDown = false, dialogueRefreshing = false, dialogueEpoch = 0;
let executionDirty = false;
const frontendSubscribers=new Set();
const names = {move_to:"移动到目的地",drop:"丢币",ability:"使用技能",map:"地图操作",sail:"出航",extended_world:"全场状态",pay_currency:"多币种付款",idle:"已停止",follow:"正在跟随",autonomous:"自主陪玩中",manual:"手动操作中",move:"移动",move_long:"长段移动",sprint:"疾跑",stop:"停止",pay:"付款",pay_coin:"单枚付款",completed:"输入结束",verified:"已核验",unverified:"待观察",unknown:"反馈未返回",failed:"本轮失败",cancelled:"已取消",stopped:"已停止",waiting:"等待下一帧"};
const campaignGoal="自主完成当前战役：依据真实地图、资源、建设、兵力、任务和岛屿进度，发展经济与防御、清除贪婪威胁、准备出航并推进各岛目标；以游戏报告战役完成为准，自主使用当前货币和技能。";
const defaultGoals={cooperate:campaignGoal+"与P1协作，但不等待逐句指令。",independent:campaignGoal+"P2自由行动，不以P1是否移动作为行动条件。"};
let lastDefaultGoal=defaultGoals.independent;
if(!["cooperate","independent"].includes($("play-style").value)) $("play-style").value="independent";

function draftPlayStyle(){return $("play-style").value==="cooperate" ? "cooperate" : "independent";}

function renderPlayStyle(value){
  const control=value?.control||{},style=draftPlayStyle();
  $("play-style-note").textContent=style==="independent" ? "自由模式让 P2 自行安排巡视和购买；P1 始终由你手动控制。"+($("continuous-play").checked ? "持续自主已勾选，P1 未操作时 P2 也可行动；游戏暂停或进入菜单时仍会停止动作。" : "要一次启动后持续自由游玩，请勾选「持续自主游玩」；游戏暂停或进入菜单时仍会停止动作。") : "协作模式按营地与 P1 的局面安排 P2 行动；P1 始终由你手动控制。游戏暂停或进入菜单时仍会停止动作。";
  const actual=control.play_style==="independent" ? "自由游玩（P2独立行动）" : "协作陪玩";
  $("play-style-status").textContent=shuttingDown ? "后台正在退出，玩法设置不会启动新的动作。" : !backendConnected ? "后台连接中断，实际玩法暂未读取。" : control.running&&control.mode==="autonomous" ? `实际运行：${actual}。当前选择是下一次启动的草稿，不会切换本次运行。` : control.running&&control.mode==="follow" ? "实际运行：跟随 P1，不采用自主玩法；当前自主选择仅是下一次启动的草稿。" : "尚未启动自主游玩，当前选择仅是下一次启动的草稿。";
}

function frontendState(){return {latest,backendConnected,shuttingDown,sessionReady:!!session,busy};}
function notifyFrontend(event="state"){for(const listener of frontendSubscribers){try{listener(frontendState(),event);}catch{ /* Keep an optional widget from breaking emergency controls. */ }}}
globalThis.KingdomCompanion=Object.freeze({api,showNotice:notice,getState:frontendState,emergencyStop,
  subscribe(listener){frontendSubscribers.add(listener);listener(frontendState(),"initial");return()=>frontendSubscribers.delete(listener);}});

function notice(message, good=false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("good", good);
  $("notice").hidden = !message;
}

async function api(path, options={}) {
  if (shuttingDown && path!=="/api/shutdown") throw new Error("后台正在退出，请重新启动后刷新页面。");
  const response = await fetch(path, {...options, headers:{"X-Kingdom-Session":session,"Content-Type":"application/json",...(options.headers||{})}});
  const value = await response.json();
  if (!response.ok) { const error=new Error(value.detail || `请求失败：HTTP ${response.status}`);error.status=response.status;throw error; }
  return value;
}

function number(value, digits=1) {
  return typeof value === "number" ? value.toLocaleString("zh-CN",{maximumFractionDigits:digits}) : "不可读取";
}

function player(id, value) {
  const fields = [number(value?.x),number(value?.coins,0),number(value?.stamina)];
  const list = $(id).querySelectorAll("dd");
  fields.forEach((item,index) => { list[index].textContent = value ? item : "—"; });
}

function applyModel(value) {
  $("model-status").classList.toggle("good", value.configured);
  $("api-key").placeholder = value.key_present ? "已保护保存 · 留空保留" : "输入密钥（本地服务可留空）";
  if (!modelDirty) { $("endpoint").value=value.endpoint; $("model").value=value.model; }
}

function executionFields(locked=false) {
  const local=$("execution-provider").value==="ollama";
  $("execution-fields").hidden=!local;
  $("execution-model").required=local;
  for(const id of ["execution-endpoint","execution-model","execution-context","execution-timeout","execution-installed","execution-models-refresh"]) $(id).disabled=locked||!local;
}

function applyExecution(value, locked) {
  const settings=value.execution||{provider:"main",endpoint:"http://127.0.0.1:11434",model:"",context_tokens:8192,timeout_seconds:60};
  if(!executionDirty){
    $("execution-provider").value=settings.provider;
    $("execution-endpoint").value=settings.endpoint;
    $("execution-model").value=settings.model;
    $("execution-context").value=settings.context_tokens;
    $("execution-timeout").value=settings.timeout_seconds;
  }
  $("execution-provider").disabled=$("save-execution").disabled=locked;
  $("test-execution").disabled=locked||executionDirty||!(value.execution_configured??value.configured);
  executionFields(locked);
  $("execution-note").textContent=executionDirty ? "执行配置有未保存的修改；保存后再测试或开始游玩。" : settings.provider==="ollama" ? `已保存 · 动作决策使用 Ollama ${settings.model}；聊天使用默认模型。` : "已保存 · 动作决策与聊天共用默认模型。";
}

function usageText(usage,label="本次启动") {
  if(!usage||usage.reported_requests<1)return `${label} · token 用量尚未返回。`;
  const cache=typeof usage.cache_hit_ratio==="number" ? `缓存命中 ${(usage.cache_hit_ratio*100).toFixed(1)}%${typeof usage.cache_hit_tokens==="number" ? ` · 命中 ${usage.cache_hit_tokens.toLocaleString()} / 未命中 ${usage.cache_miss_tokens.toLocaleString()}` : ""}` : "服务商未返回缓存用量";
  return `${label} · 输入 ${Number(usage.prompt_tokens).toLocaleString()} · 输出 ${Number(usage.completion_tokens).toLocaleString()} token · ${cache}`;
}

function renderUsage(usage) {
  const routes=usage?.routes;
  $("model-usage").textContent=Array.isArray(routes)&&routes.length ? routes.filter(item=>item.provider!=="ollama").map(item=>usageText(item,`本次启动 · ${item.lane==="chat" ? "聊天/连接测试" : "默认执行"} ${item.model}`)).join("\n")||"默认模型尚无请求用量。" : usageText(usage);
  $("execution-usage").textContent=Array.isArray(routes) ? routes.filter(item=>item.provider==="ollama").map(item=>usageText(item,`本次启动 · Ollama ${item.model}`)).join("\n")||"Ollama 执行用量尚未返回；本地缓存不属于云端计费缓存。" : "Ollama 执行用量尚未返回。";
}

function paymentIssue(state, p2) {
  const target=p2?.current_payable;
  if(!target) return "P2 尚未选中可支付的建筑或商店等目标，请先靠近目标。";
  const label=target.name||target.target_id||"当前目标";
  if(!target.currency) return `${label} 的币种尚未读取，暂不能付款。`;
  if(!Number.isSafeInteger(target.price)) return `${label} 的价格尚未读取，暂不能付款。`;
  if(target.price===0) return `${label} 当前价格为 0，无需投币。`;
  if(target.price<1) return `${label} 的价格无效。`;
  if(target.can_pay!==true) return target.can_pay===false ? `${label} 当前不可支付，暂不能向此对象投币。` : `${label} 的可支付状态尚未读取，暂不能付款。`;
  if(p2.transaction_pending!==false) return p2.transaction_pending===true ? "P2 上一笔交易尚未结算，暂不能再次付款。" : "P2 的交易状态尚未读取，暂不能付款。";
  const balance=target.currency==="coins" ? p2.coins : p2.currencies?.[target.currency];
  if(!Number.isSafeInteger(balance)) return "P2 对应货币的钱包余额尚未读取，暂不能付款。";
  if(balance<target.price) return `P2 钱包只有 ${balance} ${target.currency}，不足以完整支付 ${target.price}。`;
  if(!state?.capabilities?.includes("pay")) return "当前桥接未提供完整付款能力，请更新游戏模组后再核对。";
  return "";
}

function renderPayment(value, ready) {
  const control=value?.control||{},state=value?.bridge?.state;
  const p2=state?.players?.find(item=>item.player_id===1),target=p2?.current_payable;
  if(ready===undefined){const age=state ? (Date.now()-Date.parse(state.captured_at))/1000 : Infinity;ready=backendConnected&&value?.bridge?.connected&&age<=2.5&&age>=-2&&state.ready&&state.coop&&state.controlled_player_id===1&&!!p2;}
  const draft=Number($("coin-budget").value),validDraft=$("coin-budget").value.trim()!==""&&Number.isSafeInteger(draft)&&draft>=0;
  const draftText=validDraft ? `${draft} 枚金币` : "有效的非负整数";
  const walletDraft=$("wallet-spending").checked;
  const running=control.running===true,autonomous=running&&control.mode==="autonomous";
  const wallet=autonomous&&control.spending_mode==="wallet";
  const remaining=Number.isSafeInteger(control.coin_budget_remaining) ? control.coin_budget_remaining : null;
  let mode="尚未授权",budget;
  if(wallet){mode="AI自主支配";budget="本次不设累计投币额度。AI根据P2实际余额自主决定购买和留钱，每笔按游戏实时价格执行；当前开关仅修改下一次启动的草稿。";}
  else if(autonomous){mode=remaining===null ? "运行额度未读取" : remaining===0 ? "本次额度已为 0" : `本次剩余 ${remaining} 枚`;budget=`自主付款${remaining===null ? "额度尚未读取，暂不能付款" : `只可使用后台确认的剩余 ${remaining} 枚金币`}。预算框为下一次启动的草稿，修改不会增加本次额度。`;}
  else if(running&&control.mode==="follow"){mode="跟随不投币";budget=`当前仅跟随你，不会给建筑或商店投币。下次自主预算草稿为 ${draftText}，点击「开始自主陪玩」后才会授权。`;}
  else if(running){mode="自主付款未运行";budget=`正在进行手动核对；下次自主预算草稿为 ${draftText}，尚未授权自主付款。`;}
  else if(walletDraft){mode="下次由AI支配";budget="自主陪玩尚未启动。金币支配草稿为AI自主决定，点击「开始自主陪玩」后生效；不设本次累计额度，使用P2实际钱包。";}
  else {budget=validDraft ? `自主陪玩尚未启动。预算草稿为 ${draftText}，尚未授权；设置明确目标并点击「开始自主陪玩」后，才会建立本次总额度。` : "自主陪玩尚未启动。请将预算草稿设为非负整数；修改输入框不会授权付款。";}
  $("payment-mode").textContent=mode;$("payment-budget-note").textContent=budget;
  let issue=paymentIssue(state,p2);
  if(!backendConnected) issue="后台未连接，暂不能读取付款条件。";
  else if(!ready) issue="P2 的实时可操作状态未就绪，暂不能付款，请检查游戏连接与合作存档。";
  $("payment-target-note").textContent=issue||`${target.name||target.target_id} 当前可完整支付 ${target.price} ${target.currency}。${wallet ? "AI自行决定是否购买。" : autonomous&&remaining!==null&&remaining<target.price ? `本次剩余额度 ${remaining} 枚不足，AI不会支付。` : "按当前金币支配设置选择付款。"}`;
  $("payment-target-note").classList.toggle("warn",!!issue||autonomous&&!wallet&&remaining!==null&&remaining<target?.price);
  const goal=autonomous ? control.goal||"" : $("goal").value;
  const mentionsPayment=/投币|付款|支付|购买|建造|建设|修建|建墙|建塔|升级|招募|雇佣|商店|建筑/.test(goal);
  const zero=autonomous ? !wallet&&remaining===0 : !walletDraft&&validDraft&&draft===0;
  $("payment-goal-note").hidden=!(mentionsPayment&&zero);
  $("payment-goal-note").textContent=autonomous ? "当前任务包含付款，但本次剩余额度为 0；AI可以移动和观察，不会继续投币。重新设置额度后，需要先停止并重新开始自主陪玩。" : "目标包含付款，但预算草稿为 0。启动后 AI可以移动和观察，不会给建筑或商店投币；要付款，请设置非零总额度再开始自主陪玩。";
  $("payment-goal-note").classList.toggle("warn",mentionsPayment&&zero);
  const unit=({coins:"枚金币",gems:"颗宝石"})[target?.currency]||target?.currency||"币种未读取";
  $("payable").textContent=`当前支付对象：${target?.name||target?.target_id||"没有可支付对象"}${target ? ` · ${Number.isSafeInteger(target.price) ? target.price : "价格未知"} ${unit}` : ""}`;
  $("pay-coin").textContent=target&&Number.isSafeInteger(target.price)&&target.price>0 ? `完整支付 ${target.price} ${unit}` : "向当前对象完整付款";
  let manualIssue=issue;
  if(control.running) manualIssue="AI 正在运行，手动付款已锁定。要手动核对，请先点击「立即停止 / 接管」。";
  if(busy) manualIssue="上一项操作正在处理，手动付款暂不可用。";
  if(control.stopping) manualIssue="正在停止并确认输入释放，手动付款暂不可用。";
  if(control.accepting_control===false||shuttingDown) manualIssue="后台正在退出，付款已停用。";
  if(!backendConnected) manualIssue="后台连接中断，手动付款暂不可用。";
  $("pay-coin").disabled=!!manualIssue;
  $("manual-pay-note").textContent=manualIssue||`可手动完整支付 ${target.price} ${unit}；此按钮仅授权当前这一笔交易，不受自主预算草稿影响。`;
  $("manual-pay-note").classList.toggle("warn",!!manualIssue);
  $("decision-budget").disabled=shuttingDown||$("continuous-play").checked;
  $("coin-budget").disabled=shuttingDown||walletDraft;
  $("wallet-spending").disabled=shuttingDown;
  $("continuous-note").textContent=$("continuous-play").checked ? "持续模式会不断请求模型并按服务商规则计费。动作失败后重新观察选择，不要求人工核验；停止或游戏失联时结束控制。" : "本次自主游玩最多使用设置的模型决策次数，不要求动作后的人工核验。";
}

function lockControls(all=false) {
  for(const id of ["follow","autonomous","move-left","move-right","pay-coin","open-coop","save-model","test-model","model-deepseek","save-directory","detect-game","launch-game","save-execution","test-execution","execution-provider"]) $(id).disabled=true;
  executionFields(true);
  if(all) for(const id of ["stop","refresh","shutdown"]) $(id).disabled=true;
  $("manual-pay-note").textContent=all ? "后台正在退出，付款已停用。" : "后台连接中断，手动付款暂不可用。";
  $("payment-target-note").textContent=all ? "后台正在退出，付款已停用。" : "后台连接中断，付款状态暂不可用。";
  $("manual-pay-note").classList.toggle("warn",true);$("payment-target-note").classList.toggle("warn",true);
  if(all){$("continuous-play").disabled=true;$("decision-budget").disabled=true;$("play-style").disabled=true;$("wallet-spending").disabled=true;$("coin-budget").disabled=true;}
  renderPlayStyle(latest);
  dialogueEpoch++;
  $("game-bubble-status").textContent=all ? "后台正在退出，游戏内气泡已停止更新。" : "后台连接中断，游戏内气泡状态暂未读取。";
}

async function refreshDialogueStatus() {
  if(dialogueRefreshing||!session||shuttingDown||!backendConnected) return;
  dialogueRefreshing=true;
  const epoch=dialogueEpoch;
  try{
    const value=await api("/api/dialogue/status");
    if(shuttingDown||!backendConnected||epoch!==dialogueEpoch) return;
    const error=typeof value.last_error==="string"&&value.last_error ? value.last_error : null;
    $("game-bubble-status").textContent=value.available===true ? error ? `游戏内气泡转发未确认：${error}` : "游戏内气泡可用，助手回复会以文字气泡显示。" : typeof value.message==="string" ? value.message : "当前模组尚不支持游戏内气泡，请关闭游戏后更新模组再使用。";
  }catch{
    if(!shuttingDown&&backendConnected&&epoch===dialogueEpoch) $("game-bubble-status").textContent="游戏内气泡状态暂未读取；请核对后台与模组版本。";
  }finally{dialogueRefreshing=false;}
}

function render(value) {
  if(shuttingDown){lockControls(true);return;}
  latest = value;
  renderUsage(value.model?.usage);
  const bridge=value.bridge, state=bridge.state, control=value.control;
  const p1=state?.players.find((item)=>item.player_id===0), p2=state?.players.find((item)=>item.player_id===1);
  const age=state ? (Date.now()-Date.parse(state.captured_at))/1000 : Infinity;
  const fresh=Number.isFinite(age)&&age<=2.5&&age>=-2;
  const ready=backendConnected&&bridge.connected&&fresh&&state.ready&&state.coop&&state.controlled_player_id===1&&!!p2;
  $("connection").textContent = ready ? "P2 已就绪" : state?.ui_ready ? "已连接 · 地图操作中" : control.phase==="transitioning" ? "已连接 · 跨岛加载中" : bridge.connected ? "已连接 · 等待 P2" : "桥接未连接";
  $("connection").className = `badge ${ready ? "good" : "warn"}`;
  $("mode").textContent=control.running&&control.mode==="autonomous" ? control.play_style==="independent" ? "P2自由游玩中" : "协作陪玩中" : names[control.mode]||control.mode;
  $("mode").classList.toggle("good",control.running);
  player("p1",p1); player("p2",p2);
  const world=state?.world, enemies=world?.nearby_enemies, targets=world?.nearby_payables;
  const campaign=world?.campaign;
  if($("campaign-progress")) $("campaign-progress").textContent=campaign ? `${campaign.theme||"当前战役"} · 岛屿 ${campaign.land??"未读取"} · 已完成 ${campaign.completed_islands??"未读取"} / ${campaign.max_islands??"未读取"} · ${campaign.completed===true ? "战役已完成" : campaign.lost===true ? "战役失败" : "目标：推进通关"}` : "等待当前战役、岛屿与任务进度。";
  if($("interface-world")) $("interface-world").textContent=world ? JSON.stringify({wallet:p2?.currencies,...world},null,2) : "等待桥接状态";
  $("world-phase").textContent=typeof world?.is_night==="boolean" ? world.is_night ? "夜晚" : "白天" : "未读取";
  $("world-day").textContent=typeof world?.day==="number" ? number(world.day) : "未读取";
  $("world-enemies").textContent=Array.isArray(enemies) ? `${enemies.length} 条记录` : "未读取";
  const distances=Array.isArray(enemies)&&typeof p2?.x==="number" ? enemies.filter(item=>typeof item?.x==="number"&&Number.isFinite(item.x)).map(item=>Math.abs(item.x-p2.x)) : [];
  $("world-distance").textContent=distances.length ? number(Math.min(...distances)) : "未读取";
  $("world-payables").textContent=Array.isArray(targets) ? targets.slice(0,3).map(item=>`${item?.name||item?.target_id||"未命名对象"} · ${typeof item?.price==="number" ? number(item.price,0) : "价格未读取"} ${({coins:"金币",gems:"宝石"})[item?.currency]||item?.currency||"币种未读取"}`).join(" / ")||"0 条对象记录" : "未读取";
  $("p2-caption").textContent=ready ? "AI 操作只作用于 P2" : "等待可操作状态";
  $("movement-detail").textContent=state?.capabilities.includes("extended_world") ? "已接入目的地移动与全场状态，无 5 秒移动或 20 枚付款上限；坐骑疲劳由游戏处理。" : "旧桥接提供分段移动；更新至 0.4.0 模组可使用全场状态、技能、多币种付款和地图操作。";
  $("remaining-coins").textContent=control.spending_mode==="wallet" ? "AI自主支配" : control.coin_budget_remaining;
  $("remaining-decisions").textContent=control.continuous===true ? "持续" : control.decisions_remaining;
  const phases={idle:"已停止",observing:"读取局势",deciding:"等待模型决策",moving:"移动中",paying:"付款中",recovering:"恢复输入",waiting:"等待后重选",interacting:"原生交互中",transitioning:"等待跨岛加载"};
  $("decision-phase").textContent=phases[control.phase]||(control.running ? "游玩中" : "已停止");
  $("decision-phase-detail").textContent=control.running ? `已请求 ${control.decisions_made||0} 次决策 · 当前阶段 ${typeof control.phase_seconds==="number" ? Math.floor(control.phase_seconds) : 0} 秒。${control.phase==="deciding" ? "等待模型回复期间输入会停下；格式错误或临时超时会重新决策。" : control.phase==="paying" ? "正在等待游戏原生交易结束；失败后暂避该对象，选择其他行动。" : "动作反馈和实时观测用于下一轮选择。"}` : "启动后显示当前决策阶段；本轮等待不会结束持续自主游玩。";
  $("last-action").textContent=control.last_result ? names[control.last_result.operation]||"停止" : "尚未执行";
  $("last-result").textContent=control.last_result ? `${names[control.last_result.status]||control.last_result.status} · ${control.last_result.message}` : "尚未执行动作";
  $("game-version").textContent=state ? `游戏 ${state.game_version} · 桥接 ${state.bridge_version}` : "未读取游戏版本";
  $("scene").textContent=state?.scene||"—";
  $("capabilities").textContent=state?.capabilities.map((item)=>names[item]||item).join(" / ")||"尚未提供";
  $("input-state").textContent=state ? state.input_released ? "AI 输入已释放" : "AI 输入执行中" : "—";
  $("bridge-detail").textContent=control.error||control.release_error||bridge.error||(state&&!fresh ? "游戏状态已过期，等待新帧后才能操作。" : ready ? "桥接已提供当前 P2 身份与控制能力。实机效果仍需你核对。" : state?.reason||"请进入本地合作存档并检查模组诊断。");
  $("diagnostics").replaceChildren(...(state?.diagnostics||[]).map((item)=>{ const line=document.createElement("li");line.textContent=item;return line; }));
  const unavailable=!backendConnected||control.accepting_control===false||control.stopping;
  const locked=busy||control.running||unavailable;
  const canMove=ready&&p2?.x!=null&&p2.transaction_pending===false&&state.capabilities.includes("move");
  $("follow").disabled=locked||!canMove||!p1||p1.x==null;
  $("autonomous").disabled=locked||!canMove||!(value.model.execution_configured??value.model.configured)||executionDirty;
  $("move-left").disabled=$("move-right").disabled=locked||!canMove;
  renderPayment(value,ready);
  renderPlayStyle(value);
  $("continuous-play").disabled=false;
  $("play-style").disabled=false;
  $("save-model").disabled=$("test-model").disabled=$("model-deepseek").disabled=busy||control.running||unavailable;
  for(const id of ["save-directory","detect-game","launch-game"]) $(id).disabled=busy||control.running||unavailable;
  $("open-coop").disabled=busy||!bridge.connected||!!p2||unavailable;
  $("coop-result").textContent=state?.coop_request_result||"合作提示会暂停 AI；仍需在游戏中完成官方确认与外观选择。";
  $("stop").disabled=false;
  applyModel(value.model);
  applyExecution(value.model,busy||control.running||unavailable);
  notifyFrontend();
}

async function refresh() {
  if (refreshing||!session||shuttingDown) return;
  refreshing=true;
  try {const value=await api("/api/status");if(!shuttingDown){backendConnected=true;render(value);void refreshDialogueStatus();} }
  catch(error) {if(!shuttingDown){backendConnected=false;latest=null;notice(error.message);$("connection").textContent="后台连接中断";lockControls();notifyFrontend("disconnected");} }
  finally { refreshing=false; }
}

async function operate(operation, message) {
  if (busy||shuttingDown||!backendConnected) return;
  busy=true;if(latest)render(latest);notice("");
  try { const value=await operation();notice(value.message||message,!["failed","unknown","unverified"].includes(value.status)); }
  catch(error) { notice(error.message); }
  finally { busy=false;await refresh(); }
}

function start(mode) {
  if(mode==="autonomous"&&executionDirty)throw new Error("请先保存执行配置，再开始自主游玩。");
  const continuous=mode==="autonomous"&&$("continuous-play").checked;
  const count=Number($("decision-budget").value);
  const decisionBudget=(continuous||mode==="follow")&&(!Number.isInteger(count)||count<1||count>100) ? 30 : count;
  const wallet=mode==="autonomous"&&$("wallet-spending").checked;
  return api("/api/control/start",{method:"POST",body:JSON.stringify({mode,goal:$("goal").value.trim(),coin_budget:mode==="follow"||wallet ? 0 : Number($("coin-budget").value),decision_budget:decisionBudget,decision_interval_seconds:1,follow_distance:Number($("follow-distance").value),allow_sprint:$("allow-sprint").checked,...(mode==="autonomous" ? {continuous,play_style:draftPlayStyle(),spending_mode:wallet ? "wallet" : "budgeted"} : {})})});
}

$("refresh").addEventListener("click",refresh);
$("follow").addEventListener("click",()=>operate(()=>start("follow"),"跟随已开始，无需调用模型。"));
$("autonomous").addEventListener("click",()=>operate(()=>start("autonomous"),"自主陪玩已开始，可随时停止或按 F8 接管。"));
async function emergencyStop(){
  if(shuttingDown||!session)return;
  notifyFrontend("control-stop");
  // The stop request deliberately bypasses the UI busy lock and any pending model/manual call.
  try{const value=await api("/api/control/stop",{method:"POST"});notice(value.release_error||"已停止后续决策并要求游戏释放 AI 输入。",!value.release_error);}
  catch(error){notice(`${error.message}；请在游戏中按 F8 接管。`);}
  await refresh();
}
$("stop").addEventListener("click",emergencyStop);
for(const [id,direction] of [["move-left","left"],["move-right","right"]]) $(id).addEventListener("click",()=>operate(()=>api("/api/control/manual",{method:"POST",body:JSON.stringify({operation:"move",direction,duration_ms:500,sprint:$("allow-sprint").checked})}),"移动输入已结束。"));
$("pay-coin").addEventListener("click",()=>{if($("pay-coin").disabled)return;const target=latest?.bridge.state?.players.find(item=>item.player_id===1)?.current_payable;const body={operation:"pay",target_id:target?.target_id,max_coins:target?.price,currency:target?.currency};operate(()=>api("/api/control/manual",{method:"POST",body:JSON.stringify(body)}),"付款输入已结束，请观察游戏结果。");});
for(const id of ["goal","coin-budget"]) $(id).addEventListener("input",()=>{renderPayment(latest);});
$("wallet-spending").addEventListener("change",()=>{renderPayment(latest);});
$("continuous-play").addEventListener("change",()=>{renderPayment(latest);renderPlayStyle(latest);});
$("play-style").addEventListener("change",()=>{
  const next=defaultGoals[draftPlayStyle()];
  if($("goal").value===lastDefaultGoal) $("goal").value=next;
  lastDefaultGoal=next;renderPlayStyle(latest);renderPayment(latest);
});
for(const id of ["endpoint","model","api-key","clear-key"]) $(id).addEventListener("input",()=>{modelDirty=true;});
$("model-deepseek").addEventListener("click",()=>{if(busy||shuttingDown||!backendConnected||latest?.control.running)return;$("endpoint").value="https://api.deepseek.com";$("model").value="deepseek-flash";modelDirty=true;notice("已填写 DeepSeek 预设，请输入密钥后保存配置。",true);});
$("model-form").addEventListener("submit",event=>{
  event.preventDefault();
  const key=$("clear-key").checked ? "" : $("api-key").value||null;
  const body={endpoint:$("endpoint").value.trim(),model:$("model").value.trim(),api_key:key};
  operate(async()=>{const value=await api("/api/model",{method:"PUT",body:JSON.stringify(body)});$("api-key").value="";$("clear-key").checked=false;modelDirty=false;applyModel(value);return value;},"模型配置已保存。");
});
$("test-model").addEventListener("click",()=>operate(()=>api("/api/model/test",{method:"POST"}),"模型连接正常。"));

for(const id of ["execution-provider","execution-endpoint","execution-model","execution-context","execution-timeout"]) $(id).addEventListener("input",()=>{executionDirty=true;if(latest)render(latest);});
$("execution-installed").addEventListener("change",()=>{if($("execution-installed").value){$("execution-model").value=$("execution-installed").value;executionDirty=true;if(latest)render(latest);}});
$("execution-models-refresh").addEventListener("click",()=>{if($("execution-models-refresh").disabled)return;return operate(async()=>{
  const value=await api("/api/execution/models",{method:"POST",body:JSON.stringify({endpoint:$("execution-endpoint").value.trim()})});
  const placeholder=document.createElement("option");placeholder.value="";placeholder.textContent="选择已安装模型";
  const options=(value.models||[]).map(item=>{const option=document.createElement("option");option.value=item.name;option.textContent=`${item.name}${item.parameter_size ? " · "+item.parameter_size : ""}${item.quantization ? " · "+item.quantization : ""}`;return option;});
  $("execution-installed").replaceChildren(placeholder,...options);
  $("execution-installed").value=(value.models||[]).some(item=>item.name===$("execution-model").value) ? $("execution-model").value : "";
  $("execution-models-note").textContent=value.models?.length ? `已找到 ${value.models.length} 个模型，请选择并保存。` : value.message;
  return value;
},"已读取 Ollama 模型列表。");});
$("execution-form").addEventListener("submit",event=>{event.preventDefault();if($("save-execution").disabled)return;
  const body={provider:$("execution-provider").value,endpoint:$("execution-endpoint").value.trim(),model:$("execution-model").value.trim(),context_tokens:Number($("execution-context").value),timeout_seconds:Number($("execution-timeout").value)};
  return operate(async()=>{const value=await api("/api/execution",{method:"PUT",body:JSON.stringify(body)});executionDirty=false;applyExecution(value,false);return value;},"执行配置已保存，下次自主游玩使用所选模型。");
});
$("test-execution").addEventListener("click",()=>{if($("test-execution").disabled)return;return operate(()=>api("/api/execution/test",{method:"POST"}),"执行模型连接正常。");});
$("detect-game").addEventListener("click",()=>operate(async()=>{
  const value=await api("/api/installation/detect",{method:"POST"});
  const candidates=value.candidates;
  $("game-candidates").replaceChildren(...candidates.map(item=>{const option=document.createElement("option");option.value=item.path;option.textContent=`${item.path}${item.bridge_installed ? " · 已准备桥接" : " · 尚无桥接"}`;return option;}));
  $("game-candidates").hidden=candidates.length<2;
  if(candidates.length) {$("game-directory").value=candidates[0].path;$("game-candidates").value=candidates[0].path;}
  return {message:candidates.length ? `发现 ${candidates.length} 个游戏目录，请核对后保存。` : "没有在 Steam 库中找到游戏，请手动填写安装目录。"};
},"目录检测完成。"));
$("game-candidates").addEventListener("change",()=>{$("game-directory").value=$("game-candidates").value;});
$("save-directory").addEventListener("click",()=>operate(async()=>{
  const value=await api("/api/installation",{method:"PUT",body:JSON.stringify({path:$("game-directory").value.trim()})});
  $("game-directory").value=value.game_directory;
  $("installation-note").textContent=value.bridge_installed ? "已保存目录并找到私有桥接配置，启动游戏后重新检测连接。" : "已保存游戏目录，尚需运行模组安装脚本准备桥接。";
  return value;
},"目录已保存。"));
$("launch-game").addEventListener("click",()=>operate(()=>api("/api/game/launch",{method:"POST"}),"已请求启动游戏。"));
$("open-coop").addEventListener("click",()=>operate(()=>api("/api/game/coop",{method:"POST"}),"已请求打开游戏合作提示。"));
$("shutdown").addEventListener("click",async()=>{if(shuttingDown)return;shuttingDown=true;lockControls(true);notifyFrontend("shutdown");try{const value=await api("/api/shutdown",{method:"POST"});notice(value.message,true);backendConnected=false;latest=null;session="";}catch(error){notice(error.message);if(error.status){shuttingDown=false;$("refresh").disabled=$("shutdown").disabled=false;await refresh();}else{$("connection").textContent="后台已断开，请重新启动后刷新页面。";}}});

(async()=>{
  try{const response=await fetch("/api/session",{cache:"no-store"});if(!response.ok)throw new Error("页面会话未能建立");session=(await response.json()).token;const installation=await api("/api/installation");$("game-directory").value=installation.game_directory||"";if(installation.game_directory)$("installation-note").textContent=installation.bridge_installed ? "游戏目录与桥接已准备，进入本地合作存档后核对 P2 状态。" : "已选择游戏目录，尚需运行模组安装脚本。";await refresh();setInterval(refresh,1000);}
  catch(error){notice(error.message);}
})();
