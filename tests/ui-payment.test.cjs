"use strict";

// Exercise actual frontend code with local DOM/network fixtures only.
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path");
const test=require("node:test"),vm=require("node:vm");
const directory=path.join(__dirname,"..","companion","static");
const markup=fs.readFileSync(path.join(directory,"index.html"),"utf8");
const source=fs.readFileSync(path.join(directory,"app.js"),"utf8");
const turn=()=>new Promise(resolve=>setImmediate(resolve));
const response=value=>({ok:true,status:200,json:async()=>structuredClone(value)});
function status(){return {bridge:{connected:true,state:{captured_at:new Date().toISOString(),ready:true,coop:true,controlled_player_id:1,game_version:"fixture",bridge_version:"fixture",scene:"island",input_released:true,diagnostics:[],capabilities:["move","move_long","pay"],players:[{player_id:0,x:10,coins:10},{player_id:1,x:8,coins:47,transaction_pending:false,current_payable:{target_id:"fixture-wall",name:"Wall",price:3,currency:"coins",can_pay:true}}]}},control:{mode:"idle",running:false,accepting_control:true,stopping:false,needs_review:false,coin_budget_remaining:0,decisions_remaining:30,last_result:null,continuous:false},model:{configured:true,key_present:true,endpoint:"https://fixture.invalid",model:"fixture"}};}
class Element{
  constructor(){this.value="";this.checked=false;this.disabled=false;this.hidden=false;this.textContent="";this.children=[];this.listeners=new Map();this.fields=[{},{},{}];this.classList={toggle(){}};}
  addEventListener(type,fn){const group=this.listeners.get(type)||[];group.push(fn);this.listeners.set(type,group);}
  querySelectorAll(){return this.fields;}
  replaceChildren(...items){this.children=items;}
  set innerHTML(_value){throw new Error("Payment details must use literal text");}
  async fire(type="click"){await Promise.all((this.listeners.get(type)||[]).map(fn=>fn({preventDefault(){}})));}
}
async function fixture(initial=status(),dialogue={available:false,last_error:null},wallet=false){
  const elements=new Map(),calls=[],plans=new Map();let data=initial;
  for(const match of markup.matchAll(/<([a-z]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)){
    const element=new Element();element.value=/\bvalue="([^"]*)"/.exec(match[2])?.[1]||"";element.disabled=/\bdisabled\b/.test(match[2]);element.checked=/\bchecked\b/.test(match[2]);element.hidden=/\bhidden\b/.test(match[2]);elements.set(match[3],element);
  }
  elements.get("goal").value=/<textarea id="goal"[^>]*>([^<]*)<\/textarea>/.exec(markup)[1];
  // Existing budget-mode cases explicitly exercise the optional bounded setting.
  elements.get("wallet-spending").checked=wallet;
  const fetch=async(url,options={})=>{
    calls.push({url,options});if(plans.has(url)){const plan=plans.get(url);plans.delete(url);return plan();}
    if(url==="/api/session")return response({token:"fixture-token"});
    if(url==="/api/installation")return response({game_directory:null,bridge_installed:false});
    if(url==="/api/status")return response(data);
    if(url==="/api/dialogue/status")return response(dialogue);
    if(url==="/api/control/start")return response({message:"fixture started"});
    if(url==="/api/control/manual")return response({status:"verified",message:"fixture paid"});
    if(url==="/api/control/stop")return response({release_error:null});
    throw new Error("Unexpected fixture request "+url);
  };
  const context=vm.createContext({document:{getElementById(id){assert.ok(elements.has(id),"Missing DOM "+id);return elements.get(id);},createElement:()=>new Element()},fetch,setInterval(){}});
  vm.runInContext(source,context,{filename:"app.js"});for(let i=0;i<6;i++)await turn();
  return {elements,calls,fire:(id,type)=>elements.get(id).fire(type),async update(value){data=value;await elements.get("refresh").fire();},failRefresh(){plans.set("/api/status",()=>Promise.reject(new Error("fixture disconnected")));},async budget(value){elements.get("coin-budget").value=String(value);await elements.get("coin-budget").fire("input");}};
}
const writes=ui=>ui.calls.filter(call=>["POST","PUT"].includes(call.options.method));
const p2=data=>data.bridge.state.players[1];

