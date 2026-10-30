import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const state = { scenarios: [], scenario: null, program: "", setup: null, view: null, idx: 0, playing: false,
  selectedLine: null, added: new Set(), repairDone: null };

// ---------------------------------------------------------------- 3D scene
const vp = $("viewport");
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
vp.appendChild(renderer.domElement);
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(40, 1, 1, 5000);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
scene.add(new THREE.HemisphereLight(0xdfe8f2, 0x1a1f24, 1.1));
const sun = new THREE.DirectionalLight(0xffffff, 1.3);
sun.position.set(-120, 260, 160);
scene.add(sun);
const world = new THREE.Group();
scene.add(world);
const toolGroup = new THREE.Group();
scene.add(toolGroup);
let markers = [];

const V = (x, y, z) => new THREE.Vector3(x, z, -y); // machine work frame (Z up) -> three (Y up)

function resize() {
  const r = vp.getBoundingClientRect();
  renderer.setSize(r.width, r.height);
  camera.aspect = r.width / Math.max(r.height, 1);
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(vp);

let t0 = performance.now();
function loop(t) {
  requestAnimationFrame(loop);
  controls.update();
  const k = 1 + 0.25 * Math.sin((t - t0) / 180);
  markers.forEach((m) => m.scale.setScalar(k));
  if (state.playing && state.view) {
    const n = state.view.path.length;
    state.idx = Math.min(n - 1, state.idx + Math.max(1, Math.round(n / 900)));
    $("scrub").value = state.idx;
    placeTool(state.idx);
    if (state.idx >= n - 1) setPlaying(false);
  }
  renderer.render(scene, camera);
}
requestAnimationFrame(loop);

function clearWorld() {
  world.traverse((o) => { o.geometry?.dispose?.(); o.material?.dispose?.(); });
  world.clear();
  markers = [];
}

function buildScene(view) {
  clearWorld();
  const s = view.stock, g = view.grid, H = view.heights;
  const depth = Math.max(1e-6, s.z1 - s.z0);
  // material height map
  const pos = new Float32Array(g.nx * g.ny * 3), col = new Float32Array(g.nx * g.ny * 3);
  const top = new THREE.Color(0xc3ccd6), deep = new THREE.Color(0x2f6fa8), c = new THREE.Color();
  for (let j = 0; j < g.ny; j++) for (let i = 0; i < g.nx; i++) {
    const k = j * g.nx + i, h = H[k];
    const p = V(g.x0 + (i + 0.5) * g.dx, g.y0 + (j + 0.5) * g.dy, h);
    pos.set([p.x, p.y, p.z], k * 3);
    c.copy(top).lerp(deep, Math.min(1, Math.pow((s.z1 - h) / depth, 0.6)));
    col.set([c.r, c.g, c.b], k * 3);
  }
  const idx = [];
  for (let j = 0; j < g.ny - 1; j++) for (let i = 0; i < g.nx - 1; i++) {
    const a = j * g.nx + i, b = a + 1, d = a + g.nx, e = d + 1;
    idx.push(a, d, b, b, d, e);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  geo.setAttribute("color", new THREE.BufferAttribute(col, 3));
  geo.setIndex(idx);
  geo.computeVertexNormals();
  world.add(new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.55, metalness: 0.35, side: THREE.DoubleSide })));
  // stock walls + outline
  const box = new THREE.BoxGeometry(s.x1 - s.x0, depth, s.y1 - s.y0);
  const wall = new THREE.Mesh(box, new THREE.MeshStandardMaterial({ color: 0x8e99a5, transparent: true, opacity: 0.18, depthWrite: false }));
  wall.position.copy(V((s.x0 + s.x1) / 2, (s.y0 + s.y1) / 2, (s.z0 + s.z1) / 2));
  world.add(wall);
  const edges = new THREE.LineSegments(new THREE.EdgesGeometry(box), new THREE.LineBasicMaterial({ color: 0x5b6672 }));
  edges.position.copy(wall.position);
  world.add(edges);
  // fixtures
  for (const f of view.fixtures) {
    const isTable = /table/i.test(f.name);
    const bg = new THREE.BoxGeometry(f.x1 - f.x0, f.z1 - f.z0, f.y1 - f.y0);
    const m = new THREE.Mesh(bg, new THREE.MeshStandardMaterial({ color: isTable ? 0x232a31 : 0xff8a3d, transparent: true,
      opacity: isTable ? 0.9 : 0.6, roughness: 0.6, metalness: 0.2, depthWrite: isTable }));
    m.position.copy(V((f.x0 + f.x1) / 2, (f.y0 + f.y1) / 2, (f.z0 + f.z1) / 2));
    world.add(m);
    if (!isTable) {
      const e = new THREE.LineSegments(new THREE.EdgesGeometry(bg), new THREE.LineBasicMaterial({ color: 0xffb27a }));
      e.position.copy(m.position);
      world.add(e);
    }
  }
  // toolpath
  const feed = [], rapid = [];
  const ceil = Math.max(s.z1, ...view.fixtures.map((f) => f.z1)) + 40; // hide long moves to the tool-change height
  for (const p of view.path) {
    const arr = p.kind === "rapid" ? rapid : feed;
    if (p.a[2] > ceil && p.b[2] > ceil) continue;
    const a = V(p.a[0], p.a[1], Math.min(p.a[2], ceil)), b = V(p.b[0], p.b[1], Math.min(p.b[2], ceil));
    arr.push(a.x, a.y, a.z, b.x, b.y, b.z);
  }
  const mk = (arr, color, opacity) => {
    const lg = new THREE.BufferGeometry();
    lg.setAttribute("position", new THREE.Float32BufferAttribute(arr, 3));
    return new THREE.LineSegments(lg, new THREE.LineBasicMaterial({ color, transparent: true, opacity }));
  };
  world.add(mk(feed, 0x4cc2ff, 0.85));
  world.add(mk(rapid, 0xf2b33a, 0.45));
  // findings
  for (const is of view.issues) {
    if (!is.at || !["crash", "scrap", "alarm"].includes(is.severity)) continue;
    const m = new THREE.Mesh(new THREE.SphereGeometry(3, 20, 14), new THREE.MeshBasicMaterial({ color: is.severity === "scrap" ? 0xff7a45 : 0xef4a50, depthTest: false }));
    m.renderOrder = 10;
    m.position.copy(V(...is.at));
    world.add(m);
    markers.push(m);
  }
}

