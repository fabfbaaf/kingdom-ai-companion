"use strict";

(() => {
  const shell=globalThis.KingdomCompanion;
  if(!shell) return;
  const el=id=>document.getElementById(id);
  let app=shell.getState(), voiceBusy=false, polling=false, speechBusy=false, noticesBusy=false;
  let viewEpoch=0, voiceEpoch=0, cursor=0, historySignature="", lastGoal=null, voice=null;
  let speech=null, notices=null, speechEpoch=0, noticesEpoch=0, draftRevision=0;
  let queueSupported=false, serverBusy=false, serverQueued=0, requestSequence=0, lastHistoryId=0, externalBusy=false;
  let speechSignature="", lastPartial="", latestReply=0;
  let deviceBusy=false, deviceSignature="", deviceLoaded=false, devices=[];
  let voiceQueued=0, voiceDialogueError="";
  const pending=new Map(), maxQueued=3;
  const labels={unavailable:"语音组件暂不可用",needs_model:"先准备中文语音模型",downloading:"正在准备语音模型",ready:"可以开启监听",listening:"正在听你说话",error:"语音需要检查"};
  const online=()=>app.sessionReady&&app.backendConnected&&!app.shuttingDown&&app.latest?.control.accepting_control!==false;
  const quickPhrases=new Set(["停","停下","停止","停一下","停下来","停一停","停住","暂停","暂停一下","停在这里","停在这儿","别动","不要动","别走","别再走","停下先别动","停下来让我看看","停下听我说","别动等一下","停止陪玩","停止行动","停止跟随","别跟","接管","stop","跟着我","跟随我","跟我走","跟上我","跟随","跟着我走","跟我一起走","继续跟着我","继续跟随我","跟紧我","陪我走","follow","followme"]);
  function quick(text){
    if(/["'“”‘’「」『』《》]/.test(text)) return false;
    const compact=text.replace(/[\s，。！？、；,.!?;]/g,"").toLowerCase().replace(/^(?:(?:请|麻烦|你|先|现在|马上|立刻|立即|赶紧|帮我|给我|陪玩|ai)){0,4}/,"").replace(/(?:吧|啊|呀|了|好吗|好不好)$/,"");
    if(quickPhrases.has(compact)) return true;
    if(/如果|假如|要是|比如|举例|有人说|他说|她说|[？?]|怎么样|好不好|可以吗|要不要/.test(text)) return false;
    return /^(?:我们|你|帮我|请|现在|先|接下来|还是|优先){0,4}(?:发展经济|搞经济|发展营地|赚金币|攒钱)(?:吧|啊)?$/.test(compact)||/^(?:你|帮我|请|现在|先|今晚|今天|接下来){0,4}(?:守住?|保护|防守)(?:左边|右边|营地|左侧|右侧)(?:吧|啊)?$/.test(compact)||/^(?:金币|钱|宝石)(?:先|都|暂时)?(?:留着|留给|留|保留)(?:修船|造船|出航|修墙|升级城堡)(?:用|吧)?$/.test(compact)||/^目标[:：].+/.test(compact);
  }
  const ordinaryPending=()=>Array.from(pending.values()).filter(item=>!item.quick).length;
  const usableVoice=item=>typeof item?.name==="string"&&item.enabled!==false&&typeof item.culture==="string"&&/^zh(?:-|$)/i.test(item.culture);
  const load=()=>Math.max(ordinaryPending()+(externalBusy ? 1 : 0),(serverBusy ? 1 : 0)+serverQueued);
  const queueFull=()=>load()>=maxQueued+1;

  function queueStatus(){
    const busy=pending.size>0||serverBusy||serverQueued>0;
    const queued=Math.min(maxQueued,Math.max(serverQueued,ordinaryPending()-(externalBusy ? 0 : 1),0));
    el("chat-busy").hidden=!busy;
    el("chat-busy").textContent=queueSupported ? "正在回复 · 排队 "+queued+" / "+maxQueued : "正在回复…";
    el("chat-queue").textContent=(busy ? queueSupported ? "正在回复 · 排队 "+queued+" / "+maxQueued : "正在回复" : "对话空闲")+(voiceQueued ? ` · 语音等待 ${voiceQueued} 句` : "");
  }

  function chatStatus(value){
    if(typeof value.busy==="boolean"&&Number.isSafeInteger(value.queued)){
      queueSupported=true;serverBusy=value.busy;serverQueued=Math.max(0,Math.min(maxQueued,value.queued));
      if(!serverBusy&&!serverQueued) externalBusy=false;
    }
    const context=value.context;
    voiceQueued=Number.isInteger(value.voice_queued) ? Math.max(0,value.voice_queued) : 0;
    voiceDialogueError=typeof value.voice_error==="string" ? value.voice_error : "";
    if(context&&typeof context==="object"){
      const task=typeof context.control_goal==="string"&&context.control_goal.trim() ? context.control_goal : context.mode==="follow" ? "跟随你" : "等待你安排";
      el("companion-task").textContent=task;
      const action=context.last_action;
      const names={move:"移动",pay:"付款",pay_coin:"付款",stop:"停止"};
      el("companion-action").textContent=typeof action==="string" ? action : action ? action.message||names[action.operation]||"等待读取反馈" : "尚未执行";
      el("companion-context-detail").textContent=context.stale ? "游戏观测已过期，等待新的真实状态。" : typeof context.message==="string"&&context.message ? context.message : "聊天与游玩可同时进行，停止指令随时可用。";
      strategy(context);
    }
    controls();
  }

  function controls(){
    const usable=online();
    el("chat-send").disabled=!usable||(queueSupported ? queueFull()&&!quick(el("chat-text").value) : pending.size>0);
    el("chat-text").disabled=!usable||(!queueSupported&&pending.size>0);
    queueStatus();
    el("chat-stop-game").disabled=!app.sessionReady||app.shuttingDown;
    el("voice-prepare").disabled=!usable||voiceBusy||voice?.stopping||voice?.model_ready===true||voice?.state==="downloading"||voice?.state==="listening"||voice?.state==="unavailable";
    el("voice-start").disabled=!usable||voiceBusy||voice?.stopping||voice?.model_ready!==true||voice?.state==="listening"||voice?.state==="downloading"||voice?.state==="unavailable";
    el("voice-input").disabled=!usable||voiceBusy||deviceBusy||voice?.stopping||voice?.state==="listening";
    el("voice-refresh").disabled=!usable||deviceBusy||voice?.stopping||voice?.state==="listening";
    // Stop is deliberately independent of chat/voice busy and backend polling failures.
    el("voice-stop").disabled=!app.sessionReady||app.shuttingDown;
    for(const button of document.querySelectorAll("[data-chat-text]")) button.disabled=!usable||(!queueSupported&&pending.size>0);
    const available=Array.isArray(speech?.available_voices)&&speech.available_voices.some(usableVoice);
    el("speech-enabled").disabled=!usable||speechBusy||!speech||(!available&&!speech.enabled);
    el("speech-voice").disabled=!usable||speechBusy||!available;
    el("speech-headphones").disabled=!usable||speechBusy||!speech;
    el("speech-stop").disabled=!app.sessionReady||app.shuttingDown;
    el("notices-enabled").disabled=!usable||noticesBusy||!notices;
  }

  function speechStatus(value){
    speech=value;
    const voices=Array.isArray(value.available_voices) ? value.available_voices.filter(item=>typeof item?.name==="string"&&item.name.trim()).slice(0,100) : [];
    const signature=JSON.stringify(voices);
    if(signature!==speechSignature){
      speechSignature=signature;
      const options=voices.map(item=>{const option=document.createElement("option");option.value=item.name;option.textContent=item.name+(typeof item.culture==="string" ? " · "+item.culture : "")+(!usableVoice(item) ? " · 不可用中文播报" : "");option.disabled=!usableVoice(item);return option;});
      if(!options.length){const option=document.createElement("option");option.value="";option.textContent="未找到 Windows 中文音色";options.push(option);}
      el("speech-voice").replaceChildren(...options);
    }
    el("speech-enabled").checked=value.enabled===true;
    el("speech-headphones").checked=value.headphones===true;
    const selected=typeof value.voice_name==="string" ? value.voice_name : voices.find(usableVoice)?.name||"";
    el("speech-voice").value=selected;
    const state=value.error ? "需检查" : value.speaking ? "正在播报" : value.enabled ? voice?.state==="listening"&&!value.headphones ? "监听中 · 播报暂停" : "已开启" : "已关闭";
    el("speech-status").textContent=state;
    el("speech-status").classList.toggle("good",value.enabled===true&&!value.error);
    el("speech-status").classList.toggle("warn",!!value.error);
    el("speech-detail").textContent=typeof value.message==="string"&&value.message ? value.message : typeof value.error==="string"&&value.error ? value.error : !voices.some(usableVoice) ? "未安装可用的 Windows 中文音色，请在 Windows 语音设置中添加中文音色。" : value.enabled ? "使用本机 Windows 音色播报回复。" : "播报已关闭。开启后使用所选本机音色。";
    controls();
  }

  function noticesStatus(value){
    notices=value;el("notices-enabled").checked=value.enabled===true;
    el("notices-detail").textContent=typeof value.message==="string"&&value.message ? value.message : value.enabled ? "主动提醒已开启，仅根据真实观测简短提醒。" : "默认关闭。开启后，仅在已观测的局面发生重要变化时简短提醒。";
    controls();
  }

  async function stopSpeech(automatic=false){
    if(!app.sessionReady||app.shuttingDown||automatic&&(!online()||!speech?.enabled&&!speech?.speaking)) return;
    const token=++speechEpoch;speechBusy=false;controls();
    try{
      const value=await shell.api("/api/speech/stop",{method:"POST"});
      if(token!==speechEpoch||app.shuttingDown) return;
      if(typeof value.enabled==="boolean") speechStatus(value);
      else {el("speech-status").textContent="已请求停止播报";el("speech-detail").textContent="已请求停止播报，游戏和监听继续运行。";}
    }catch(error){if(token===speechEpoch&&!app.shuttingDown)el("speech-detail").textContent=error.message;}
  }

  async function saveSpeech(){
    if(!online()||speechBusy||!speech) return;
    const token=++speechEpoch,epoch=viewEpoch;
    const body={enabled:el("speech-enabled").checked,voice_name:el("speech-voice").value||null,headphones:el("speech-headphones").checked};
    speechBusy=true;controls();
    try{
      const value=await shell.api("/api/speech/settings",{method:"PUT",body:JSON.stringify(body)});
      if(token!==speechEpoch||epoch!==viewEpoch||!online()) return;
      if(typeof value.enabled==="boolean") speechStatus(value);
    }catch(error){if(token===speechEpoch&&epoch===viewEpoch&&!app.shuttingDown){speechStatus(speech);el("speech-detail").textContent=error.message;}}
    finally{if(token===speechEpoch){speechBusy=false;controls();await poll();}}
  }

  async function saveNotices(){
    if(!online()||noticesBusy||!notices) return;
    const token=++noticesEpoch,epoch=viewEpoch;
    noticesBusy=true;controls();
    try{
      const value=await shell.api("/api/notices/settings",{method:"PUT",body:JSON.stringify({enabled:el("notices-enabled").checked})});
      if(token!==noticesEpoch||epoch!==viewEpoch||!online()) return;
      if(typeof value.enabled==="boolean") noticesStatus(value);
    }catch(error){if(token===noticesEpoch&&epoch===viewEpoch&&!app.shuttingDown){noticesStatus(notices);el("notices-detail").textContent=error.message;}}
    finally{if(token===noticesEpoch){noticesBusy=false;controls();await poll();}}
  }

  function voiceStatus(value){
    voice=value;
    const partial=typeof value.partial==="string" ? value.partial.trim() : "";
    if(value.state==="listening"&&partial&&partial!==lastPartial) void stopSpeech(true);
    lastPartial=partial;
    if(Array.isArray(value.devices)){
      devices=value.devices;
      const signature=JSON.stringify(devices);
      if(signature!==deviceSignature){
        deviceSignature=signature;
        const defaultOption=document.createElement("option");defaultOption.value="";
        defaultOption.textContent=devices.some(item=>item.default) ? "Windows 默认麦克风" : "Windows 默认麦克风（未找到默认输入）";
        el("voice-input").replaceChildren(defaultOption,...devices.filter(item=>Number.isInteger(item.index)).map(item=>{
          const option=document.createElement("option");option.value=String(item.index);
          option.textContent=`${item.name} · ${item.host_api||"音频设备"}`;return option;
        }));
      }
      el("voice-input").value=Number.isInteger(value.selected_device) ? String(value.selected_device) : "";
    }
    el("voice-label").textContent=value.stopping ? "正在关闭麦克风" : labels[value.state]||"语音状态未读取";
    el("voice-detail").textContent=typeof value.message==="string" ? value.message : "麦克风由你手动开启。";
    const listening=value.state==="listening";
    el("voice-status").textContent=value.stopping ? "正在关闭" : listening ? "监听中" : value.state==="downloading" ? "下载中" : value.state==="error" ? "需检查" : "未监听";
    el("voice-status").classList.toggle("good",listening&&!value.stopping);
    el("voice-status").classList.toggle("warn",value.state==="error"||value.state==="unavailable"||value.stopping);
    el("voice-panel")?.classList.toggle("listening",listening);
    const progress=typeof value.progress==="number"&&Number.isFinite(value.progress)
      ? Math.max(0,Math.min(100,value.progress<=1 ? value.progress*100 : value.progress)) : null;
    el("voice-prepare").textContent=value.state==="downloading"&&progress!==null
      ? `正在下载 · ${Math.round(progress)}%` : value.model_ready ? "中文语音模型已准备" : "准备中文语音模型 · 42 MB";
    const device=typeof value.device==="string" ? value.device : typeof value.device?.name==="string" ? value.device.name : null;
    el("voice-device").textContent=device ? `麦克风：${device} · 游戏在前台也可收音` : "在本机识别，游戏窗口在前台也可收音。";
    const level=typeof value.level==="number"&&Number.isFinite(value.level) ? Math.max(0,Math.min(1,value.level)) : 0;
    const db=level>0 ? Math.round(20*Math.log10(level)) : -60;
    el("voice-level").value=listening ? Math.max(0,Math.min(100,(db+60)/60*100)) : 0;
    el("voice-level-label").textContent=!listening ? "输入音量 · 未监听" : value.peak>=0.98 ? "输入过大 · 建议降低麦克风增益" : level>0 ? `输入音量 · ${db} dBFS` : "正在监听 · 暂未检测到声音";
    el("voice-diagnostic").textContent=value.error_code ? `诊断：${value.error_code} · ${value.message||""}` : value.selected_device==null&&value.device_error ? value.device_error : voiceDialogueError|| (value.overflows>0 ? `本次音频溢出 ${value.overflows} 次，已重置不完整句并继续监听。` : "");
    el("voice-partial").textContent=typeof value.partial==="string"&&value.partial.trim()
      ? value.partial : listening ? "正在聆听，停顿后会自动形成一句话…" : "等待你开启监听…";
    if(speech&&!speechBusy) el("speech-status").textContent=speech.error ? "需检查" : speech.speaking ? "正在播报" : speech.enabled ? listening&&!speech.headphones ? "监听中 · 播报暂停" : "已开启" : "已关闭";
    controls();
  }

  async function refreshDevices(){
    if(!online()||deviceBusy) return;
    const epoch=viewEpoch;deviceBusy=true;controls();
    try{const value=await shell.api("/api/voice/devices");if(epoch===viewEpoch&&online()){deviceLoaded=true;voiceStatus(value);}}
    catch(error){if(epoch===viewEpoch)el("voice-diagnostic").textContent=error.message;}
    finally{deviceBusy=false;controls();}
  }

  async function selectDevice(){
    if(!online()||deviceBusy||voice?.state==="listening"||voice?.stopping) return;
    const epoch=viewEpoch;deviceBusy=true;controls();
    try{const raw=el("voice-input").value;const value=await shell.api("/api/voice/device",{method:"PUT",body:JSON.stringify({device:raw==="" ? null : Number(raw)})});if(epoch===viewEpoch&&online())voiceStatus(value);}
    catch(error){if(epoch===viewEpoch){if(voice)voiceStatus(voice);el("voice-diagnostic").textContent=error.message;}}
    finally{deviceBusy=false;controls();}
  }

  function strategy(context){
    const plan=context.plan||{},memory=context.memory||{};
    const stages={explore:"探索",economy:"经济",defense:"防御",attack:"进攻",sail:"出航",complete:"战役完成"};
    const status={in_progress:"进行中",completed:"条件已达成",waiting_observation:"等待观测"};
    el("strategy-stage").textContent=`阶段计划 · ${stages[plan.stage]||"等待规划"}`;
    el("strategy-status").textContent=status[plan.status]||"未观测";
    el("strategy-objective").textContent=plan.objective||"等待真实游戏观测。";
    el("strategy-feedback").textContent=plan.feedback||plan.fallback||"";
    el("strategy-criteria").replaceChildren(...(Array.isArray(plan.criteria) ? plan.criteria : []).map(item=>{
      const line=document.createElement("p");line.className="hint";
      line.textContent=`${item.status==="completed" ? "✓" : item.status==="unknown" ? "?" : "○"} ${item.description} · 观测：${item.observed??"未知"}`;return line;
    }));
    const islands=Array.isArray(memory.islands) ? memory.islands : [];
    el("memory-summary").textContent=`战役记忆 · 已记录 ${islands.length} 个区域`;
    el("memory-detail").textContent=memory.error||(!memory.identity_verified ? "当前模组未提供有效存档标识，仅保留本次会话记忆。更新模组后可按战役恢复历史观测。" : "按战役保存在本机。历史地图与资源带观测时间，以当前游戏状态为准。")+(memory.observed_at ? ` 最近观测：${new Date(memory.observed_at).toLocaleString()}` : "");
    el("memory-islands").replaceChildren(...islands.map(item=>{
      const line=document.createElement("p");line.className="hint";
      line.textContent=`岛屿 ${item.land??"未知"} · ${item.zone==="cave" ? "洞穴" : "地表"} · 已知对象 ${item.known_objects??0} · P2 到达范围 ${Array.isArray(item.explored) ? item.explored.map(x=>Number(x).toFixed(1)).join(" ～ ") : "未知"}`;return line;
    }));
  }

  function history(value){
    if(!Array.isArray(value.messages)) return;
    const messages=value.messages.filter(item=>item&&["user","assistant"].includes(item.role)&&typeof item.content==="string").slice(-60);
    const lastId=messages.reduce((last,item)=>Number.isSafeInteger(item.id) ? Math.max(last,item.id) : last,0);
    if(lastId&&lastId<lastHistoryId) return;
    lastHistoryId=Math.max(lastHistoryId,lastId);
    const signature=JSON.stringify(messages);
    if(signature===historySignature) return;
    historySignature=signature;
    const list=el("chat-log");
    const nearBottom=list.scrollHeight-list.scrollTop-list.clientHeight<60;
    const lines=messages.map(item=>{
      const article=document.createElement("article");article.className=`chat-message ${item.role}`;
      const label=document.createElement("span");label.className="chat-author";
      label.textContent=item.role==="user" ? item.source==="voice" ? "你 · 语音" : "你" : item.source==="system" ? "本机提示" : "同行助手";
      const text=document.createElement("p");text.textContent=item.content;
      article.append(label,text);return article;
    });
    if(!lines.length){const empty=document.createElement("p");empty.className="chat-empty";empty.textContent="发条消息，或开启监听后说话。试试「跟着我」和「停止」。";lines.push(empty);}
    list.replaceChildren(...lines);
    if(nearBottom) list.scrollTop=list.scrollHeight;
  }

  function goal(value){
    if(typeof value.goal!=="string"||!value.goal.trim()) return;
    el("chat-goal").textContent=`当前目标：${value.goal}`;
    el("chat-goal").hidden=false;
    if(value.goal!==lastGoal&&document.activeElement!==el("goal")) el("goal").value=value.goal.slice(0,500);
    lastGoal=value.goal;
  }

  async function poll(){
    if(!online()||polling) return;
    if(!deviceLoaded&&!deviceBusy) void refreshDevices();
    polling=true;
    const epoch=viewEpoch,speechToken=speechEpoch,noticesToken=noticesEpoch;
    const outcomes=await Promise.allSettled([
      shell.api("/api/chat"),shell.api("/api/voice/status"),shell.api(`/api/voice/events?after=${cursor}`),
      shell.api("/api/speech/status"),shell.api("/api/notices/status"),
    ]);
    try{
      if(!online()||epoch!==viewEpoch) return;
      const [chat,state,events,speechState,noticeState]=outcomes;
      if(chat.status==="fulfilled"){history(chat.value);goal(chat.value);chatStatus(chat.value);}
      else el("chat-result").textContent=chat.reason?.message||"对话记录暂未读取";
      if(state.status==="fulfilled") voiceStatus(state.value);
      else {voice=null;el("voice-label").textContent="语音状态未读取";el("voice-detail").textContent=state.reason?.message||"请重新检查后台连接";controls();}
      if(events.status==="fulfilled"){
        const value=events.value;
        if(Number.isSafeInteger(value.cursor)&&value.cursor>=cursor) cursor=value.cursor;
        const finals=Array.isArray(value.events) ? value.events.filter(item=>typeof item?.text==="string"&&item.text.trim()) : [];
        if(finals.length){el("voice-final").textContent=`最近识别：${finals.at(-1).text}`;el("voice-final").hidden=false;}
      }
      if(speechToken===speechEpoch&&!speechBusy){
        if(speechState.status==="fulfilled") speechStatus(speechState.value);
        else {speech=null;el("speech-status").textContent="状态未读取";el("speech-detail").textContent=speechState.reason?.message||"播报状态暂不可用";}
      }
      if(noticesToken===noticesEpoch&&!noticesBusy){
        if(noticeState.status==="fulfilled") noticesStatus(noticeState.value);
        else {notices=null;el("notices-detail").textContent=noticeState.reason?.message||"提醒状态暂不可用";}
      }
      controls();
    }finally{polling=false;}
  }

  async function send(){
    if(!online()||!queueSupported&&pending.size>0) return;
    const draft=el("chat-text").value,text=draft.trim(),urgent=quick(text);
    if(!text){el("chat-result").textContent="先写一句话吧。";return;}
    if(text.length>1000){el("chat-result").textContent="消息最多 1000 字，请缩短一些。";return;}
    if(queueSupported&&!urgent&&queueFull()){el("chat-result").textContent="已有三条消息等待回复，请稍后再发。停止和跟随指令仍可发送。";return;}
    const epoch=viewEpoch,id=++requestSequence;
    if(!urgent&&!ordinaryPending()) externalBusy=serverBusy;
    pending.set(id,{quick:urgent,epoch});
    el("chat-text").value="";el("chat-count").textContent="0 / 1000";
    const clearedRevision=++draftRevision;
    el("chat-result").textContent="";controls();void stopSpeech(true);
    try{
      const value=await shell.api("/api/chat",{method:"POST",body:JSON.stringify({text})});
      if(!online()||epoch!==viewEpoch) return;
      if(Array.isArray(value.messages)) history(value);
      if(id>=latestReply){
        latestReply=id;
        el("chat-result").textContent=typeof value.control_result==="string" ? value.control_result : "回复已收到。";
        goal(value);chatStatus(value);
      }
      // Chat replies and voice finals only render text; all intent dispatch belongs to the backend.
    }catch(error){
      if(!app.shuttingDown&&epoch===viewEpoch){
        el("chat-result").textContent=error.message;
        if(draftRevision===clearedRevision&&!el("chat-text").value){el("chat-text").value=draft;el("chat-count").textContent=draft.length+" / 1000";draftRevision++;}
      }
    }finally{pending.delete(id);if(!ordinaryPending())externalBusy=false;controls();await poll();}
  }

  async function voiceAction(action){
    if(action!=="stop"&&(!online()||voiceBusy)) return;
    if(!app.sessionReady||app.shuttingDown) return;
    if(action==="start"&&(voice?.stopping||voice?.model_ready!==true||["listening","downloading","unavailable"].includes(voice?.state))) return;
    if(action==="prepare"&&(voice?.stopping||voice?.model_ready===true||["downloading","listening","unavailable"].includes(voice?.state))) return;
    const token=++voiceEpoch;
    if(action==="stop"){viewEpoch++;el("voice-partial").textContent="正在停止监听…";}
    voiceBusy=true;controls();
    try{
      const value=await shell.api(`/api/voice/${action}`,{method:"POST"});
      if(token!==voiceEpoch||app.shuttingDown) return;
      if(typeof value.state==="string") voiceStatus(value);
      else el("voice-detail").textContent=typeof value.message==="string" ? value.message : action==="stop" ? "已请求停止监听。" : action==="prepare" ? "语音模型正在后台准备；完成后再开启监听。" : "已请求开启监听。";
    }catch(error){if(token===voiceEpoch&&!app.shuttingDown)el("voice-detail").textContent=error.message;}
    finally{if(token===voiceEpoch){voiceBusy=false;controls();await poll();}}
  }

  el("chat-form").addEventListener("submit",event=>{event.preventDefault();return send();});
  el("chat-text").addEventListener("input",()=>{draftRevision++;el("chat-count").textContent=`${el("chat-text").value.length} / 1000`;controls();});
  el("chat-text").addEventListener("keydown",event=>{if(event.key==="Enter"&&!event.shiftKey&&!event.isComposing){event.preventDefault();void send();}});
  for(const button of document.querySelectorAll("[data-chat-text]")) button.addEventListener("click",()=>{if(!online()||!queueSupported&&pending.size>0)return;draftRevision++;el("chat-text").value=button.dataset.chatText;el("chat-count").textContent=`${el("chat-text").value.length} / 1000`;el("chat-text").focus();controls();});
  for(const action of ["prepare","start","stop"]) el(`voice-${action}`).addEventListener("click",()=>voiceAction(action));
  el("chat-stop-game").addEventListener("click",()=>shell.emergencyStop());
  for(const id of ["speech-enabled","speech-voice","speech-headphones"]) el(id).addEventListener("change",saveSpeech);
  el("speech-stop").addEventListener("click",()=>stopSpeech());
  el("voice-refresh").addEventListener("click",refreshDevices);
  el("voice-input").addEventListener("change",selectDevice);
  el("notices-enabled").addEventListener("change",saveNotices);
  shell.subscribe((state,event)=>{
    app=state;
    if(["control-stop","shutdown","disconnected"].includes(event)){
      viewEpoch++;pending.clear();serverBusy=false;serverQueued=0;externalBusy=false;speechEpoch++;noticesEpoch++;speechBusy=false;noticesBusy=false;
      if(event==="shutdown") voiceEpoch++;
      if(event==="disconnected"){lastHistoryId=0;historySignature="";}
    }
    controls();
    if(online()) void poll();
  });
  setInterval(()=>{void poll();},1000);
})();
