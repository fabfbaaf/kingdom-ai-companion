"use strict";

const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path");
const test=require("node:test"),vm=require("node:vm");
const directory=path.join(__dirname,"..","companion","static");
const markup=fs.readFileSync(path.join(directory,"index.html"),"utf8");
const source=fs.readFileSync(path.join(directory,"app.js"),"utf8");
const conversation=fs.readFileSync(path.join(directory,"conversation.js"),"utf8");
const turn=()=>new Promise(resolve=>setImmediate(resolve));
function deferred(){let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};}
function response(value,status=200){return {ok:status>=200&&status<300,status,json:async()=>structuredClone(value)};}
function state(){return {bridge:{connected:false,state:null,error:"fixture offline game"},control:{mode:"idle",running:false,accepting_control:true,needs_review:false},model:{configured:true,key_present:true,endpoint:"https://fixture.invalid",model:"fixture"}};}

class Element{
  constructor(tag="div"){this.tag=tag;this.value="";this.textContent="";this.disabled=false;this.hidden=false;this.checked=false;this.children=[];this.dataset={};this.listeners=new Map();this.scrollHeight=100;this.clientHeight=100;this.scrollTop=0;this.classList={toggle(){}};this.fields=Array.from({length:3},()=>({textContent:""}));}
  addEventListener(type,fn){const group=this.listeners.get(type)||[];group.push(fn);this.listeners.set(type,group);}
  querySelectorAll(){return this.fields;}
  replaceChildren(...items){this.children=items;}
  append(...items){this.children.push(...items);}
  focus(){}
  set innerHTML(_value){throw new Error("Untrusted chat must never be inserted as HTML");}
  async fire(type="click"){await Promise.all((this.listeners.get(type)||[]).map(fn=>fn({preventDefault(){}})));}
}

async function fixture({sessionGate=null}={}){
  const elements=new Map(),suggestions=[];
  for(const match of markup.matchAll(/<([a-z]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)){
    const element=new Element(match[1]);element.disabled=/\bdisabled\b/.test(match[2]);element.hidden=/\bhidden\b/.test(match[2]);element.value=/\bvalue="([^"]*)"/.exec(match[2])?.[1]||"";elements.set(match[3],element);
  }
  for(const match of markup.matchAll(/data-chat-text="([^"]+)"/g)){const element=new Element("button");element.dataset.chatText=match[1];suggestions.push(element);}
  elements.get("goal").value="fixture initial goal";
  const calls=[],plans=new Map(),intervals=[];
  let voice={state:"needs_model",message:"Fixture model needed",partial:"",model_ready:false};
  let chat={messages:[],goal:null},events={events:[],cursor:0,dropped:0};
  function queue(url,value){const group=plans.get(url)||[];group.push(value);plans.set(url,group);}
  const fetch=async(url,options={})=>{
    calls.push({url,options});
    const planned=plans.get(url)?.shift();if(planned!==undefined)return typeof planned==="function"?planned():planned;
    if(url==="/api/session")return sessionGate?sessionGate.promise:response({token:"fixture-token"});
    if(url==="/api/installation")return response({game_directory:null,bridge_installed:false});
    if(url==="/api/status")return response(state());
    if(url==="/api/chat"&&!options.method)return response(chat);
    if(url==="/api/voice/status")return response(voice);
    if(url.startsWith("/api/voice/events?"))return response(events);
    if(url==="/api/control/stop")return response({release_error:null});
    if(url==="/api/voice/stop"){voice={...voice,state:"ready",model_ready:true,partial:""};return response(voice);}
    if(url==="/api/voice/start"){voice={...voice,state:"listening",model_ready:true};return response(voice);}
    if(url==="/api/voice/prepare"){voice={...voice,state:"downloading",progress:0.25};return response(voice);}
    throw new Error(`Unexpected fixture request: ${url}`);
  };
  const document={activeElement:null,getElementById(id){assert.ok(elements.has(id),`Missing DOM ${id}`);return elements.get(id);},createElement:tag=>new Element(tag),querySelectorAll(selector){assert.equal(selector,"[data-chat-text]");return suggestions;}};
  const context=vm.createContext({document,fetch,setInterval(fn){intervals.push(fn);return intervals.length;}});
  vm.runInContext(source,context,{filename:"app.js"});vm.runInContext(conversation,context,{filename:"conversation.js"});
  for(let i=0;i<8;i++)await turn();
  return {elements,calls,queue,document,fire:(id,type)=>elements.get(id).fire(type),setVoice:value=>{voice=value;},setChat:value=>{chat=value;},setEvents:value=>{events=value;},async tick(){for(const fn of intervals)await fn();for(let i=0;i<5;i++)await turn();}};
}
function posts(ui,url){return ui.calls.filter(call=>call.url===url&&call.options.method==="POST");}