function buildTool(t) {
  toolGroup.clear();
  if (!t) return;
  const add = (r, h, y0, color, opacity = 1) => {
    const m = new THREE.Mesh(new THREE.CylinderGeometry(r, r, h, 28), new THREE.MeshStandardMaterial({ color, metalness: 0.6, roughness: 0.35, transparent: opacity < 1, opacity }));
    m.position.y = y0 + h / 2;
    toolGroup.add(m);
  };
  add(t.diameter / 2, t.flute_length, 0, 0xe8edf2);
  add(t.diameter / 2 * 0.98, Math.max(0.1, t.stickout - t.flute_length), t.flute_length, 0xaab4be);
  add(t.holder_diameter / 2, Math.max(1, t.length - t.stickout), t.stickout, 0x56606b, 0.85);
}

function placeTool(i) {
  const v = state.view;
  if (!v || !v.path.length) return;
  const p = v.path[Math.max(0, Math.min(i, v.path.length - 1))];
  if (toolGroup.userData.tool !== p.tool) {
    buildTool(v.tools[p.tool]);
    toolGroup.userData.tool = p.tool;
  }
  toolGroup.position.copy(V(...p.b));
  $("posLabel").textContent = `line ${p.line} · T${p.tool} · ${p.kind}`;
  markLine(p.line, "cur");
}

function fitCamera(view, mode = "iso") {
  const s = view.stock;
  const c = V((s.x0 + s.x1) / 2, (s.y0 + s.y1) / 2, s.z0 + (s.z1 - s.z0) * 0.6);
  const span = Math.max(s.x1 - s.x0, s.y1 - s.y0) * 1.5;
  const dirs = { iso: [0.9, 1.0, 1.25], top: [0, 1, 0.001], front: [0, 0.35, 1] };
  const d = new THREE.Vector3(...dirs[mode]).normalize().multiplyScalar(span * 1.35);
  camera.position.copy(c.clone().add(d));
  controls.target.copy(c);
  controls.update();
}
document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => state.view && fitCamera(state.view, b.dataset.view)));

