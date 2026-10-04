"use strict";

// Execute the shipped UI with a small DOM/network boundary. No browser or game is started.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const staticDirectory = path.join(__dirname, "..", "companion", "static");
const source = fs.readFileSync(path.join(staticDirectory, "app.js"), "utf8");
const markup = fs.readFileSync(path.join(staticDirectory, "index.html"), "utf8");
const turn = () => new Promise((resolve) => setImmediate(resolve));

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return {promise, resolve};
}

function response(value, status = 200) {
  return {ok: status >= 200 && status < 300, status,
    json: async () => structuredClone(value)};
}

function readyStatus(price = 3) {
  return {
    bridge: {connected: true, state: {
      captured_at: new Date().toISOString(), ready: true, coop: true,
      controlled_player_id: 1, game_version: "fixture", bridge_version: "fixture",
      scene: "island", input_released: true, diagnostics: [], capabilities: ["move", "pay"],
      players: [
        {player_id: 0, x: 10, coins: 12, stamina: 1},
        {player_id: 1, x: 8, coins: 12, stamina: 1, transaction_pending: false,
          current_payable: {target_id: "fixture-wall", name: "wall", price,
            currency: "coins", can_pay: true}},
      ],
    }},
    control: {mode: "idle", running: false, accepting_control: true, needs_review: false,
      coin_budget_remaining: 0, decisions_remaining: 0, last_result: null},
    model: {configured: true, key_present: true, endpoint: "http://localhost:9000/v1",
      model: "fixture"},
  };
}

class Element {
  constructor(tag = "div") {
    this.tag = tag;
    this.disabled = false;
    this.hidden = false;
    this.checked = false;
    this.value = "";
    this.textContent = "";
    this.children = [];
    this.listeners = new Map();
    this.classes = new Set();
    this.classList = {toggle: (name, enabled) => {
      if (enabled) this.classes.add(name); else this.classes.delete(name);
    }};
    this.fields = Array.from({length: 3}, () => ({textContent: ""}));
  }
  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }
  querySelectorAll(selector) {
    assert.equal(selector, "dd");
    return this.fields;
  }
  replaceChildren(...children) { this.children = children; }
  async fire(type = "click") {
    // Synthetic dispatch also exercises the handler guard for disabled controls.
    await Promise.all((this.listeners.get(type) || []).map((listener) =>
      listener({preventDefault() {}})));
  }
}

async function uiFixture() {
  const elements = new Map();
  for (const match of markup.matchAll(/<([a-z]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const element = new Element(match[1]);
    element.disabled = /\bdisabled\b/.test(match[2]);
    element.hidden = /\bhidden\b/.test(match[2]);
    element.value = /\bvalue="([^"]*)"/.exec(match[2])?.[1] || "";
    elements.set(match[3], element);
  }
  elements.get("goal").value = "Follow the first player.";
  const calls = [], plans = new Map(), intervals = [];
  const queue = (url, plan) => {
    const entries = plans.get(url) || [];
    entries.push(plan);
    plans.set(url, entries);
  };
  const fetch = async (url, options = {}) => {
    calls.push({url, options});
    const entries = plans.get(url);
    if (entries?.length) {
      const plan = entries.shift();
      return typeof plan === "function" ? plan() : plan;
    }
    if (url === "/api/session") return response({token: "fixture-session"});
    if (url === "/api/installation") {
      return response({game_directory: options.method === "PUT"
        ? JSON.parse(options.body).path : "C:\\old-game", bridge_installed: true});
    }
    if (url === "/api/status") return response(readyStatus());
    throw new Error(`Unexpected UI request: ${url}`);
  };
  const context = vm.createContext({document: {
    getElementById(id) {
      assert.ok(elements.has(id), `Missing DOM fixture for ${id}`);
      return elements.get(id);
    },
    createElement: (tag) => new Element(tag),
  }, fetch, setInterval: (callback) => { intervals.push(callback); return intervals.length; }});
  vm.runInContext(source, context, {filename: "companion/static/app.js"});
  for (let attempt = 0; attempt < 20 && elements.get("follow").disabled; attempt++) await turn();
  assert.equal(elements.get("follow").disabled, false, "Fixture must reach a ready UI");
  return {elements, calls, queue, intervals,
    fire: (id, type) => elements.get(id).fire(type)};
}

const actionControls = ["follow", "autonomous", "move-left", "move-right", "pay-coin",
  "open-coop", "save-model", "test-model", "save-directory", "detect-game",
  "launch-game"];

test("execution defaults preserve the main model without discovery, downloads or inference", async()=>{
  const ui=await uiFixture();
  assert.equal(ui.elements.get("execution-provider").value,"main");
  assert.equal(ui.elements.get("execution-fields").hidden,true);
  assert.equal(ui.elements.get("test-execution").disabled,false);
  assert.equal(ui.calls.some(call=>call.url.startsWith("/api/execution")),false);
});