test("idle zero-budget Tower2 shows both missing authorization and its actual payment block",async()=>{
  const data=status();p2(data).current_payable={target_id:"fixture-tower",name:"Tower2",price:4,currency:"coins",can_pay:false};
  const ui=await fixture(data);
  assert.match(ui.elements.get("payment-budget-note").textContent,/尚未启动.*草稿.*尚未授权/);
  assert.match(ui.elements.get("payment-target-note").textContent,/Tower2 当前不可支付/);
  assert.match(ui.elements.get("manual-pay-note").textContent,/Tower2 当前不可支付/);
  assert.equal(ui.elements.get("pay-coin").disabled,true);assert.equal(ui.elements.get("follow").disabled,false);assert.equal(ui.elements.get("autonomous").disabled,false);
  assert.equal(writes(ui).length,0);assert.match(markup,/0\.5\.0/);
});

test("budget and target edits update a draft immediately without authorizing or starting anything",async()=>{
  const ui=await fixture();await ui.budget(8);
  assert.match(ui.elements.get("payment-budget-note").textContent,/草稿为 8 枚金币.*尚未授权/);
  assert.equal(ui.elements.get("remaining-coins").textContent,0);assert.equal(writes(ui).length,0);
  await ui.budget(0);ui.elements.get("goal").value="升级营地建筑并购买商店用品";await ui.fire("goal","input");
  assert.equal(ui.elements.get("payment-goal-note").hidden,false);assert.match(ui.elements.get("payment-goal-note").textContent,/预算草稿为 0.*不会给建筑或商店投币/);
  assert.equal(ui.elements.get("autonomous").disabled,false);assert.equal(ui.elements.get("follow").disabled,false);
  assert.equal(ui.elements.get("pay-coin").disabled,false,"manual one-shot authorization remains independent of the autonomous draft");
  assert.equal(writes(ui).length,0);
});

test("running autonomous payment displays actual remaining authority despite higher local drafts",async()=>{
  const data=status();data.control={...data.control,mode:"autonomous",running:true,coin_budget_remaining:2,continuous:true,goal:"购买商店用品"};
  const ui=await fixture(data);await ui.budget(20);
  assert.match(ui.elements.get("payment-mode").textContent,/剩余 2 枚/);assert.match(ui.elements.get("payment-budget-note").textContent,/后台确认的剩余 2 枚.*修改不会增加本次额度/);
  assert.match(ui.elements.get("payment-target-note").textContent,/剩余额度 2 枚不足/);
  assert.equal(ui.elements.get("remaining-decisions").textContent,"持续");assert.equal(ui.elements.get("remaining-coins").textContent,2);
  assert.match(ui.elements.get("manual-pay-note").textContent,/AI 正在运行.*手动付款已锁定/);
  data.control.coin_budget_remaining=0;await ui.update(data);
  assert.equal(ui.elements.get("payment-goal-note").hidden,false);assert.match(ui.elements.get("payment-goal-note").textContent,/本次剩余额度为 0/);
  assert.equal(writes(ui).length,0);
});

test("follow never authorizes payments even when a nonzero next-run budget is drafted",async()=>{
  const data=status();data.control={...data.control,mode:"follow",running:true};const ui=await fixture(data);await ui.budget(10);
  assert.equal(ui.elements.get("payment-mode").textContent,"跟随不投币");assert.match(ui.elements.get("payment-budget-note").textContent,/不会给建筑或商店投币.*草稿为 10/);
  assert.equal(ui.elements.get("pay-coin").disabled,true);assert.equal(writes(ui).length,0);
});