function setPlaying(on) {
  state.playing = on;
  $("playBtn").textContent = on ? "❚❚" : "▶";
  if (on && state.view && state.idx >= state.view.path.length - 1) state.idx = 0;
}
$("playBtn").addEventListener("click", () => setPlaying(!state.playing));
$("scrub").addEventListener("input", (e) => { setPlaying(false); state.idx = +e.target.value; placeTool(state.idx); });

// ---------------------------------------------------------------- panels
function renderScenarios() {
  $("scenarios").innerHTML = state.scenarios.map((s) =>
    `<button class="scn ${state.scenario?.id === s.id ? "active" : ""}" data-id="${s.id}"><span>${esc(s.title)}</span><small>${s.id === "clean" ? "baseline" : "mistake"}</small></button>`).join("");
  $("scenarios").querySelectorAll(".scn").forEach((b) => b.addEventListener("click", () => selectScenario(b.dataset.id)));
}

function selectScenario(id) {
  const s = state.scenarios.find((x) => x.id === id);
  state.scenario = s;
  state.program = s.program;
  state.setup = structuredClone(s.setup);
  state.added = new Set();
  $("story").textContent = s.story;
  $("programInput").value = s.program;
  renderScenarios();
  renderSetup();
  run();
}

function renderSetup() {
  const s = state.setup;
  const rows = Object.entries(s.tools).map(([n, t]) => {
    const cam = s.cam_tools?.[n];
    const differs = cam && (Math.abs(cam.diameter - t.diameter) > 0.01 || cam.kind !== t.kind);
    const h = s.h_table?.[n] ?? t.length;
    return `<tr class="${differs ? "diff" : ""}" title="${esc(t.description || "")}${differs ? ` — CAM expects ${cam.kind} D${cam.diameter}` : ""}">
      <td class="t">T${n}</td><td>${esc(t.kind)}</td>
      <td><input data-t="${n}" data-f="diameter" value="${t.diameter}" inputmode="decimal"></td>
      <td><input data-t="${n}" data-f="length" value="${t.length}" inputmode="decimal"></td>
      <td><input data-t="${n}" data-f="h" value="${h}" inputmode="decimal"></td></tr>`;
  }).join("");
  $("toolTable").innerHTML = `<tr><th>Tool</th><th>Type</th><th>Ø mm</th><th>Length</th><th>H reg.</th></tr>${rows}`;
  $("toolTable").querySelectorAll("input").forEach((inp) => inp.addEventListener("change", () => {
    const v = parseFloat(inp.value);
    if (Number.isNaN(v)) return;
    const n = inp.dataset.t, f = inp.dataset.f;
    s.h_table = s.h_table || {};
    if (f === "h") s.h_table[n] = v;
    else s.tools[n][f] = v;
    if (f === "length" && s.h_table[n] === undefined) s.h_table[n] = v;
    inp.classList.add("changed");
  }));
  $("fixtureList").innerHTML = s.fixtures.map((f) => `<div><b>■</b> ${esc(f.name)} <span>Z ${f.z0}…${f.z1}</span></div>`).join("");
}

$("useProgram").addEventListener("click", () => {
  state.program = $("programInput").value;
  state.scenario = { ...state.scenario, id: null };
  run();
});

async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

async function run() {
  $("runBtn").disabled = true;
  $("verdictSub").textContent = "Simulating on the real setup…";
  try {
    const view = await post("/api/analyze", { program: state.program, setup: state.setup });
    showView(view);
  } catch (e) {
    $("verdictSub").textContent = "Error: " + e.message;
  } finally {
    $("runBtn").disabled = false;
  }
}
$("runBtn").addEventListener("click", run);

