/**
 * End-to-end browser verification for a RuiC-card-skill viewer.
 *
 * references/verification.md asks for the running page to be driven, not merely
 * fetched: drag in both directions, wheel zoom, flip, reset, the keyboard, every
 * depth slider plus foil and the finish swatches, the screenshot download, a
 * ~390 px layout and the reduced-motion path. This script does exactly that over
 * the Chrome DevTools Protocol and compares REAL captured frames, so "the slider
 * moved its own readout" never counts as proof. `window.__holo` is used only for
 * state, never as evidence that the shaders compiled.
 *
 * It launches its own headless Chromium-family browser and, when handed a
 * project directory, its own viewer server — no manual setup, nothing left
 * running except when --keep-server is passed.
 *
 * Usage:
 *   node scripts/verify_web.mjs <project-dir | url> [--out DIR] [--browser PATH]
 *                               [--only desktop|mobile] [--keep-server]
 *
 * Exit code is 0 only when every check passed. Screenshots and report.json land
 * in <project>/verification by default.
 */
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const sha = (buf) => createHash("sha256").update(buf).digest("hex").slice(0, 16);

// ---------------------------------------------------------------- arguments
const argv = process.argv.slice(2);
const flag = (name, fallback = null) => {
  const i = argv.indexOf(name);
  return i >= 0 ? (argv[i + 1] ?? true) : fallback;
};
const positionals = [];
for (let i = 0; i < argv.length; i++) {
  if (["--out", "--browser", "--only"].includes(argv[i])) {
    i += 1;
    continue;
  }
  if (argv[i].startsWith("--")) continue;
  positionals.push(argv[i]);
}
const target = positionals[0];
if (!target) {
  console.error("usage: node scripts/verify_web.mjs <project-dir | url> [--out DIR] [--browser PATH] [--only desktop|mobile] [--keep-server]");
  process.exit(2);
}
const only = flag("--only");
const keepServer = argv.includes("--keep-server");
const isDir = existsSync(target);
const project = isDir ? path.resolve(target) : null;
const outDir = path.resolve(flag("--out", project ? path.join(project, "verification") : process.cwd()));
const downloadDir = mkdtempSync(path.join(tmpdir(), "holo-dl-"));
mkdirSync(outDir, { recursive: true });

// ---------------------------------------------------------------- browser
const BROWSERS = [
  process.env.RUIC_BROWSER,
  flag("--browser"),
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
  "msedge",
  "google-chrome",
  "google-chrome-stable",
  "chromium",
  "chromium-browser",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
].filter(Boolean);
const browser = BROWSERS.find((p) => (p.includes("/") || p.includes("\\") ? existsSync(p) : true));
if (!browser) {
  console.error("No Chromium-family browser found. Pass --browser <path-to-msedge-or-chrome>.");
  process.exit(2);
}

const freePort = () =>
  new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });

const waitForHttp = async (url, timeout = 20000) => {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    try {
      const r = await fetch(url);
      if (r.ok) return true;
    } catch {}
    await sleep(250);
  }
  return false;
};

// ---------------------------------------------------------------- CDP client
class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.seq = 0;
    this.pending = new Map();
    this.consoleErrors = [];
    this.failedRequests = [];
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
        return;
      }
      if (msg.method === "Runtime.exceptionThrown") this.consoleErrors.push("exception: " + (msg.params?.exceptionDetails?.text || "?"));
      if (msg.method === "Log.entryAdded" && msg.params?.entry?.level === "error") this.consoleErrors.push("log: " + msg.params.entry.text);
      if (msg.method === "Runtime.consoleAPICalled" && msg.params?.type === "error")
        this.consoleErrors.push("console: " + (msg.params.args || []).map((a) => a.value ?? a.description).join(" "));
      if (msg.method === "Network.loadingFailed" && !msg.params?.canceled) this.failedRequests.push(`${msg.params?.type} ${msg.params?.errorText}`);
    });
  }
  send(method, params = {}) {
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async eval(expression) {
    const r = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text + " " + JSON.stringify(r.result?.value));
    return r.result.value;
  }
  async shot(tag, out) {
    const { data } = await this.send("Page.captureScreenshot", { format: "png" });
    const buf = Buffer.from(data, "base64");
    writeFileSync(path.join(out, `${tag}.png`), buf);
    return sha(buf);
  }
}

