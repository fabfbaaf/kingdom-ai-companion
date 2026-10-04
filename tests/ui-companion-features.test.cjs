"use strict";

// Offline DOM/API boundary: no server, model, audio device or game is started.
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path");
const test=require("node:test"),vm=require("node:vm");
const directory=path.join(__dirname,"..","companion","static");
const markup=fs.readFileSync(path.join(directory,"index.html"),"utf8");
const source=fs.readFileSync(path.join(directory,"conversation.js"),"utf8");
const turn=()=>new Promise(resolve=>setImmediate(resolve));
function deferred(){let resolve,reject;const promise=new Promise((done,fail)=>{resolve=done;reject=fail;});return {promise,resolve,reject};}
const blankChat=()=>({messages:[],goal:null,busy:false,queued:0,context:{control_goal:"保护营地",mode:"autonomous",last_action:null,message:"聊天与游玩同时进行。",stale:false}});
const blankSpeech=()=>({enabled:false,speaking:false,headphones:false,voice_name:null,error:null,message:"播报默认关闭。",available_voices:[{name:"Fixture 中文音色",culture:"zh-CN",enabled:true}]});

class Element{
  constructor(tag="div"){this.tag=tag;this.value="";this.textContent="";this.disabled=false;this.hidden=false;this.checked=false;this.children=[];this.dataset={};this.listeners=new Map();this.scrollHeight=100;this.clientHeight=100;this.scrollTop=0;this.classes=new Set();this.classList={toggle:(name,on)=>on?this.classes.add(name):this.classes.delete(name)};}
  addEventListener(type,fn){const group=this.listeners.get(type)||[];group.push(fn);this.listeners.set(type,group);}
  replaceChildren(...items){this.children=items;}
  append(...items){this.children.push(...items);}
  focus(){}
  set innerHTML(_value){throw new Error("Companion content must be rendered as literal text");}
  async fire(type="click",event={}){await Promise.all((this.listeners.get(type)||[]).map(fn=>fn({preventDefault(){},...event})));}
}