test("widgets wait for shared authenticated session and never automatically download or listen",async()=>{
  const gate=deferred(),ui=await fixture({sessionGate:gate});
  assert.deepEqual(ui.calls.map(call=>call.url),["/api/session"]);
  assert.equal(ui.elements.get("chat-send").disabled,true);
  gate.resolve(response({token:"fixture-token"}));await ui.tick();
  assert.ok(ui.calls.some(call=>call.url==="/api/chat"));
  assert.ok(ui.calls.filter(call=>call.url.startsWith("/api/voice/")).every(call=>!call.options.method));
  assert.equal(posts(ui,"/api/voice/start").length,0);assert.equal(posts(ui,"/api/voice/prepare").length,0);
  assert.equal(ui.elements.get("chat-send").disabled,false);
});

test("DeepSeek preset only fills form; it preserves protected key and waits for save",async()=>{
  const ui=await fixture();ui.elements.get("api-key").value="synthetic-key";
  const before=ui.calls.length;await ui.fire("model-deepseek");
  assert.equal(ui.elements.get("endpoint").value,"https://api.deepseek.com");
  assert.equal(ui.elements.get("model").value,"deepseek-flash");
  assert.equal(ui.elements.get("api-key").value,"synthetic-key");assert.equal(ui.calls.length,before);
});

test("chat sends only text using shared token and renders literal content safely",async()=>{
  const ui=await fixture(),content="<img src=x onerror=alert(1)>";
  ui.queue("/api/chat",response({reply:content,intent:"chat",goal:null,control_result:"",messages:[{id:1,role:"user",content:"你好",source:"text"},{id:2,role:"assistant",content,source:"text"}]}));
  ui.elements.get("chat-text").value="你好";await ui.fire("chat-form","submit");
  const request=posts(ui,"/api/chat")[0];assert.deepEqual(JSON.parse(request.options.body),{text:"你好"});
  assert.equal(request.options.headers["X-Kingdom-Session"],"fixture-token");
  // A later authoritative GET may replace the POST history; inspect the render before it via server state.
  ui.setChat({messages:[{id:2,role:"assistant",content,source:"text"}],goal:null});await ui.tick();
  assert.equal(ui.elements.get("chat-log").children[0].children[1].textContent,content);
  assert.equal(posts(ui,"/api/control/start").length,0);
});

test("AI stop and voice off bypass pending chat; late reply cannot apply goal or start anything",async()=>{
  const ui=await fixture(),reply=deferred();ui.queue("/api/chat",reply.promise);
  ui.elements.get("chat-text").value="跟着我";const waiting=ui.fire("chat-form","submit");await turn();
  assert.equal(ui.elements.get("chat-send").disabled,true);
  await ui.fire("chat-stop-game");await ui.fire("voice-stop");
  assert.equal(posts(ui,"/api/control/stop").length,1);assert.equal(posts(ui,"/api/voice/stop").length,1);
  reply.resolve(response({reply:"old reply",intent:"follow",goal:"late goal",control_result:"old action",messages:[]}));await waiting;
  assert.equal(ui.elements.get("goal").value,"fixture initial goal");
  assert.notEqual(ui.elements.get("chat-result").textContent,"old action");
  assert.equal(posts(ui,"/api/control/start").length,0);assert.equal(posts(ui,"/api/voice/start").length,0);
});

test("voice final is display-only and authoritative chat source prevents duplicate dispatch",async()=>{
  const ui=await fixture();ui.setVoice({state:"listening",model_ready:true,partial:"跟着",message:"listening",device:"Fixture microphone"});
  ui.setEvents({events:[{seq:1,text:"跟着我",captured_at:new Date().toISOString()}],cursor:1,dropped:0});
  ui.setChat({messages:[{id:1,role:"user",source:"voice",content:"跟着我"},{id:2,role:"assistant",source:"system",content:"已请求跟随"}],goal:null});
  await ui.tick();await ui.tick();
  assert.match(ui.elements.get("voice-final").textContent,/跟着我/);
  assert.equal(ui.elements.get("voice-partial").textContent,"跟着");
  assert.equal(ui.elements.get("chat-log").children.length,2);
  assert.equal(ui.elements.get("chat-log").children[0].children[0].textContent,"你 · 语音");
  assert.equal(posts(ui,"/api/chat").length,0);assert.equal(posts(ui,"/api/control/start").length,0);
  assert.ok(ui.calls.some(call=>call.url==="/api/voice/events?after=1"));
});

test("prepare and microphone start require clicks; a late start reply cannot undo voice stop",async()=>{
  const ui=await fixture();await ui.fire("voice-start");assert.equal(posts(ui,"/api/voice/start").length,0);
  await ui.fire("voice-prepare");assert.equal(posts(ui,"/api/voice/prepare").length,1);
  assert.match(ui.elements.get("voice-prepare").textContent,/25%/);assert.equal(posts(ui,"/api/voice/start").length,0);
  ui.setVoice({state:"ready",model_ready:true,message:"ready",partial:""});await ui.tick();
  const start=deferred();ui.queue("/api/voice/start",start.promise);const starting=ui.fire("voice-start");await turn();
  await ui.fire("voice-stop");start.resolve(response({state:"listening",model_ready:true,message:"late",partial:"late"}));await starting;
  assert.equal(ui.elements.get("voice-status").textContent,"未监听");assert.notEqual(ui.elements.get("voice-partial").textContent,"late");
  assert.equal(posts(ui,"/api/voice/start").length,1);
});