/** Launch a headless instance and attach to its page target. */
async function launch({ width, height, extraFlags = [] }, out) {
  const port = await freePort();
  const profile = mkdtempSync(path.join(tmpdir(), "holo-profile-"));
  const child = spawn(
    browser,
    [
      "--headless=new",
      "--enable-unsafe-swiftshader",
      "--disable-gpu",
      "--hide-scrollbars",
      "--no-first-run",
      "--no-default-browser-check",
      `--window-size=${width},${height}`,
      `--user-data-dir=${profile}`,
      `--remote-debugging-port=${port}`,
      ...extraFlags,
      "about:blank",
    ],
    { stdio: "ignore", detached: false },
  );
  let targetInfo = null;
  for (let i = 0; i < 60; i++) {
    await sleep(500);
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      targetInfo = list.find((t) => t.type === "page");
      if (targetInfo) break;
    } catch {}
  }
  if (!targetInfo) throw new Error("headless browser did not expose a page target");
  const ws = new WebSocket(targetInfo.webSocketDebuggerUrl);
  await new Promise((r, j) => {
    ws.addEventListener("open", r, { once: true });
    ws.addEventListener("error", j, { once: true });
  });
  const cdp = new Cdp(ws);
  for (const d of ["Page", "Runtime", "Log", "Network", "DOM"]) await cdp.send(`${d}.enable`);
  const close = () => {
    try { ws.close(); } catch {}
    try { child.kill("SIGKILL"); } catch {}
    try { rmSync(profile, { recursive: true, force: true }); } catch {}
  };
  return { cdp, close, port };
}

const waitReady = async (cdp, timeout = 30000) => {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const st = await cdp.eval(`(()=>{const h=window.__holo;return h?{ready:!!h.ready,fallback:!!h.fallback3d,error:h.error||null}:null})()`);
    if (st && (st.ready || st.fallback)) return st;
    await sleep(250);
  }
  throw new Error("viewer never became ready");
};

const same = (a, b) => a === b;