test("unsaved Ollama draft survives polling and blocks start and connection test", async()=>{
  const ui=await uiFixture();
  ui.elements.get("execution-provider").value="ollama";
  await ui.fire("execution-provider","input");
  ui.elements.get("execution-model").value="draft:4b";
  await ui.fire("execution-model","input");
  await ui.fire("refresh");
  assert.equal(ui.elements.get("execution-provider").value,"ollama");
  assert.equal(ui.elements.get("execution-model").value,"draft:4b");
  assert.equal(ui.elements.get("execution-fields").hidden,false);
  assert.equal(ui.elements.get("autonomous").disabled,true);
  assert.equal(ui.elements.get("test-execution").disabled,true);
  const count=ui.calls.length;
  await ui.fire("test-execution");
  assert.equal(ui.calls.length,count);
});

test("Ollama discovery renders literal model names and never selects or starts a model", async()=>{
  const ui=await uiFixture();
  ui.elements.get("execution-provider").value="ollama";
  await ui.fire("execution-provider","input");
  ui.queue("/api/execution/models",response({models:[{name:"<fixture>:4b",parameter_size:"4B",quantization:"Q4_K_M"}],message:"read"}));
  await ui.fire("execution-models-refresh");
  const option=ui.elements.get("execution-installed").children[1];
  assert.equal(option.value,"<fixture>:4b");
  assert.match(option.textContent,/<fixture>:4b.*Q4_K_M/);
  assert.equal(ui.elements.get("execution-model").value,"");
  ui.elements.get("execution-installed").value="<fixture>:4b";
  await ui.fire("execution-installed","change");
  assert.equal(ui.elements.get("execution-model").value,"<fixture>:4b");
  assert.equal(ui.calls.filter(call=>call.url==="/api/execution/models").length,1);
  assert.equal(ui.calls.some(call=>call.url==="/api/execution/test"||call.url==="/api/control/start"),false);
});

test("saving optional local execution keeps chat settings and permits play without a main model", async()=>{
  const ui=await uiFixture();
  const status=readyStatus();
  status.model.configured=false;
  status.model.execution_configured=true;
  status.model.execution={provider:"ollama",endpoint:"http://127.0.0.1:11434",model:"fixture:4b",context_tokens:8192,timeout_seconds:60};
  ui.elements.get("execution-provider").value="ollama";
  await ui.fire("execution-provider","input");
  ui.elements.get("execution-model").value="fixture:4b";
  ui.queue("/api/execution",response(status.model));
  ui.queue("/api/status",response(status));
  await ui.fire("execution-form","submit");
  const request=ui.calls.find(call=>call.url==="/api/execution");
  const body=JSON.parse(request.options.body);
  assert.deepEqual(body,{provider:"ollama",endpoint:"http://127.0.0.1:11434",model:"fixture:4b",context_tokens:8192,timeout_seconds:60});
  assert.equal("api_key" in body,false);
  assert.equal(ui.elements.get("autonomous").disabled,false);
  assert.equal(ui.elements.get("test-execution").disabled,false);
  assert.match(ui.elements.get("execution-note").textContent,/聊天使用默认模型/);
});

test("execution selector and tests lock during active play and backend loss", async()=>{
  const ui=await uiFixture();
  const running=readyStatus();running.control.running=true;running.control.mode="autonomous";
  ui.queue("/api/status",response(running));await ui.fire("refresh");
  for(const id of ["execution-provider","save-execution","test-execution","execution-models-refresh"]) assert.equal(ui.elements.get(id).disabled,true);
  ui.queue("/api/status",()=>Promise.reject(new Error("connection lost")));await ui.fire("refresh");
  for(const id of ["execution-provider","save-execution","test-execution"]) assert.equal(ui.elements.get(id).disabled,true);
  assert.equal(ui.elements.get("stop").disabled,false);
});

test("local and compatible provider cache use is displayed separately with unknown data intact", async()=>{
  const ui=await uiFixture();const status=readyStatus();
  status.model.usage={routes:[
    {lane:"chat",provider:"main",model:"cloud",reported_requests:1,prompt_tokens:1000,completion_tokens:20,cache_hit_ratio:.8,cache_hit_tokens:800,cache_miss_tokens:200},
    {lane:"decision",provider:"ollama",model:"local:4b",reported_requests:1,prompt_tokens:400,completion_tokens:8,cache_hit_ratio:null},
  ]};
  ui.queue("/api/status",response(status));await ui.fire("refresh");
  assert.match(ui.elements.get("model-usage").textContent,/80\.0%.*800.*200/);
  assert.doesNotMatch(ui.elements.get("model-usage").textContent,/local:4b/);
  assert.match(ui.elements.get("execution-usage").textContent,/Ollama local:4b.*未返回缓存用量/);
});

function assertLocked(ui, ids = actionControls) {
  for (const id of ids) assert.equal(ui.elements.get(id).disabled, true, `${id} must stay locked`);
}