function showView(view, keepCamera = false) {
  state.view = view;
  $("repairBox").hidden = true;
  $("briefBox").hidden = true;
  const v = $("verdict");
  v.className = "verdict " + (view.verdict === "GO" ? "go" : "stop");
  $("verdictBig").textContent = view.verdict;
  const blocking = view.issues.filter((i) => ["crash", "scrap", "alarm"].includes(i.severity));
  const firstStop = blocking.filter((i) => i.line).sort((a, b) => a.line - b.line)[0] || blocking[0];
  $("verdictSub").textContent = view.verdict === "GO"
    ? `All ${view.checks.length} checks pass on this setup · ${view.stats.moves} moves simulated in ${view.stats.sim_ms} ms`
    : `Fails ${firstStop.line ? "at line " + firstStop.line : "on the finished part"}: ${firstStop.message} Do not press Cycle Start.`;
  $("repairBtn").disabled = view.verdict === "GO";
  $("briefBtn").disabled = false;
  $("certBtn").disabled = false;
  renderFindings(view);
  renderChecks(view);
  renderCode(view);
  buildScene(view);
  if (!keepCamera) fitCamera(view);
  $("scrub").max = Math.max(0, view.path.length - 1);
  const first = view.issues.find((i) => i.at && ["crash", "scrap", "alarm"].includes(i.severity));
  state.idx = first ? Math.max(0, view.path.findIndex((p) => p.line === first.line)) : 0;
  $("scrub").value = state.idx;
  toolGroup.userData.tool = null;
  placeTool(state.idx);
}

const CAUSES = new Set(["H_MISMATCH", "TLO_OFFSET_ERROR", "NO_TLO", "TOOL_MISMATCH", "WCS_NOT_SET", "H_NOT_SET", "UNSUPPORTED"]);
const BLOCK = ["crash", "scrap", "alarm"];

function renderFindings(view) {
  const items = view.issues.filter((i) => i.severity !== "info");
  if (!items.length) { $("findings").innerHTML = `<p class="empty">Nothing found. The program is safe to run on this setup.</p>`; return; }
  const blocking = items.filter((i) => BLOCK.includes(i.severity));
  const first = blocking.filter((i) => i.line).sort((a, b) => a.line - b.line)[0] || blocking[0];
  const causes = items.filter((i) => i !== first && CAUSES.has(i.code));
  const rest = items.filter((i) => i !== first && !causes.includes(i));
  const card = (i) => `<div class="finding ${i.severity}" data-k="${items.indexOf(i)}">
    <div class="h"><span>${i.line ? "line " + i.line : "whole part"} · ${esc(i.code)}</span><span class="sev ${i.severity}">${i.severity.toUpperCase()}</span></div>
    <div class="m">${esc(i.message)}</div></div>`;
  let html = "";
  if (first) html += `<div class="group">First failure</div>${card(first)}`;
  if (causes.length) html += `<div class="group">Likely cause</div>${causes.map(card).join("")}`;
  if (rest.length) html += `<details class="more" ${first ? "" : "open"}><summary>${rest.length} more finding${rest.length > 1 ? "s" : ""}${first ? " after the first failure" : ""}</summary>${rest.map(card).join("")}</details>`;
  $("findings").innerHTML = html;
  $("findings").querySelectorAll(".finding").forEach((el) => el.addEventListener("click", () => focusIssue(items[+el.dataset.k])));
}

function focusIssue(i) {
  setPlaying(false);
  if (i.line) {
    const k = state.view.path.findIndex((p) => p.line === i.line);
    if (k >= 0) { state.idx = k; $("scrub").value = k; placeTool(k); }
    scrollToLine(i.line);
  }
  if (i.at) {
    const p = V(...i.at);
    const off = camera.position.clone().sub(controls.target).normalize().multiplyScalar(110);
    controls.target.copy(p);
    camera.position.copy(p.clone().add(off));
  }
}

function renderChecks(view) {
  $("checks").innerHTML = view.checks.map((c) => `<li><span class="${c.pass ? "ok" : "no"}">${c.pass ? "✓" : "✕"}</span>
    <span>${esc(c.label)} ${c.pass ? "" : `<small>line ${c.lines.join(", ")}</small>`}</span></li>`).join("");
}