async function fixture(initial={}){
  const elements=new Map(),suggestions=[],calls=[],plans=new Map(),intervals=[],subscribers=[];
  for(const match of markup.matchAll(/<([a-z]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)){
    const element=new Element(match[1]);element.disabled=/\bdisabled\b/.test(match[2]);element.hidden=/\bhidden\b/.test(match[2]);element.value=/\bvalue="([^"]*)"/.exec(match[2])?.[1]||"";elements.set(match[3],element);
  }
  for(const match of markup.matchAll(/data-chat-text="([^"]+)"/g)){const element=new Element("button");element.dataset.chatText=match[1];suggestions.push(element);}
  elements.get("goal").value="fixture draft goal";
  let app={sessionReady:true,backendConnected:true,shuttingDown:false,busy:true,latest:{control:{accepting_control:true,running:true,mode:"autonomous"}}};
  let chat=initial.chat||blankChat(),speech=initial.speech||blankSpeech(),voice=initial.voice||{state:"ready",model_ready:true,partial:"",message:"microphone off"},notices={enabled:false,message:"主动提醒默认关闭。"};
  const key=(url,method="GET")=>method+" "+url;
  function queue(url,value,method="POST"){const id=key(url,method),group=plans.get(id)||[];group.push(value);plans.set(id,group);}
  const api=async(url,options={})=>{
    const method=options.method||"GET";calls.push({url,options});
    const group=plans.get(key(url,method));if(group?.length){const planned=group.shift();return typeof planned==="function"?planned():planned;}
    if(url==="/api/chat"&&method==="GET")return structuredClone(chat);
    if(url==="/api/voice/status")return structuredClone(voice);
    if(url.startsWith("/api/voice/events?"))return {events:[],cursor:0};
    if(url==="/api/speech/status")return structuredClone(speech);
    if(url==="/api/notices/status")return structuredClone(notices);
    if(url==="/api/speech/settings"){speech={...speech,...JSON.parse(options.body)};return structuredClone(speech);}
    if(url==="/api/speech/stop"){speech={...speech,speaking:false,queued:0};return structuredClone(speech);}
    if(url==="/api/notices/settings"){notices={...notices,...JSON.parse(options.body)};return structuredClone(notices);}
    if(url==="/api/control/stop")return {};
    if(url==="/api/voice/stop"){voice={...voice,state:"ready",partial:""};return structuredClone(voice);}
    throw new Error("Unexpected fixture API: "+method+" "+url);
  };
  function emit(event="state",change={}){app={...app,...change};for(const listener of subscribers)listener(app,event);}
  const shell={api,getState:()=>app,subscribe(fn){subscribers.push(fn);fn(app,"initial");},emergencyStop(){emit("control-stop");return api("/api/control/stop",{method:"POST"});}};
  const document={activeElement:null,getElementById(id){assert.ok(elements.has(id),"Missing DOM "+id);return elements.get(id);},createElement:tag=>new Element(tag),querySelectorAll(selector){assert.equal(selector,"[data-chat-text]");return suggestions;}};
  const context=vm.createContext({document,KingdomCompanion:shell,setInterval(fn){intervals.push(fn);}});
  vm.runInContext(source,context,{filename:"conversation.js"});for(let i=0;i<5;i++)await turn();
  return {elements,calls,queue,emit,setChat:value=>{chat=value;},setSpeech:value=>{speech=value;},setVoice:value=>{voice=value;},fire:(id,type)=>elements.get(id).fire(type),async tick(){for(const fn of intervals)fn();for(let i=0;i<5;i++)await turn();},async write(text){elements.get("chat-text").value=text;await elements.get("chat-text").fire("input");},async send(text){await this.write(text);return this.fire("chat-form","submit");}};
}
const posts=(ui,url)=>ui.calls.filter(call=>call.url===url&&call.options.method==="POST");
const puts=(ui,url)=>ui.calls.filter(call=>call.url===url&&call.options.method==="PUT");

test("natural urgent stop combinations pass even when ordinary chat is full",async()=>{
  for(const phrase of ["停下，先别动","先停下来让我看看","停下听我说","先别动，等一下"]){
    const ui=await fixture({chat:{...blankChat(),busy:true,queued:3}});
    ui.queue("/api/chat",{...blankChat(),reply:"已停止",intent:"stop"});
    await ui.send(phrase);
    assert.equal(posts(ui,"/api/chat").length,1,phrase);
  }
});

test("polling only reads speech and notices; context renders literal verified information",async()=>{
  const content="<img src=x onerror=fixture()>";
  const ui=await fixture({chat:{...blankChat(),busy:true,queued:2,context:{control_goal:content,last_action:{status:"verified",message:content},message:content}}});
  assert.ok(ui.calls.every(call=>!call.options.method));
  assert.equal(ui.elements.get("speech-enabled").checked,false);assert.equal(ui.elements.get("notices-enabled").checked,false);
  assert.equal(ui.elements.get("companion-task").textContent,content);assert.equal(ui.elements.get("companion-action").textContent,content);
  assert.equal(ui.elements.get("companion-context-detail").textContent,content);assert.match(ui.elements.get("chat-queue").textContent,/排队 2 \/ 3/);
  assert.match(markup,/0\.5\.0/);assert.match(markup,/没有回声消除/);
});

test("chat admits one active and three queued requests without locking the draft or game stop",async()=>{
  const ui=await fixture(),gates=[],waiting=[];
  for(let i=0;i<4;i++){
    const gate=deferred();gates.push(gate);ui.queue("/api/chat",gate.promise);
    await ui.write("消息 "+i);waiting.push(ui.fire("chat-form","submit"));await turn();
  }
  assert.equal(posts(ui,"/api/chat").length,4);
  assert.equal(ui.elements.get("chat-text").disabled,false);assert.equal(ui.elements.get("chat-send").disabled,true);
  assert.match(ui.elements.get("chat-queue").textContent,/排队 3 \/ 3/);
  await ui.write("新草稿");await ui.fire("chat-form","submit");assert.equal(posts(ui,"/api/chat").length,4);
  assert.equal(ui.elements.get("chat-text").value,"新草稿");assert.equal(ui.elements.get("chat-stop-game").disabled,false);
  for(const gate of gates)gate.resolve(blankChat());await Promise.all(waiting);
  assert.equal(ui.elements.get("chat-text").value,"新草稿");assert.equal(ui.elements.get("chat-send").disabled,false);
  assert.equal(posts(ui,"/api/control/start").length,0);
});