test("each observed payment blocker has a specific explanation and disabled clicks cannot submit",async()=>{
  const cases=[
    [data=>{p2(data).current_payable=null;},/尚未选中/],
    [data=>{p2(data).current_payable.price=null;},/价格尚未读取/],
    [data=>{p2(data).current_payable.can_pay=false;},/当前不可支付/],
    [data=>{p2(data).current_payable.can_pay=null;},/可支付状态尚未读取/],
    [data=>{p2(data).current_payable.currency=null;},/币种尚未读取/],
    [data=>{p2(data).current_payable.currency="gems";},/对应货币.*钱包余额尚未读取/],
    [data=>{p2(data).current_payable.price=0;},/无需投币/],
    [data=>{p2(data).coins=1;},/钱包只有 1.*不足/],
    [data=>{p2(data).coins=null;},/钱包余额尚未读取/],
    [data=>{p2(data).transaction_pending=true;},/交易尚未结算/],
    [data=>{p2(data).transaction_pending=null;},/交易状态尚未读取/],
    [data=>{data.bridge.state.capabilities=["move"];},/未提供完整付款能力/],
    [data=>{data.control.stopping=true;},/正在停止.*输入释放/],
    [data=>{data.bridge.state.coop=false;},/实时可操作状态未就绪/],
  ];
  const ui=await fixture();
  for(const [change,expected] of cases){const data=status();change(data);await ui.update(data);assert.equal(ui.elements.get("pay-coin").disabled,true);assert.match(ui.elements.get("manual-pay-note").textContent,expected);await ui.fire("pay-coin");}
  assert.equal(writes(ui).length,0);
});

test("noncoin targets and literal names are rendered without mislabeling or HTML",async()=>{
  const data=status();p2(data).current_payable={target_id:"fixture-gem",name:"<script>fixture</script>",price:2,currency:"gems",can_pay:true};
  const ui=await fixture(data);
  assert.match(ui.elements.get("payable").textContent,/<script>fixture<\/script> · 2 颗宝石/);
  assert.equal(ui.elements.get("pay-coin").textContent,"完整支付 2 颗宝石");assert.equal(ui.elements.get("pay-coin").disabled,true);
});

test("old unknown-action records no longer lock control; lost connection stays locked",async()=>{
  const data=status();data.control={...data.control,needs_review:true,last_result:{operation:"pay",status:"unknown",message:"fixture unknown"}};
  const ui=await fixture(data);await ui.budget(20);assert.match(ui.elements.get("payment-target-note").textContent,/当前可完整支付/);
  assert.equal(ui.elements.has("review"),false);assert.equal(ui.elements.get("pay-coin").disabled,false);
  assert.equal(ui.elements.get("autonomous").disabled,false);
  ui.failRefresh();await ui.fire("refresh");assert.match(ui.elements.get("manual-pay-note").textContent,/连接中断/);
  await ui.fire("pay-coin");assert.equal(writes(ui).length,0);
});

test("continuous autonomy is default, uses an explicit flag, and can opt into bounded decisions",async()=>{
  const ui=await fixture();assert.equal(ui.elements.get("continuous-play").checked,true);assert.equal(ui.elements.get("decision-budget").disabled,true);
  assert.match(ui.elements.get("continuous-note").textContent,/不断请求模型.*计费.*不要求人工核验/);
  assert.match(ui.elements.get("goal").value,/自主完成当前战役/);
  await ui.fire("autonomous");let starts=writes(ui).filter(call=>call.url==="/api/control/start");
  assert.equal(JSON.parse(starts[0].options.body).continuous,true);assert.equal(JSON.parse(starts[0].options.body).coin_budget,0);
  ui.elements.get("continuous-play").checked=false;await ui.fire("continuous-play","change");assert.equal(ui.elements.get("decision-budget").disabled,false);
  await ui.budget(5);await ui.fire("autonomous");starts=writes(ui).filter(call=>call.url==="/api/control/start");
  assert.equal(JSON.parse(starts[1].options.body).continuous,false);assert.equal(JSON.parse(starts[1].options.body).coin_budget,5);
  await ui.fire("follow");starts=writes(ui).filter(call=>call.url==="/api/control/start");const follow=JSON.parse(starts[2].options.body);
  assert.equal(follow.mode,"follow");assert.equal(follow.coin_budget,0);assert.equal(Object.hasOwn(follow,"continuous"),false);
});

test("game bubble status is read only, supports old modules, and renders literal display errors",async()=>{
  let ui=await fixture();assert.match(ui.elements.get("game-bubble-status").textContent,/尚不支持.*更新模组/);assert.equal(writes(ui).length,0);
  ui=await fixture(status(),{available:true,last_error:null});assert.match(ui.elements.get("game-bubble-status").textContent,/游戏内气泡可用/);assert.equal(writes(ui).length,0);
  ui=await fixture(status(),{available:true,last_error:"<script>fixture</script>"});assert.match(ui.elements.get("game-bubble-status").textContent,/<script>fixture<\/script>/);assert.equal(writes(ui).length,0);
  const css=fs.readFileSync(path.join(directory,"style.css"),"utf8");assert.match(css,/\.chat-message p::before/);assert.match(css,/\.chat-message\.user p::before/);
});