function renderCode(view) {
  const sev = {};
  const rank = { crash: 0, alarm: 1, scrap: 2, warning: 3 };
  for (const i of view.issues) if (i.line && (sev[i.line] === undefined || rank[i.severity] < rank[sev[i.line]])) sev[i.line] = i.severity;
  $("code").innerHTML = view.lines.map((l, k) => {
    const n = k + 1;
    const body = esc(l).replace(/(\([^)]*\))/g, '<span class="cm">$1</span>');
    const cls = [sev[n] || "", state.added.has(l) ? "added" : ""].join(" ");
    return `<div class="l ${cls}" data-n="${n}"><span class="n">${n}</span>${body || " "}</div>`;
  }).join("");
  $("code").querySelectorAll(".l").forEach((el) => el.addEventListener("click", () => {
    state.selectedLine = +el.dataset.n;
    const k = state.view.path.findIndex((p) => p.line === state.selectedLine);
    if (k >= 0) { setPlaying(false); state.idx = k; $("scrub").value = k; placeTool(k); }
  }));
  $("code").querySelectorAll(".l").forEach((el) => el.addEventListener("dblclick", () => restartAt(+el.dataset.n)));
}

let curEl = null;
function markLine(n) {
  if (curEl) curEl.classList.remove("cur");
  curEl = $("code").querySelector(`.l[data-n="${n}"]`);
  if (curEl) { curEl.classList.add("cur"); if (state.playing) curEl.scrollIntoView({ block: "nearest" }); }
}
function scrollToLine(n) { $("code").querySelector(`.l[data-n="${n}"]`)?.scrollIntoView({ block: "center", behavior: "smooth" }); }

// ---------------------------------------------------------------- repair loop
$("repairBtn").addEventListener("click", () => repair(false));