test("new spoken goal syncs idle start form; unchanged server goal preserves local draft",async()=>{
  const ui=await fixture();ui.setChat({messages:[],goal:"语音指定的新目标"});await ui.tick();
  assert.equal(ui.elements.get("goal").value,"语音指定的新目标");
  ui.elements.get("goal").value="正在改的本地目标";ui.document.activeElement=ui.elements.get("goal");
  ui.setChat({messages:[],goal:"随后更新的服务端目标"});await ui.tick();
  assert.equal(ui.elements.get("goal").value,"正在改的本地目标");ui.document.activeElement=null;await ui.tick();
  assert.equal(ui.elements.get("goal").value,"正在改的本地目标");
});

test("backend shutdown locks chat and mic, and pending chat cannot resume polling or controls",async()=>{
  const ui=await fixture(),reply=deferred();ui.queue("/api/chat",reply.promise);ui.elements.get("chat-text").value="hello";
  const chatting=ui.fire("chat-form","submit");await turn();ui.queue("/api/shutdown",response({message:"fixture exit"}));
  await ui.fire("shutdown");reply.resolve(response({reply:"late",intent:"follow",goal:"late",control_result:"late",messages:[]}));await chatting;
  for(const id of ["chat-send","chat-text","chat-stop-game","voice-prepare","voice-start","voice-stop"])assert.equal(ui.elements.get(id).disabled,true);
  const before=ui.calls.length;await ui.tick();await ui.fire("voice-start");await ui.fire("voice-stop");assert.equal(ui.calls.length,before);
});

test("voice error or pending hardware close never claims microphone is already off",async()=>{
  const ui=await fixture();ui.setVoice({state:"error",model_ready:true,stopping:false,message:"fixture native close failure",partial:""});await ui.tick();
  assert.equal(ui.elements.get("voice-status").textContent,"需检查");
  ui.setVoice({state:"error",model_ready:true,stopping:true,message:"fixture closing",partial:""});await ui.tick();
  assert.equal(ui.elements.get("voice-status").textContent,"正在关闭");assert.equal(ui.elements.get("voice-start").disabled,true);
  await ui.fire("voice-start");assert.equal(posts(ui,"/api/voice/start").length,0);
  assert.equal(ui.elements.get("voice-stop").disabled,false);
});

test("input selection lists literal device names and sends selection without opening microphone",async()=>{
  const ui=await fixture(),value={state:"ready",model_ready:true,devices:[{index:5,name:"USB <img>",host_api:"WASAPI",default:false}],selected_device:null};
  ui.queue("/api/voice/devices",response(value));await ui.fire("voice-refresh");
  assert.equal(ui.elements.get("voice-input").children[1].textContent,"USB <img> · WASAPI");
  ui.elements.get("voice-input").value="5";ui.queue("/api/voice/device",response({...value,selected_device:5}));await ui.fire("voice-input","change");
  const call=ui.calls.find(call=>call.url==="/api/voice/device");assert.deepEqual(JSON.parse(call.options.body),{device:5});
  assert.equal(posts(ui,"/api/voice/start").length,0);
});

test("input meter, device fault, voice backlog and plan evidence are visible",async()=>{
  const ui=await fixture();
  ui.setVoice({state:"listening",model_ready:true,level:0.5,peak:0.6,partial:"",message:"fixture"});
  ui.setChat({messages:[],busy:true,queued:0,voice_queued:3,voice_error:"A voice request failed",context:{
    plan:{stage:"defense",status:"waiting_observation",objective:"Guard <right>",criteria:[{description:"Walls repaired",status:"unknown",observed:null}]},
    memory:{identity_verified:true,islands:[{land:0,zone:"surface",known_objects:12,explored:[0,10]}]}}});
  await ui.tick();
  assert.ok(ui.elements.get("voice-level").value>80);assert.match(ui.elements.get("voice-level-label").textContent,/-6 dBFS/);
  assert.match(ui.elements.get("chat-queue").textContent,/语音等待 3 句/);assert.equal(ui.elements.get("voice-diagnostic").textContent,"A voice request failed");
  assert.equal(ui.elements.get("strategy-objective").textContent,"Guard <right>");assert.match(ui.elements.get("strategy-criteria").children[0].textContent,/未知/);
  assert.equal(ui.elements.get("memory-islands").children.length,1);
});

test("natural arrangements can be submitted while ordinary dialogue queue is full",async()=>{
  const ui=await fixture();ui.setChat({messages:[],busy:true,queued:3});await ui.tick();
  for(const text of ["先发展经济","今晚守右边","钱留着修船"]){ui.elements.get("chat-text").value=text;await ui.fire("chat-text","input");assert.equal(ui.elements.get("chat-send").disabled,false);}
  ui.elements.get("chat-text").value="如果今晚守右边呢";await ui.fire("chat-text","input");assert.equal(ui.elements.get("chat-send").disabled,true);
});