// ---------------------------------------------------------------- desktop pass
async function desktopPass(base, out) {
  const checks = [];
  let errors = [];
  let failedRequests = [];
  const check = (name, pass, detail) => {
    checks.push({ name, pass: !!pass, detail });
    console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
  };
  const { cdp, close } = await launch({ width: 1440, height: 1000 }, out);
  try {
    await cdp.send("Page.navigate", { url: base });
    const ready = await waitReady(cdp);
    check("viewer ready (WebGL path, not the CSS fallback)", ready.ready && !ready.fallback, JSON.stringify(ready));

    const meta = await cdp.eval(`(()=>({
      title: document.getElementById('card-title').textContent.trim(),
      subtitle: document.getElementById('subtitle').textContent.trim(),
      edition: document.getElementById('edition').textContent.trim(),
      cfgTitle: window.__holo.config.title,
      model: window.__holo.modelSource,
      finish: document.getElementById('finish-name').textContent.trim(),
      tex: {
        subject:[window.__holo.uniforms.tSubject.value.image.width,window.__holo.uniforms.tSubject.value.image.height],
        background:[window.__holo.uniforms.tBackground.value.image.width,window.__holo.uniforms.tBackground.value.image.height],
        text:[window.__holo.uniforms.tText.value.image.width,window.__holo.uniforms.tText.value.image.height],
        line:[window.__holo.uniforms.tLine.value.image?.width??null,window.__holo.uniforms.tLine.value.image?.height??null],
        effects:[window.__holo.uniforms.tEffects.value.image?.width??null,window.__holo.uniforms.tEffects.value.image?.height??null]
      },
      u: Object.fromEntries(['uDepth','uBgDepth','uFxDepth','uFoil','uScale','uHasFx','uHasLine','uRelief'].map(k=>[k,window.__holo.uniforms[k].value]))
    }))()`);
    check("card metadata rendered", !!meta.title && meta.cfgTitle === meta.title, `${meta.title} / ${meta.subtitle} / ${meta.edition}`);
    check("exported model reached the page", String(meta.model).includes(".glb"), String(meta.model));
    const wanted = [meta.tex.subject, meta.tex.background, meta.tex.text, meta.tex.line, meta.tex.effects].filter((t) => t && t[0] !== null);
    check("image layers uploaded at one shared canvas", wanted.length >= 4 && new Set(wanted.map((t) => t.join("x"))).size === 1, JSON.stringify(meta.tex));
    const cfg = await cdp.eval("(()=>{const p=window.__holo.config.parameters||{};return {d:p.subjectDepth,b:p.backgroundDepth,f:p.effectsDepth,s:p.subjectScale}})()");
    check(
      "page starts from card-config.json depths",
      Math.abs(meta.u.uDepth - (cfg.d ?? 0.32)) < 1e-6 && Math.abs(meta.u.uBgDepth - (cfg.b ?? -0.18)) < 1e-6,
      `depth ${meta.u.uDepth}/${cfg.d}, bg ${meta.u.uBgDepth}/${cfg.b}, fx ${meta.u.uFxDepth}, foil ${meta.u.uFoil}`,
    );
    await cdp.shot("01-desktop-front", out);

    const animA = await cdp.shot("02-anim-a", out);
    await sleep(900);
    const animB = await cdp.shot("03-anim-b", out);
    check("idle motion animates the card", !same(animA, animB), `${animA} -> ${animB}`);
    check("auto motion reports on", (await cdp.eval("window.__holo.getState().auto")) === true);

    const stage = await cdp.eval(`(()=>{const r=document.getElementById('stage').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);
    const drag = async (dx, dy) => {
      await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x: stage.x, y: stage.y, button: "left", clickCount: 1, buttons: 1 });
      for (let i = 1; i <= 6; i++)
        await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: stage.x + (dx * i) / 6, y: stage.y + (dy * i) / 6, button: "left", buttons: 1 });
      await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: stage.x + dx, y: stage.y + dy, button: "left", buttons: 0 });
      await sleep(450);
    };
    const rot = () => cdp.eval("(()=>{const r=window.__holo.root.rotation;return [r.x,r.y,r.z]})()");
    await cdp.eval("window.__holo.reset()");
    await sleep(400);
    const rot0 = await rot();
    await drag(220, 90);
    const rotRight = await rot();
    const shotRight = await cdp.shot("04-tilt-right", out);
    await cdp.eval("window.__holo.reset()");
    await sleep(400);
    await drag(-220, -90);
    const rotLeft = await rot();
    const shotLeft = await cdp.shot("05-tilt-left", out);
    check("drag turns the card both directions", rotRight[1] > rot0[1] + 0.05 && rotLeft[1] < rot0[1] - 0.05, `y ${rot0[1].toFixed(3)} -> ${rotRight[1].toFixed(3)} / ${rotLeft[1].toFixed(3)}`);
    check("vertical drag tilts the card", Math.abs(rotRight[0] - rot0[0]) > 0.02 || Math.abs(rotLeft[0] - rot0[0]) > 0.02, `x ${rot0[0].toFixed(3)} -> ${rotRight[0].toFixed(3)} / ${rotLeft[0].toFixed(3)}`);
    check("both tilt directions render differently", !same(shotRight, shotLeft), `${shotRight} vs ${shotLeft}`);
    check("drag stops auto motion", (await cdp.eval("window.__holo.getState().auto")) === false);

    const zoom0 = await cdp.eval("window.__holo.getState().zoom");
    await cdp.send("Input.dispatchMouseEvent", { type: "mouseWheel", x: stage.x, y: stage.y, deltaX: 0, deltaY: -240 });
    await sleep(300);
    const zoomIn = await cdp.eval("window.__holo.getState().zoom");
    await cdp.send("Input.dispatchMouseEvent", { type: "mouseWheel", x: stage.x, y: stage.y, deltaX: 0, deltaY: 240 });
    await sleep(300);
    const zoomOut = await cdp.eval("window.__holo.getState().zoom");
    check("wheel zooms in and out", zoomIn !== zoom0 && zoomOut < zoomIn, `${zoom0} -> ${zoomIn} -> ${zoomOut}`);

    const frontShot = await cdp.shot("06-before-flip", out);
    await cdp.eval("window.__holo.flip()");
    await sleep(900);
    const flipped = await cdp.eval("window.__holo.getState().flipped");
    const backShot = await cdp.shot("07-back", out);
    check("flip to the back works", flipped === true && !same(backShot, frontShot), `flipped=${flipped}`);
    check("view label follows the flip", (await cdp.eval("document.getElementById('view-label').textContent.trim()")) === "02 / BACK");
    await cdp.eval("window.__holo.flip(false)");
    await sleep(900);
    check("flip back to the front works", (await cdp.eval("window.__holo.getState().flipped")) === false);

    await drag(200, 60);
    await cdp.eval("document.getElementById('stage').focus()");
    await cdp.eval("window.__holo.reset()");
    await sleep(700);
    const rotReset = await rot();
    const resetState = await cdp.eval("window.__holo.getState()");
    // reset() parks the card in its designed three-quarter study pose (-0.035, -0.15).
    check(
      "reset returns to the study pose and un-flips",
      Math.abs(rotReset[0] + 0.035) < 0.02 && Math.abs(rotReset[1] + 0.15) < 0.02 && resetState.zoom === 1 && resetState.flipped === false,
      `rot ${JSON.stringify(rotReset.map((v) => +v.toFixed(3)))} zoom=${resetState.zoom} flipped=${resetState.flipped}`,
    );
    for (const key of ["ArrowRight", "ArrowUp"]) {
      const code = key === "ArrowRight" ? 39 : 38;
      await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key, code: key, windowsVirtualKeyCode: code });
      await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key, code: key, windowsVirtualKeyCode: code });
    }
    await sleep(400);
    const rotKey = await rot();
    check("keyboard arrows nudge the card", Math.abs(rotKey[1]) > 0.02 || Math.abs(rotKey[0]) > 0.02, JSON.stringify(rotKey.map((v) => +v.toFixed(3))));
    await cdp.shot("08-keyboard-tilt", out);

    const panelToggle = await cdp.eval(`(()=>{const b=document.getElementById('depth-toggle');const before=document.getElementById('parameter-panel').hidden;b.click();const after=document.getElementById('parameter-panel').hidden;b.click();return {before,after}})()`);
    check("景深调整 panel toggles", panelToggle.before !== panelToggle.after, JSON.stringify(panelToggle));

    await cdp.eval("window.__holo.reset()");
    await sleep(600);
    const sliders = [
      ["scale", "uScale", "画面比例"],
      ["depth", "uDepth", "画面景深"],
      ["fx-depth", "uFxDepth", "特效景深"],
      ["bg-depth", "uBgDepth", "底纹景深"],
      ["foil", "uFoil", "光泽 / 镭射"],
    ];
    const hasFx = meta.u.uHasFx === 1;
    for (const [id, uniform, label] of sliders) {
      const present = await cdp.eval(`!!document.getElementById('${id}')`);
      if (!present) {
        check(`${label}: slider present`, false, "control missing from the markup");
        continue;
      }
      const before = await cdp.shot(`09-${id}-before`, out);
      const from = Number(await cdp.eval(`document.getElementById('${id}').value`));
      const to = await cdp.eval(`(()=>{const el=document.getElementById('${id}');const mid=(Number(el.max)+Number(el.min))/2;
        const t=Math.abs(Number(el.value)-mid)<0.02?Number(el.max):mid;
        el.value=String(t);el.dispatchEvent(new Event('input',{bubbles:true}));return Number(el.value)})()`);
      await sleep(500);
      const after = await cdp.shot(`10-${id}-after`, out);
      const now = Number(await cdp.eval(`window.__holo.uniforms['${uniform}'].value`));
      const readout = await cdp.eval(`document.getElementById('${id}-value').textContent.trim()`);
      const expectFrame = !(id === "fx-depth" && !hasFx);
      check(
        `${label}: slider drives the uniform and the rendered frame`,
        Math.abs(now - from) > 0.001 && Math.abs(now - to) < 0.02 && (same(before, after) ? !expectFrame : true),
        `uniform ${from} -> ${now} (control -> ${to}, readout ${readout}), frame ${same(before, after) ? "unchanged" : before + " -> " + after}${expectFrame ? "" : " [no effects layer: frame may not change]"}`,
      );
      await cdp.eval(`(()=>{const el=document.getElementById('${id}');el.value=String(${from});el.dispatchEvent(new Event('input',{bubbles:true}))})()`);
      await sleep(200);
    }
    await cdp.eval(`(()=>{const p=window.__holo.config.parameters||{};const v={scale:p.subjectScale??1,depth:p.subjectDepth??0.32,'fx-depth':p.effectsDepth??0.14,'bg-depth':p.backgroundDepth??-0.18,foil:p.foil??0.52};
      for(const[k,val]of Object.entries(v)){const el=document.getElementById(k);if(el){el.value=String(val);el.dispatchEvent(new Event('input',{bubbles:true}))}}})()`);
    await sleep(400);
    await cdp.shot("11-sliders-restored", out);

    const finishes = [];
    for (const value of ["pearl", "silver", "gold", "original"]) {
      const before = await cdp.shot(`12-finish-${value}-before`, out);
      const u = await cdp.eval(`(()=>{document.querySelector('[data-finish="${value}"]').click();return window.__holo.uniforms.uFinish.value})()`);
      await sleep(450);
      const after = await cdp.shot(`13-finish-${value}`, out);
      finishes.push({ value, u, changed: !same(before, after), label: await cdp.eval("document.getElementById('finish-name').textContent.trim()") });
    }
    check(
      "all four finishes switch the material",
      new Set(finishes.map((f) => f.u)).size >= 3 && finishes.every((f) => f.changed),
      finishes.map((f) => `${f.label}=${f.u}${f.changed ? "" : "(frame unchanged)"}`).join(" "),
    );

    errors = cdp.consoleErrors;
    failedRequests = cdp.failedRequests;
    check("no console errors or exceptions", errors.length === 0, errors.slice(0, 4).join(" | ") || "clean");
    check("no failed asset requests", failedRequests.length === 0, failedRequests.slice(0, 4).join(" | ") || "clean");

    // The screenshot button renders at 1400x1800 and runs toDataURL synchronously:
    // under software GL that keeps the renderer busy for a long time afterwards, so
    // it is the last probe of the pass and the click is scheduled, not awaited.
    await cdp.send("Page.setDownloadBehavior", { behavior: "allow", downloadPath: downloadDir });
    await cdp.eval("setTimeout(()=>document.getElementById('save').click(),0); 'scheduled'");
    let downloaded = [];
    for (let i = 0; i < 40; i++) {
      await sleep(2000);
      downloaded = readdirSync(downloadDir).filter((f) => f.endsWith(".png"));
      if (downloaded.length) break;
    }
    check("screenshot button downloads a card PNG", downloaded.length > 0, downloaded.length ? `got ${downloaded.join(",")}` : "no PNG appeared within 80s");
    return { checks, errors, failedRequests, downloaded };
  } finally {
    close();
  }
}

// ---------------------------------------------------------------- mobile pass
async function mobilePass(base, out) {
  const checks = [];
  const check = (name, pass, detail) => {
    checks.push({ name, pass: !!pass, detail });
    console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
  };
  const { cdp, close } = await launch({ width: 390, height: 844 }, out);
  try {
    // headless --window-size bottoms out near 492 CSS px, so the narrow viewport is
    // forced; deviceScaleFactor 2 mirrors a real phone.
    await cdp.send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
    await cdp.send("Page.navigate", { url: base });
    const ready = await waitReady(cdp);
    check("viewer ready at 390x844", ready.ready === true && ready.fallback === false, JSON.stringify(ready));

    const layout = await cdp.eval(`(()=>({
      innerW: window.innerWidth,
      scrollW: document.documentElement.scrollWidth,
      clientW: document.documentElement.clientWidth,
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
      offenders: [...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right > window.innerWidth + 1).slice(0,5).map(e=>e.tagName+'.'+(e.className||'').toString().slice(0,30)),
      title: document.getElementById('card-title').textContent.trim(),
      canvas: [document.querySelector('canvas').getBoundingClientRect().width|0, document.querySelector('canvas').getBoundingClientRect().height|0]
    }))()`);
    const narrow = layout.innerW <= 420;
    check(
      "narrow viewport has no horizontal overflow",
      narrow && !layout.overflow,
      `innerWidth=${layout.innerW} scrollWidth=${layout.scrollW}${layout.offenders.length ? " offenders=" + JSON.stringify(layout.offenders) : ""}${narrow ? "" : " [viewport override refused: measured at " + layout.innerW + "px, not a 390px claim]"}`,
    );
    check("card still renders at the narrow width", !!layout.title && layout.canvas[0] > 100, `${layout.title}, canvas ${layout.canvas.join("x")}`);
    await cdp.shot("14-mobile-390", out);

    await cdp.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
    await cdp.send("Page.navigate", { url: base });
    const readyRm = await waitReady(cdp);
    await sleep(1200);
    const rm = await cdp.eval(`(()=>({match:matchMedia('(prefers-reduced-motion: reduce)').matches,auto:window.__holo.getState().auto,t:window.__holo.uniforms.uTime.value}))()`);
    check("reduced motion: auto motion off and shader clock pinned", readyRm.ready === true && rm.match === true && rm.auto === false && rm.t === 0, JSON.stringify(rm));
    const rmShot = await cdp.shot("15-reduced-motion", out);
    await sleep(1200);
    const rmShot2 = await cdp.shot("16-reduced-motion-stable", out);
    check("reduced motion keeps the card still", same(rmShot, rmShot2), `frame ${rmShot} -> ${rmShot2} over 1.2s`);
    check("no console errors on mobile", cdp.consoleErrors.length === 0, cdp.consoleErrors.slice(0, 3).join(" | ") || "clean");
    return { checks };
  } finally {
    close();
  }
}

// ---------------------------------------------------------------- run
let server = null;
let base = target;
if (isDir) {
  const web = path.join(project, "web");
  if (!existsSync(path.join(web, "server.mjs"))) {
    console.error(`No viewer at ${web} — run scripts/run_pipeline.py --project ${project} first.`);
    process.exit(2);
  }
  const port = await freePort();
  base = `http://127.0.0.1:${port}/`;
  server = spawn(process.execPath, ["server.mjs"], { cwd: web, env: { ...process.env, PORT: String(port) }, stdio: "ignore" });
  if (!(await waitForHttp(base))) {
    console.error(`Viewer server did not answer on ${base}`);
    server.kill("SIGKILL");
    process.exit(2);
  }
  console.log(`serving ${web} at ${base}`);
}

const report = { target, base, browser, passes: {} };
let failed = 0;
try {
  if (only !== "mobile") {
    const d = await desktopPass(base, outDir);
    report.passes.desktop = { checks: d.checks, consoleErrors: d.errors, failedRequests: d.failedRequests, downloads: d.downloaded };
    failed += d.checks.filter((c) => !c.pass).length;
  }
  if (only !== "desktop") {
    const m = await mobilePass(base, outDir);
    report.passes.mobile = { checks: m.checks };
    failed += m.checks.filter((c) => !c.pass).length;
  }
} finally {
  if (server && !keepServer) server.kill("SIGKILL");
  rmSync(downloadDir, { recursive: true, force: true });
}

const all = Object.values(report.passes).flatMap((p) => p.checks);
report.summary = { total: all.length, passed: all.filter((c) => c.pass).length, failed };
writeFileSync(path.join(outDir, "report.json"), JSON.stringify(report, null, 2));
console.log(`\n${report.summary.passed}/${report.summary.total} checks passed — report: ${path.join(outDir, "report.json")}`);
if (server && keepServer) console.log(`viewer left running at ${base}`);
if (failed) console.log("FAILED: " + all.filter((c) => !c.pass).map((c) => c.name).join("; "));
process.exit(failed ? 1 : 0);