test("provider usage shows current process totals and leaves absent cache data unknown", async () => {
  const ui=await uiFixture();
  const value=readyStatus();
  value.model.usage={reported_requests:2,prompt_tokens:2000,completion_tokens:60,cache_hit_ratio:0.8};
  ui.queue("/api/status",response(value));
  await ui.fire("refresh");
  assert.match(ui.elements.get("model-usage").textContent,/80\.0%/);
  assert.match(ui.elements.get("model-usage").textContent,/本次启动/);
  value.model.usage.cache_hit_ratio=null;
  ui.queue("/api/status",response(value));
  await ui.fire("refresh");
  assert.match(ui.elements.get("model-usage").textContent,/未返回缓存用量/);
});

test("operation followed by lost backend cannot restore old ready controls", async () => {
  const ui = await uiFixture();
  ui.queue("/api/control/manual", response({status: "verified", message: "Moved"}));
  ui.queue("/api/status", () => Promise.reject(new Error("fixture network failure")));
  await ui.fire("move-left");
  assertLocked(ui);
  assert.equal(ui.elements.get("connection").textContent, "后台连接中断");
  const before = ui.calls.length;
  await ui.fire("follow");
  assert.equal(ui.calls.length, before, "Disconnected handler must not submit a stale start");
  await ui.fire("refresh");
  assert.equal(ui.elements.get("follow").disabled, false, "A new successful state may recover UI");
});

test("an outstanding refresh cannot unlock controls during or after shutdown", async () => {
  const ui = await uiFixture();
  const stateRequest = deferred(), shutdownRequest = deferred();
  ui.queue("/api/status", stateRequest.promise);
  const pendingRefresh = ui.fire("refresh");
  await turn();
  ui.queue("/api/shutdown", shutdownRequest.promise);
  const pendingShutdown = ui.fire("shutdown");
  await turn();
  assertLocked(ui, [...actionControls, "stop", "refresh", "shutdown"]);
  stateRequest.resolve(response(readyStatus()));
  await pendingRefresh;
  assertLocked(ui, [...actionControls, "stop", "refresh", "shutdown"]);
  shutdownRequest.resolve(response({message: "Stopped"}));
  await pendingShutdown;
  assertLocked(ui, [...actionControls, "stop", "refresh", "shutdown"]);
  const before = ui.calls.length;
  for (const callback of ui.intervals) await callback();
  assert.equal(ui.calls.length, before, "Polling must not resume after backend shutdown");
});

test("detected first directory and changed selection are the paths actually saved", async () => {
  const ui = await uiFixture();
  const candidates = [{path: "D:\\Steam\\game", bridge_installed: true},
    {path: "E:\\SteamLibrary\\game", bridge_installed: false}];
  ui.queue("/api/installation/detect", response({candidates}));
  await ui.fire("detect-game");
  assert.equal(ui.elements.get("game-candidates").hidden, false);
  assert.equal(ui.elements.get("game-candidates").value, candidates[0].path);
  assert.equal(ui.elements.get("game-directory").value, candidates[0].path);
  await ui.fire("save-directory");
  let saves = ui.calls.filter((call) => call.url === "/api/installation" && call.options.method === "PUT");
  assert.equal(JSON.parse(saves.at(-1).options.body).path, candidates[0].path);
  ui.elements.get("game-candidates").value = candidates[1].path;
  await ui.fire("game-candidates", "change");
  await ui.fire("save-directory");
  saves = ui.calls.filter((call) => call.url === "/api/installation" && call.options.method === "PUT");
  assert.equal(JSON.parse(saves.at(-1).options.body).path, candidates[1].path);
});

test("payment authorizes the displayed price at click time despite later state changes", async () => {
  const ui = await uiFixture();
  const payment = deferred();
  ui.queue("/api/control/manual", payment.promise);
  assert.match(ui.elements.get("pay-coin").textContent, /3/);
  await ui.fire("pay-coin");
  const submitted = ui.calls.find((call) => call.url === "/api/control/manual");
  assert.deepEqual(JSON.parse(submitted.options.body), {
    operation: "pay", target_id: "fixture-wall", max_coins: 3, currency: "coins",
  });
  ui.queue("/api/status", response(readyStatus(7)));
  await ui.fire("refresh");
  assert.match(ui.elements.get("pay-coin").textContent, /7/);
  assert.equal(JSON.parse(submitted.options.body).max_coins, 3);
  assert.equal(ui.calls.filter((call) => call.url === "/api/control/manual").length, 1);
  payment.resolve(response({status: "verified", message: "Paid"}));
  await turn();
});

test("movement settings allow sprint without adding a four-second idle interval", async () => {
  const ui = await uiFixture();
  ui.elements.get("allow-sprint").checked = true;
  await ui.fire("follow");
  const started = ui.calls.find(call=>call.url==="/api/control/start");
  assert.equal(JSON.parse(started.options.body).decision_interval_seconds,1);
  assert.equal(JSON.parse(started.options.body).allow_sprint,true);
  ui.elements.get("allow-sprint").checked = false;
  await ui.fire("move-right");
  const manual = ui.calls.find(call=>call.url==="/api/control/manual");
  assert.equal(JSON.parse(manual.options.body).sprint,false);
  assert.match(ui.elements.get("movement-detail").textContent,/更新至 0.4.0/);
});
