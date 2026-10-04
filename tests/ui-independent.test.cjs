"use strict";

// Offline frontend execution: all requests stay inside this fixture.
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path");
const test=require("node:test"),vm=require("node:vm");
const directory=path.join(__dirname,"..","companion","static");
const markup=fs.readFileSync(path.join(directory,"index.html"),"utf8");
const source=fs.readFileSync(path.join(directory,"app.js"),"utf8");
const independent="自主完成当前战役：依据真实地图、资源、建设、兵力、任务和岛屿进度，发展经济与防御、清除贪婪威胁、准备出航并推进各岛目标；以游戏报告战役完成为准，自主使用当前货币和技能。P2自由行动，不以P1是否移动作为行动条件。";
const cooperate="自主完成当前战役：依据真实地图、资源、建设、兵力、任务和岛屿进度，发展经济与防御、清除贪婪威胁、准备出航并推进各岛目标；以游戏报告战役完成为准，自主使用当前货币和技能。与P1协作，但不等待逐句指令。";
const turn=()=>new Promise(resolve=>setImmediate(resolve));
const response=value=>({ok:true,status:200,json:async()=>structuredClone(value)});
function status(){return {bridge:{connected:true,state:{captured_at:new Date().toISOString(),ready:true,coop:true,controlled_player_id:1,game_version:"fixture",bridge_version:"fixture",scene:"island",input_released:true,diagnostics:[],capabilities:["move","move_long","pay"],world:{is_paused:false},players:[{player_id:0,x:10,coins:10},{player_id:1,x:8,coins:47,transaction_pending:false,current_payable:{target_id:"fixture-wall",name:"Wall",price:3,currency:"coins",can_pay:true}}]}},control:{mode:"idle",running:false,accepting_control:true,stopping:false,needs_review:false,coin_budget_remaining:0,decisions_remaining:30,last_result:null,continuous:false,play_style:"cooperate"},model:{configured:true,key_present:true,endpoint:"https://fixture.invalid",model:"fixture"}};}
class Element{
  constructor(){this.value="";this.checked=false;this.disabled=false;this.hidden=false;this.textContent="";this.children=[];this.listeners=new Map();this.fields=[{},{},{}];this.classList={toggle(){}};}
  addEventListener(type,fn){const group=this.listeners.get(type)||[];group.push(fn);this.listeners.set(type,group);}
  querySelectorAll(){return this.fields;}
  replaceChildren(...items){this.children=items;}
  set innerHTML(_value){throw new Error("Play style text must be literal");}
  async fire(type="click"){await Promise.all((this.listeners.get(type)||[]).map(fn=>fn({preventDefault(){}})));}
}
async function fixture(initial=status()){
  const elements=new Map(),calls=[];let data=initial;
  for(const match of markup.matchAll(/<([a-z]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)){
    const element=new Element();element.value=/\bvalue="([^"]*)"/.exec(match[2])?.[1]||"";element.disabled=/\bdisabled\b/.test(match[2]);element.checked=/\bchecked\b/.test(match[2]);element.hidden=/\bhidden\b/.test(match[2]);elements.set(match[3],element);
  }
  elements.get("goal").value=/<textarea id="goal"[^>]*>([^<]*)<\/textarea>/.exec(markup)[1];
  const selectMarkup=/<select id="play-style"[^>]*>(.*?)<\/select>/.exec(markup)[1];
  elements.get("play-style").value=/<option value="([^"]+)" selected>/.exec(selectMarkup)[1];
  const fetch=async(url,options={})=>{
    calls.push({url,options});
    if(url==="/api/session")return response({token:"fixture-token"});
    if(url==="/api/installation")return response({game_directory:null,bridge_installed:false});
    if(url==="/api/status")return response(data);
    if(url==="/api/dialogue/status")return response({available:false,last_error:null});
    if(url==="/api/control/start")return response({message:"fixture started"});
    if(url==="/api/control/stop")return response({release_error:null});
    throw new Error("Unexpected fixture request "+url);
  };
  const context=vm.createContext({document:{getElementById(id){assert.ok(elements.has(id),"Missing DOM "+id);return elements.get(id);},createElement:()=>new Element()},fetch,setInterval(){}});
  vm.runInContext(source,context,{filename:"app.js"});for(let i=0;i<6;i++)await turn();
  return {elements,calls,fire:(id,type)=>elements.get(id).fire(type),async update(value){data=value;await elements.get("refresh").fire();},async style(value){elements.get("play-style").value=value;await elements.get("play-style").fire("change");}};
}
const writes=ui=>ui.calls.filter(call=>["POST","PUT"].includes(call.options.method));
const starts=ui=>writes(ui).filter(call=>call.url==="/api/control/start").map(call=>JSON.parse(call.options.body));