test("existing backend work reserves the active slot before local messages queue",async()=>{
  const ui=await fixture({chat:{...blankChat(),busy:true}}),gates=[],waiting=[];
  for(let i=0;i<3;i++){const gate=deferred();gates.push(gate);ui.queue("/api/chat",gate.promise);await ui.write("queued "+i);waiting.push(ui.fire("chat-form","submit"));await turn();}
  assert.match(ui.elements.get("chat-queue").textContent,/排队 3 \/ 3/);
  await ui.write("too many");await ui.fire("chat-form","submit");assert.equal(posts(ui,"/api/chat").length,3);
  for(const gate of gates)gate.resolve(blankChat());await Promise.all(waiting);
});

test("explicit stop and follow bypass a full ordinary queue and older replies cannot replace them",async()=>{
  const ui=await fixture({chat:{...blankChat(),busy:true,queued:3}}),old=deferred();
  ui.queue("/api/chat",old.promise);await ui.write("跟着我");const waiting=ui.fire("chat-form","submit");await turn();
  ui.queue("/api/chat",{...blankChat(),control_result:"已停止",goal:"new accepted goal"});await ui.send("停止");
  assert.equal(posts(ui,"/api/chat").length,2);assert.equal(ui.elements.get("chat-result").textContent,"已停止");
  old.resolve({...blankChat(),control_result:"过期跟随",goal:"old goal"});await waiting;
  assert.notEqual(ui.elements.get("chat-result").textContent,"过期跟随");assert.notEqual(ui.elements.get("goal").value,"old goal");
  assert.equal(posts(ui,"/api/control/start").length,0);
});

test("a failed request restores its draft only if the user has not edited meanwhile",async()=>{
  const ui=await fixture(),first=deferred();ui.queue("/api/chat",first.promise);await ui.write("first");const waiting=ui.fire("chat-form","submit");await turn();
  await ui.write("new input");first.reject(new Error("fixture failure"));await waiting;assert.equal(ui.elements.get("chat-text").value,"new input");
  ui.queue("/api/chat",()=>Promise.reject(new Error("retry failure")));await ui.send("retry draft");assert.equal(ui.elements.get("chat-text").value,"retry draft");
});

test("speech settings use the enumerated literal voice and headphones; stopping affects only speech",async()=>{
  const name="<script>fixture</script>",ui=await fixture({speech:{...blankSpeech(),available_voices:[{name,culture:"zh-CN",enabled:true}]}});
  assert.equal(ui.elements.get("speech-voice").children[0].textContent,name+" · zh-CN");
  ui.elements.get("speech-enabled").checked=true;ui.elements.get("speech-headphones").checked=true;
  await ui.fire("speech-enabled","change");
  assert.deepEqual(JSON.parse(puts(ui,"/api/speech/settings")[0].options.body),{enabled:true,voice_name:name,headphones:true});
  await ui.fire("speech-stop");assert.equal(posts(ui,"/api/speech/stop").length,1);
  assert.equal(posts(ui,"/api/control/stop").length,0);assert.equal(posts(ui,"/api/voice/stop").length,0);
});