async function repair(live) {
  $("repairBox").hidden = false;
  $("repairLog").innerHTML = "";
  $("repairResult").innerHTML = "";
  $("repairTag").textContent = "";
  $("repairBtn").disabled = true;
  $("repairBox").scrollIntoView({ behavior: "smooth", block: "start" });
  const body = { program: state.program, setup: state.setup, scenario: state.scenario?.id && !setupEdited() ? state.scenario.id : null };
  try {
    const r = await fetch(`/api/repair${live ? "?live=true" : ""}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let k;
      while ((k = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, k); buf = buf.slice(k + 2);
        if (chunk.startsWith("data: ")) onRepairEvent(JSON.parse(chunk.slice(6)));
      }
    }
  } catch (e) {
    $("repairResult").innerHTML = `<div class="proof fail"><h3>Repair unavailable</h3>${esc(e.message)}</div>`;
  } finally {
    $("repairBtn").disabled = state.view?.verdict === "GO";
  }
}

function setupEdited() { return !!document.querySelector(".tools input.changed"); }

function onRepairEvent(ev) {
  if (ev.type === "meta") {
    $("repairTag").textContent = `${ev.model} · Nebius Token Factory · run ${ev.recorded_utc}`;
  } else if (ev.type === "round") {
    $("repairLog").insertAdjacentHTML("beforeend", `<p class="hint">Round ${ev.round}: asking Nemotron for 3 candidate fixes for ${ev.issues} finding(s)…</p>`);
  } else if (ev.type === "candidate") {
    const edits = (ev.edits || []).map((e) => `${e.op} line ${e.line}${e.text ? ": " + e.text : ""}`).join("\n");
    let why = "";
    if (ev.result === "rejected") {
      why = ev.intent && !ev.intent.preserved ? `changes the machined shape (${ev.intent.cells_changed} cells, up to ${ev.intent.max_dev_mm} mm)`
        : `still blocked: ${(ev.remaining || []).slice(0, 3).map((i) => `line ${i.line} ${i.code}`).join(", ")}`;
    } else if (ev.result === "proved") {
      why = `no findings · shape deviation ${ev.intent.max_dev_mm} mm`;
    } else if (ev.error) why = ev.error;
    $("repairLog").insertAdjacentHTML("beforeend", `<div class="cand">
      <div class="h"><span>#${ev.candidate} · T=${ev.temperature} · ${ev.latency_s}s${ev.tokens?.out ? ` · ${ev.tokens.in}→${ev.tokens.out} tok` : ""}</span><span class="chip ${ev.result}">${(ev.result || "").replace("_", " ").toUpperCase()}</span></div>
      ${ev.diagnosis ? `<div class="d">${esc(ev.diagnosis)}</div>` : ""}
      ${edits ? `<pre>${esc(edits)}</pre>` : ""}
      ${(ev.setup_changes || []).length ? `<pre>${esc(ev.setup_changes.map((c) => `setup ${c.path} = ${c.value}`).join("\n"))}</pre>` : ""}
      ${(ev.setup_actions || []).length ? `<div class="d">Setup: ${esc(ev.setup_actions.join(" · "))}</div>` : ""}
      ${why ? `<div class="hint">Simulator: ${esc(why)}</div>` : ""}</div>`);
  } else if (ev.type === "done") {
    state.repairDone = ev;
    if (ev.status === "proved") {
      const p = ev.proof;
      const diff = (ev.diff || "").split("\n").map((l) =>
        `<span class="${l.startsWith("+++") || l.startsWith("---") || l.startsWith("@@") ? "hd" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : ""}">${esc(l)}</span>`).join("\n");
      const sc = ev.setup_changes || [];
      const codeChanged = p.changed_lines > 0;
      const title = sc.length && !codeChanged ? "Setup correction proved — the program was right"
        : sc.length ? "Program and setup fix proved" : "Program fix proved by simulation";
      const scHtml = sc.length ? `<p class="hint">Change at the machine:</p><ul>${(ev.setup_actions || []).map((a) => `<li>${esc(a)}</li>`).join("")}</ul>
        <table class="cert">${sc.map((c) => `<tr><td>${esc(c.name ? c.name + " " + c.path.split(".").pop() : c.path)}</td><td>${esc(JSON.stringify(c.from))}</td><td class="to">→ ${esc(JSON.stringify(c.to))}</td></tr>`).join("")}</table>` : "";
      $("repairResult").innerHTML = `<div class="proof"><h3>${title}</h3>${scHtml}
        <dl class="stat"><dt>Findings after fix</dt><dd>0 blocking</dd>
        <dt>Shape vs. program intent</dt><dd>max deviation ${p.intent.max_dev_mm} mm, ${p.intent.cells_changed} cells changed</dd>
        <dt>Lines changed</dt><dd>${p.changed_lines}</dd>
        ${p.intent.geometry_change ? `<dt>Note</dt><dd>corrects ${p.intent.cells_corrected} cells that the original cut below the part — needs programmer approval</dd>` : ""}</dl>
        ${codeChanged ? `<pre class="diff">${diff}</pre>` : ""}
        <button class="btn primary" id="loadProved">Load the proved ${sc.length && !codeChanged ? "setup" : "program"}</button></div>`;
      $("loadProved").addEventListener("click", () => {
        state.added = new Set((ev.diff || "").split("\n").filter((l) => l.startsWith("+") && !l.startsWith("+++")).map((l) => l.slice(1)));
        state.program = ev.program;
        if (ev.setup) { state.setup = ev.setup; renderSetup(); }
        showView(ev.view, true);
      });
    } else {
      const acts = (ev.setup_actions || []).map((a) => `<li>${esc(a)}</li>`).join("");
      $("repairResult").innerHTML = `<div class="proof fail"><h3>${ev.status === "no_key" ? "Model not connected" : "No fix could be proved"}</h3>
        <p>${esc(ev.message || "")}</p>${acts ? `<p>Suggested setup corrections (not proved):</p><ul>${acts}</ul>` : ""}</div>`;
    }
  }
}

// ---------------------------------------------------------------- briefing, certificate, restart
$("briefBtn").addEventListener("click", async () => {
  const box = $("briefBox");
  box.hidden = false;
  box.textContent = "Nemotron is writing the operator briefing…";
  try {
    const r = await post("/api/explain", { program: state.program, setup: state.setup });
    box.textContent = r.status === "ok" ? `${r.text}\n\n— ${r.model}, ${r.latency_s}s` : `Briefing unavailable (${r.status}${r.error ? ": " + r.error : ""}).`;
  } catch (e) { box.textContent = "Briefing unavailable: " + e.message; }
});

function openModal(html) { $("modalBody").innerHTML = html; $("modal").showModal(); }
$("printBtn").addEventListener("click", () => window.print());

$("certBtn").addEventListener("click", async () => {
  const c = await post("/api/certificate", { program: state.program, setup: state.setup });
  const rows = c.checks.map((k) => `<tr><td>${k.pass ? "✓" : "✕"}</td><td>${esc(k.label)}</td><td>${k.pass ? "" : "line " + k.lines.join(", ")}</td></tr>`).join("");
  const tools = Object.entries(c.tools).map(([n, d]) => `<tr><td>T${n}</td><td colspan="2">${esc(d)}</td></tr>`).join("");
  openModal(`<div class="cert"><h3>CrashProof run certificate</h3>
    <div class="v ${c.verdict === "GO" ? "go" : "stop"}">${c.verdict === "GO" ? "GO — cleared for Cycle Start" : "STOP — do not run"}</div>
    <p class="hint">${esc(c.setup_name)} · issued ${esc(c.issued_utc)}</p>
    <p class="hash">program sha256 ${c.program_sha256}<br>setup sha256 ${c.setup_sha256}</p>
    <table>${rows}</table>
    <table><tr><td colspan="3"><b>Setup this verdict is valid for</b></td></tr>${tools}
    <tr><td>WCS</td><td colspan="2">${esc(JSON.stringify(c.wcs))}</td></tr>
    <tr><td>Sim</td><td colspan="2">${esc(c.simulation.method)}, ${c.simulation.resolution_mm} mm grid, ${c.simulation.moves} moves</td></tr></table>
    <p class="hint">${c.limits.map(esc).join("<br>")}</p></div>`);
});

async function restartAt(line) {
  try {
    const r = await post("/api/restart", { program: state.program, setup: state.setup, line });
    const head = r.program.split("\n").slice(0, r.header_lines + 3).join("\n");
    openModal(`<div class="cert"><h3>Safe restart at line ${line}</h3>
      <div class="v ${r.proved ? "go" : "stop"}">${r.proved ? "Proved against the material left at this point" : "Restart is not safe"}</div>
      <p class="hint">Rebuilt state: T${r.state.tool}, H${r.state.h}, ${r.state.wcs}, S${r.state.rpm} ${r.state.spindle_on ? "M3" : "spindle off"}, F${r.state.feed}. Approach from Z${r.approach_z} above the remaining material.</p>
      <pre>${esc(head)}\n…</pre></div>`);
  } catch (e) {
    openModal(`<div class="cert"><h3>Restart at line ${line}</h3><p>${esc(e.message)}</p></div>`);
  }
}

// ---------------------------------------------------------------- digital twin: control photo + catalog lookup
$("screenBtn").addEventListener("click", () => $("screenFile").click());
$("screenFile").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (f) readScreen(new Uint8Array(await f.arrayBuffer()), f.type || "image/jpeg", URL.createObjectURL(f));
});
$("sampleScreenBtn").addEventListener("click", async () => {
  const r = await fetch("/api/sample-screen");
  const buf = new Uint8Array(await r.arrayBuffer());
  readScreen(buf, "image/png", "/api/sample-screen");
});

function b64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

async function readScreen(bytes, mime, src) {
  const box = $("screenResult");
  box.innerHTML = `<img src="${src}" alt="Control screen photo"><div class="card">Reading the registers…</div>`;
  try {
    const r = await post("/api/read-screen", { image_b64: b64(bytes), mime, setup: state.setup });
    if (r.status !== "ok") { box.querySelector(".card").textContent = `Not available (${r.status}${r.message ? ": " + r.message : ""}).`; return; }
    const ch = r.changes || [];
    const unread = (r.data.unreadable || []).join(", ");
    box.querySelector(".card").innerHTML = `<b>${esc(r.model)}</b> · ${r.latency_s}s<br>
      ${ch.length ? `The control differs from the setup sheet:` : "Every register matches the setup sheet."}
      ${ch.length ? `<table>${ch.map((c) => `<tr><td>${esc(c.field)}</td><td>${esc(JSON.stringify(c.from))}</td><td class="to">→ ${esc(JSON.stringify(c.to))}</td></tr>`).join("")}</table>
      <button class="btn primary small" id="applyScreen">Use the control's values and re-prove</button>` : ""}
      ${unread ? `<div class="hint">Could not read: ${esc(unread)}</div>` : ""}`;
    $("applyScreen")?.addEventListener("click", () => {
      for (const c of ch) {
        let o = state.setup;
        for (const k of c.path.slice(0, -1)) o = o[k] = o[k] || {};
        o[c.path[c.path.length - 1]] = c.to;
      }
      renderSetup();
      ch.forEach((c) => { const m = c.field.match(/^H(\d+)$/); if (m) document.querySelector(`.tools input[data-t="${m[1]}"][data-f="h"]`)?.classList.add("changed"); });
      run();
    });
  } catch (e) { box.querySelector(".card").textContent = "Error: " + e.message; }
}

$("lookupBtn").addEventListener("click", async () => {
  const q = $("lookupQuery").value.trim();
  if (!q) return;
  const box = $("lookupResult");
  box.innerHTML = `<div class="card">Searching catalogs with Tavily…</div>`;
  try {
    const r = await post("/api/tool-lookup", { query: q });
    if (!r.tool) { box.innerHTML = `<div class="card">No verified dimensions (${esc(r.status)}${r.message ? ": " + esc(r.message) : ""}).<div class="src">${(r.sources || []).map(esc).join("<br>")}</div></div>`; return; }
    const t = r.tool;
    const ok = r.status === "verified";
    box.innerHTML = `<div class="card"><b>${ok ? "Verified" : "Not verified"}</b> · ${esc(r.model)} · ${r.latency_s}s
      <table><tr><td>type</td><td>${esc(t.kind)}</td></tr><tr><td>Ø</td><td>${t.diameter} mm</td></tr><tr><td>flute length</td><td>${t.flute_length} mm</td></tr>
      <tr><td>shank</td><td>${t.shank_diameter} mm</td></tr><tr><td>overall</td><td>${t.overall_length} mm</td></tr></table>
      <blockquote>${esc(r.extracted.evidence || "")}</blockquote>
      <div class="src">${ok ? "Quote found verbatim in" : "Quote NOT found in"} ${esc(r.extracted.source_url || "")}</div>
      ${ok ? `<div class="row" style="margin-top:6px">Apply to <select id="lookupT">${Object.keys(state.setup.tools).map((n) => `<option>${n}</option>`).join("")}</select>
      <button class="btn ghost small" id="lookupApply">Apply</button></div>` : ""}</div>`;
    $("lookupApply")?.addEventListener("click", () => {
      const tool = state.setup.tools[$("lookupT").value];
      if (t.diameter) tool.diameter = t.diameter;
      if (t.flute_length) tool.flute_length = t.flute_length;
      if (t.shank_diameter) tool.shank_diameter = t.shank_diameter;
      if (t.kind && ["flat", "ball", "drill"].includes(t.kind)) tool.kind = t.kind;
      tool.description = q;
      renderSetup();
      run();
    });
  } catch (e) { box.innerHTML = `<div class="card">Error: ${esc(e.message)}</div>`; }
});

// ---------------------------------------------------------------- boot
(async function init() {
  try {
    const st = await (await fetch("/api/status")).json();
    $("status").className = "status " + (st.llm ? "on" : "off");
    $("statusText").textContent = st.llm ? `Nemotron via Nebius Token Factory · ${st.fix_model.split("/").pop()}` : "Model offline · recorded runs only";
  } catch { $("statusText").textContent = "Status unavailable"; }
  state.scenarios = await (await fetch("/api/scenarios")).json();
  renderScenarios();
  selectScenario(state.scenarios.find((s) => s.id === "g99") ? "g99" : state.scenarios[0].id);
})();