test("independent P2 and continuous play are defaults without an automatic control request",async()=>{
  const ui=await fixture();assert.equal(ui.elements.get("play-style").value,"independent");assert.equal(ui.elements.get("goal").value,independent);
  assert.equal(ui.elements.get("continuous-play").checked,true);assert.equal(ui.elements.get("coin-budget").value,"0");
  assert.match(ui.elements.get("play-style-note").textContent,/P1 始终由你手动控制.*P1 未操作时 P2 也可行动.*暂停或进入菜单/);
  assert.match(ui.elements.get("play-style-status").textContent,/尚未启动.*草稿/);
  assert.equal(writes(ui).length,0);assert.match(markup,/0\.5\.0/);
});

test("selecting a play style replaces only the previous unchanged default goal",async()=>{
  const ui=await fixture();await ui.style("cooperate");assert.equal(ui.elements.get("goal").value,cooperate);
  await ui.style("independent");assert.equal(ui.elements.get("goal").value,independent);
  const custom="<script>literal goal</script>只巡视东侧，保留金币";ui.elements.get("goal").value=custom;await ui.fire("goal","input");
  await ui.style("cooperate");await ui.style("independent");assert.equal(ui.elements.get("goal").value,custom);
  ui.elements.get("goal").value="";await ui.fire("goal","input");await ui.style("cooperate");assert.equal(ui.elements.get("goal").value,"");
  assert.equal(writes(ui).length,0);
});

test("actual running style remains authoritative when the next-run draft changes",async()=>{
  const data=status();data.control={...data.control,mode:"autonomous",running:true,continuous:true,play_style:"cooperate",coin_budget_remaining:2};
  const ui=await fixture(data);assert.match(ui.elements.get("play-style-status").textContent,/实际运行：协作陪玩.*不会切换本次/);
  assert.equal(ui.elements.get("mode").textContent,"协作陪玩中");await ui.style("independent");
  assert.equal(ui.elements.get("mode").textContent,"协作陪玩中");assert.match(ui.elements.get("play-style-status").textContent,/实际运行：协作陪玩/);
  data.control.play_style="independent";await ui.update(data);await ui.style("cooperate");
  assert.equal(ui.elements.get("mode").textContent,"P2自由游玩中");assert.match(ui.elements.get("play-style-status").textContent,/实际运行：自由游玩/);
  assert.equal(ui.elements.get("remaining-coins").textContent,2);assert.equal(writes(ui).length,0);
});

test("autonomous starts carry the chosen style; follow carries neither style nor continuous flag",async()=>{
  const ui=await fixture();await ui.fire("autonomous");let bodies=starts(ui);
  assert.equal(bodies[0].play_style,"independent");assert.equal(bodies[0].continuous,true);assert.equal(bodies[0].goal,independent);assert.equal(bodies[0].coin_budget,0);
  await ui.style("cooperate");ui.elements.get("coin-budget").value="8";await ui.fire("coin-budget","input");await ui.fire("autonomous");bodies=starts(ui);
  assert.equal(bodies[1].play_style,"cooperate");assert.equal(bodies[1].goal,cooperate);assert.equal(bodies[1].coin_budget,0);assert.equal(bodies[1].spending_mode,"wallet");
  await ui.fire("follow");const follow=starts(ui)[2];assert.equal(follow.mode,"follow");assert.equal(follow.coin_budget,0);
  assert.equal(Object.hasOwn(follow,"play_style"),false);assert.equal(Object.hasOwn(follow,"continuous"),false);
});

test("free-play draft suggests continuity without enabling it or increasing coin authority",async()=>{
  const ui=await fixture();ui.elements.get("continuous-play").checked=false;await ui.fire("continuous-play","change");
  await ui.style("cooperate");await ui.style("independent");assert.equal(ui.elements.get("continuous-play").checked,false);
  assert.match(ui.elements.get("play-style-note").textContent,/请勾选「持续自主游玩」/);
  assert.equal(ui.elements.get("coin-budget").value,"0");assert.equal(writes(ui).length,0);
});

test("inactive P1 does not block independent autonomy, while menu state still locks start controls",async()=>{
  const data=status();data.bridge.state.players[0].x=null;const ui=await fixture(data);
  assert.equal(ui.elements.get("autonomous").disabled,false);assert.equal(ui.elements.get("follow").disabled,true);
  data.bridge.state.ready=false;data.bridge.state.scene="Menu";data.bridge.state.world.is_paused=true;await ui.update(data);
  assert.equal(ui.elements.get("autonomous").disabled,true);assert.equal(ui.elements.get("follow").disabled,true);
  assert.equal(writes(ui).length,0);
});

test("follow status accurately excludes autonomous style and legacy status falls back to cooperative",async()=>{
  const data=status();data.control={...data.control,mode:"follow",running:true,play_style:"independent"};const ui=await fixture(data);
  assert.match(ui.elements.get("play-style-status").textContent,/实际运行：跟随 P1，不采用自主玩法/);
  data.control.mode="autonomous";delete data.control.play_style;await ui.update(data);
  assert.equal(ui.elements.get("mode").textContent,"协作陪玩中");assert.match(ui.elements.get("play-style-status").textContent,/实际运行：协作陪玩/);
});