test("missing Windows Chinese voices are explicit and cannot be automatically enabled",async()=>{
  const ui=await fixture({speech:{...blankSpeech(),message:"",available_voices:[]}});
  assert.equal(ui.elements.get("speech-enabled").disabled,true);assert.equal(ui.elements.get("speech-voice").disabled,true);
  assert.match(ui.elements.get("speech-detail").textContent,/未安装.*Windows 中文音色/);assert.equal(puts(ui,"/api/speech/settings").length,0);
  ui.setSpeech({...blankSpeech(),message:"",available_voices:[{name:"English fixture",culture:"en-US",enabled:true}]});await ui.tick();
  assert.equal(ui.elements.get("speech-enabled").disabled,true);assert.equal(ui.elements.get("speech-voice").children[0].disabled,true);
});

test("polite stop and follow phrases match backend priority without treating quotations as commands",async()=>{
  const ui=await fixture({chat:{...blankChat(),busy:true,queued:3}});
  await ui.send("如果我说停止呢？");await ui.send("「停止」");assert.equal(posts(ui,"/api/chat").length,0);
  ui.queue("/api/chat",blankChat());await ui.send("请先停一下吧");assert.equal(posts(ui,"/api/chat").length,1);
  ui.setChat({...blankChat(),busy:true,queued:3});await ui.tick();
  ui.queue("/api/chat",blankChat());await ui.send("麻烦跟紧我好吗");assert.equal(posts(ui,"/api/chat").length,2);
});

test("speech stop invalidates a late enable response and microphone partials interrupt only on changes",async()=>{
  const ui=await fixture(),save=deferred();ui.queue("/api/speech/settings",save.promise,"PUT");
  ui.elements.get("speech-enabled").checked=true;const waiting=ui.fire("speech-enabled","change");await turn();
  await ui.fire("speech-stop");save.resolve({...blankSpeech(),enabled:true,speaking:true});await waiting;
  assert.equal(ui.elements.get("speech-enabled").checked,false);assert.notEqual(ui.elements.get("speech-status").textContent,"正在播报");
  ui.setSpeech({...blankSpeech(),enabled:true,headphones:true});await ui.tick();
  const before=posts(ui,"/api/speech/stop").length;
  ui.setVoice({state:"listening",model_ready:true,partial:"你好",message:"fixture listening"});await ui.tick();await ui.tick();
  assert.equal(posts(ui,"/api/speech/stop").length,before+1);
  ui.setVoice({state:"listening",model_ready:true,partial:"你好呀",message:"fixture listening"});await ui.tick();
  assert.equal(posts(ui,"/api/speech/stop").length,before+2);
  assert.equal(posts(ui,"/api/voice/start").length,0);assert.equal(posts(ui,"/api/chat").length,0);
});

test("typing a submitted message interrupts speech while ordinary polling does not",async()=>{
  const ui=await fixture({speech:{...blankSpeech(),enabled:true,speaking:true}});await ui.tick();await ui.tick();
  assert.equal(posts(ui,"/api/speech/stop").length,0);
  ui.queue("/api/chat",blankChat());await ui.send("hello");assert.equal(posts(ui,"/api/speech/stop").length,1);
});

test("notices require a manual toggle and shutdown ignores late settings and locks all new controls",async()=>{
  const ui=await fixture();ui.elements.get("notices-enabled").checked=true;await ui.fire("notices-enabled","change");
  assert.deepEqual(JSON.parse(puts(ui,"/api/notices/settings")[0].options.body),{enabled:true});
  const save=deferred();ui.queue("/api/speech/settings",save.promise,"PUT");ui.elements.get("speech-enabled").checked=true;
  const waiting=ui.fire("speech-enabled","change");await turn();ui.emit("shutdown",{shuttingDown:true});
  save.resolve({...blankSpeech(),enabled:true,speaking:true});await waiting;
  for(const id of ["chat-text","chat-send","chat-stop-game","speech-enabled","speech-voice","speech-headphones","speech-stop","notices-enabled"])assert.equal(ui.elements.get(id).disabled,true,id);
  const before=ui.calls.length;await ui.tick();await ui.fire("speech-stop");await ui.fire("notices-enabled","change");assert.equal(ui.calls.length,before);
});