test("an empty disabled decision draft cannot prevent continuous startup or change coin authority",async()=>{
  const ui=await fixture();ui.elements.get("decision-budget").value="";await ui.fire("autonomous");
  const body=JSON.parse(writes(ui).find(call=>call.url==="/api/control/start").options.body);
  assert.equal(body.continuous,true);assert.equal(body.decision_budget,30);assert.equal(body.coin_budget,0);
  await ui.fire("follow");const follow=JSON.parse(writes(ui).filter(call=>call.url==="/api/control/start")[1].options.body);
  assert.equal(follow.mode,"follow");assert.equal(follow.decision_budget,30);assert.equal(follow.coin_budget,0);
});

test("wallet spending is the page default and starts with no cumulative budget",async()=>{
  assert.match(markup,/<input id="wallet-spending"[^>]*checked/);
  const ui=await fixture(status(),undefined,true);
  assert.equal(ui.elements.get("coin-budget").disabled,true);
  assert.match(ui.elements.get("payment-budget-note").textContent,/尚未启动.*AI自主决定.*不设本次累计额度/);
  assert.equal(ui.elements.get("payment-goal-note").hidden,true);
  await ui.budget("not a number");await ui.fire("autonomous");
  const body=JSON.parse(writes(ui).find(call=>call.url==="/api/control/start").options.body);
  assert.equal(body.spending_mode,"wallet");assert.equal(body.coin_budget,0);
  assert.equal(body.continuous,true);
});

test("wallet or limited drafts never change the currently running coin authority",async()=>{
  const data=status();data.control={...data.control,mode:"autonomous",running:true,spending_mode:"wallet",coin_budget_remaining:null};
  const ui=await fixture(data,undefined,true);
  assert.equal(ui.elements.get("payment-mode").textContent,"AI自主支配");
  assert.equal(ui.elements.get("remaining-coins").textContent,"AI自主支配");
  assert.match(ui.elements.get("payment-target-note").textContent,/AI自行决定是否购买/);
  ui.elements.get("wallet-spending").checked=false;await ui.fire("wallet-spending","change");await ui.budget(0);
  assert.equal(ui.elements.get("coin-budget").disabled,false);
  assert.equal(ui.elements.get("payment-mode").textContent,"AI自主支配");
  assert.equal(ui.elements.get("payment-goal-note").hidden,true);assert.equal(writes(ui).length,0);
});

test("optional limited mode and follow keep their explicit payment policy",async()=>{
  const ui=await fixture(status(),undefined,true);
  ui.elements.get("wallet-spending").checked=false;await ui.fire("wallet-spending","change");await ui.budget(7);
  await ui.fire("autonomous");await ui.fire("follow");
  const starts=writes(ui).filter(call=>call.url==="/api/control/start").map(call=>JSON.parse(call.options.body));
  assert.equal(starts[0].spending_mode,"budgeted");assert.equal(starts[0].coin_budget,7);
  assert.equal(starts[1].coin_budget,0);assert.equal(Object.hasOwn(starts[1],"spending_mode"),false);
  assert.equal(ui.elements.has("acknowledge"),false);
});

test("runtime phase distinguishes slow decisions, payment and a temporary wait",async()=>{
  const data=status();data.control={...data.control,mode:"autonomous",running:true,phase:"paying",phase_seconds:8,decisions_made:8};
  const ui=await fixture(data);
  assert.equal(ui.elements.get("decision-phase").textContent,"付款中");
  assert.match(ui.elements.get("decision-phase-detail").textContent,/已请求 8 次决策.*当前阶段 8 秒.*原生交易/);
  data.control.phase="deciding";await ui.update(data);
  assert.equal(ui.elements.get("decision-phase").textContent,"等待模型决策");
  assert.match(ui.elements.get("decision-phase-detail").textContent,/格式错误或临时超时会重新决策/);
  data.control.phase="waiting";await ui.update(data);assert.equal(ui.elements.get("decision-phase").textContent,"等待后重选");
  assert.equal(writes(ui).length,0);
});
