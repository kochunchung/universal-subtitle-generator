"use strict";
// 萬能字幕產生器 網頁版前端(不需要建置步驟的純 JavaScript)。
// 所有使用者提供的文字一律經過 esc() 才放進畫面。

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const STATE = {
  uploaded: ["已上傳", ""], queued: ["排隊中", "run"], running: ["處理中", "run"], transcribed: ["已轉錄,等待翻譯", "run"],
  done: ["完成", "ok"], skipped: ["略過", ""], no_speech: ["沒有人聲", "warn"], failed: ["失敗", "bad"], canceled: ["已取消", "warn"],
  extracting: ["伺服器抽音訊中", "run"], choose_track: ["請選擇音軌", "warn"],
};
const ACTIVE = new Set(["queued", "running"]);
const APP_TITLE = "萬能字幕產生器";
const LOGIN_ERRORS = {
  denied: "你取消了 Google 登入。", unverified: "這個 Google 帳號的信箱還沒有驗證,無法登入。",
  domain: "這個信箱不在允許登入的名單內。", blocked: "這個帳號已被停用,請聯絡管理者。",
  failed: "Google 登入失敗,請再試一次。", disabled: "這個網站還沒有設定 Google 登入。",
};

let csrf = "", me = null, opts = null, prefs = {}, items = [], jobs = [];
const selected = new Set();
const streams = new Map();
let logLines = [];

// ------------------------------------------------------------------ 共用
async function api(path, { method = "GET", json, body, headers = {} } = {}) {
  const init = { method, headers: { "X-CSRF-Token": csrf, ...headers } };
  if (json !== undefined) { init.body = JSON.stringify(json); init.headers["Content-Type"] = "application/json"; }
  else if (body !== undefined) init.body = body;
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch { /* 非 JSON */ }
  if (!r.ok) { const e = new Error((data && data.error) || `錯誤 ${r.status}`); e.status = r.status; throw e; }
  return data;
}
let toastTimer = null;
function toast(msg, ms = 4000) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}
function fmtSize(n) {
  if (n >= 2 ** 30) return +(n / 2 ** 30).toFixed(n >= 10 * 2 ** 30 ? 0 : 1) + " GB";
  if (n >= 2 ** 20) return +(n / 2 ** 20).toFixed(1) + " MB";
  return Math.max(1, Math.round(n / 1024)) + " KB";
}
function fmtDur(s) {
  s = Math.round(s || 0); if (!s) return "";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0");
}
function fmtWait(sec) {
  if (sec == null) return "";
  if (sec < 60) return "不到 1 分鐘";
  return "約 " + Math.round(sec / 60) + " 分鐘";
}
function log(text) {
  logLines.push(text); if (logLines.length > 500) logLines = logLines.slice(-500);
  const p = $("log"); p.textContent = logLines.join("\n"); p.scrollTop = p.scrollHeight;
}

// ------------------------------------------------------------------ 啟動與畫面切換
async function boot() {
  const q = new URLSearchParams(location.search);
  const loginError = q.get("login_error");
  if (loginError) history.replaceState(null, "", location.pathname + location.hash);   // 網址不留錯誤代碼
  try {
    me = await api("/api/me");
    csrf = me.csrf;
  } catch {
    me = null;
  }
  if (!me) return showLogin(LOGIN_ERRORS[loginError] || "");
  if (me.status !== "approved") return showWaiting();
  $("nav").hidden = false; $("who").hidden = false; $("whoName").textContent = me.name;
  $("navAdmin").hidden = !me.is_admin;
  if (me.is_admin) refreshPendingBadge();
  opts = await api("/api/options");
  prefs = await api("/api/prefs");
  initOptions();
  await refresh();
  for (const j of jobs) if (ACTIVE.has(j.status)) follow(j.id);
  window.addEventListener("hashchange", route);
  route();
}
// 已登入但還沒核准(或被停用):只顯示說明,不顯示工作台
function showWaiting() {
  ["view-work", "view-settings", "view-admin", "view-login"].forEach((v) => { $(v).hidden = true; });
  $("view-wait").hidden = false;
  const blocked = me.status === "blocked";
  $("waitTitle").textContent = blocked ? "這個帳號已被停用" : "帳號尚待管理者核准";
  $("waitText").textContent = blocked ? "如果有疑問,請聯絡管理者。"
    : "你的帳號已建立,管理者核准後就能使用。核准後按「重新整理」即可進入。";
  $("waitWho").textContent = me.email ? `${me.name}(${me.email})` : me.name;
}
$("waitReload").onclick = () => location.reload();
$("waitLogout").onclick = async () => { await api("/api/logout", { method: "POST" }); location.reload(); };

async function showLogin(msg) {
  ["view-work", "view-settings", "view-admin", "view-wait"].forEach((v) => { $(v).hidden = true; });
  $("view-login").hidden = false; $("loginMsg").textContent = msg || "";
  try {
    const info = await api("/api/public-info");
    $("devLogin").hidden = !info.dev_login;
    $("googleBtn").hidden = !info.google;
    $("googleOff").hidden = info.google;
  } catch { /* 忽略 */ }
}
function route() {
  let view = location.hash === "#/settings" ? "settings" : location.hash === "#/admin" ? "admin" : "work";
  if (view === "admin" && !me.is_admin) view = "work";
  $("view-login").hidden = true; $("view-wait").hidden = true;
  $("view-work").hidden = view !== "work";
  $("view-settings").hidden = view !== "settings";
  $("view-admin").hidden = view !== "admin";
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("on", a.dataset.view === view));
  if (view === "settings") loadSettings();
  if (view === "admin") loadAdmin();
}
$("devForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/dev-login", { method: "POST", json: { name: $("devName").value.trim(), admin: $("devAdmin").checked,
      tier: $("devTier").value, pending: $("devPending").checked } });
    location.hash = "#/"; location.reload();
  } catch (x) { $("loginMsg").textContent = x.message; }
});
$("logout").onclick = async () => { await api("/api/logout", { method: "POST" }); location.hash = ""; location.reload(); };

// ------------------------------------------------------------------ 選項(自動記住)
function fill(el, pairs, value) {
  el.innerHTML = pairs.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("");
  if (value !== undefined && value !== null && pairs.some(([v]) => v === value)) el.value = value;
}
function initOptions() {
  const d = opts.defaults, p = (k) => (prefs[k] !== undefined ? prefs[k] : d[k]);
  const modelName = (m) => m.split("/").pop();
  fill($("o_model"), opts.whisper_models.map((m) => [m, modelName(m)]), p("whisper_model"));
  $("rowModel").hidden = opts.whisper_models.length < 2;
  fill($("o_src"), opts.source_languages.map((v) => [v, v === "auto" ? "自動偵測" : (opts.lang_names[v] || v)]), p("source_language"));
  fill($("o_tgt"), [["", "不翻譯(只轉錄)"], ...opts.targets.map((v) => [v, opts.lang_names[v] || v])], p("translate_to") || "");
  fill($("o_mode"), [["translated", "只輸出譯文"], ["bilingual", "雙語(原文+譯文)"]], p("subtitle_mode"));
  fill($("o_eng"), opts.engines.map((v) => [v, opts.engine_labels[v] || v]), p("translation_engine"));
  $("o_tw").checked = !!p("opencc_s2twp"); $("o_vad").checked = !!p("vad"); $("o_txt").checked = !!p("write_txt");
  $("o_upload").value = prefs.upload_mode === "original" ? "original" : "browser";
  $("o_upload").addEventListener("change", async () => {
    showUploadHint();
    try { prefs = await api("/api/prefs", { method: "PUT", json: { upload_mode: $("o_upload").value } }); } catch (x) { toast("選項沒有存起來:" + x.message); }
  });
  toggleTgt();
  for (const id of ["o_model", "o_src", "o_tgt", "o_mode", "o_eng", "o_tw", "o_vad", "o_txt"]) $(id).addEventListener("change", onOptChange);
}
function currentOptions() {
  const o = {
    source_language: $("o_src").value, translate_to: $("o_tgt").value || null, subtitle_mode: $("o_mode").value,
    translation_engine: $("o_eng").value, opencc_s2twp: $("o_tw").checked, vad: $("o_vad").checked, write_txt: $("o_txt").checked,
  };
  if (!$("rowModel").hidden) o.whisper_model = $("o_model").value;
  return o;
}
function toggleTgt() { document.querySelectorAll(".needs-tgt").forEach((el) => { el.hidden = !$("o_tgt").value; }); }
let prefTimer = null;
function onOptChange() {
  toggleTgt();
  clearTimeout(prefTimer);
  prefTimer = setTimeout(async () => {
    try { prefs = await api("/api/prefs", { method: "PUT", json: currentOptions() }); } catch (x) { toast("選項沒有存起來:" + x.message); }
  }, 300);
}

// ------------------------------------------------------------------ 上傳
// 影片:先在瀏覽器用 ffmpeg.wasm 抽出單聲道 MP3,只上傳音訊(影片不離開使用者的電腦);
//       抽不出來(格式不支援、記憶體不足…)時自動改成分塊上傳原檔,由伺服器抽音訊。
// 音檔(300 MB 以內)與 SRT:直接分塊上傳。所有上傳都可以斷線續傳。
const AUDIO_EXTS = new Set([".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"]);
let limits = null;
const FORCE_ORIGINAL = new URLSearchParams(location.search).has("original");   // 測試備援路線用

const drop = $("drop");
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => upload([...e.dataTransfer.files]));
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("file").click(); } });
$("file").addEventListener("change", (e) => { upload([...e.target.files]); e.target.value = ""; });

class Canceled extends Error {}
class NoFallback extends Error {}          // 原檔也一定不行的錯誤(例如沒有音軌、太長),不改傳原檔
const NO_ORIGINAL = "瀏覽器無法處理這個檔案。請先用桌面版或其他工具轉成 MP3 再上傳,或請管理者開通改傳原檔";

let uploading = Promise.resolve();
function upload(files) {
  for (const f of files) {
    const row = new UploadRow(f);
    uploading = uploading.then(() => processFile(f, row)).catch(() => {});
  }
}

class UploadRow {
  constructor(f) {
    this.canceled = false; this.onCancel = null;
    this.li = document.createElement("li");
    this.li.innerHTML = `<div class="up-head"><span class="name">${esc(f.name)}</span>
      <button class="link small up-x">取消</button></div>
      <div class="prog"><i style="width:0%"></i></div><div class="up-msg mute small">等待中</div><div class="up-extra"></div>`;
    $("uploads").appendChild(this.li);
    this.li.querySelector(".up-x").onclick = () => this.cancel();
  }
  status(text, pct) {
    this.li.querySelector(".up-msg").textContent = text;
    if (pct != null) this.li.querySelector("i").style.width = Math.max(0, Math.min(100, pct)).toFixed(1) + "%";
  }
  cancel() { this.canceled = true; if (this.onCancel) this.onCancel(); this.fail("已取消"); }
  check() { if (this.canceled) throw new Canceled("已取消"); }
  done() { this.li.remove(); }
  fail(msg) {
    this.li.querySelector(".up-msg").innerHTML = `<span class="bad">${esc(msg)}</span>`;
    const x = this.li.querySelector(".up-x"); x.textContent = "關閉"; x.onclick = () => this.li.remove();
  }
  chooseTrack(tracks) {      // 多條音軌:列出來讓使用者選
    return new Promise((resolve, reject) => {
      const box = this.li.querySelector(".up-extra");
      box.innerHTML = `<label class="small">這個檔案有 ${tracks.length} 條音軌,要轉錄哪一條?
        <select>${tracks.map((t, i) => `<option value="${i}">${esc(t)}</option>`).join("")}</select></label>
        <button class="btn small-btn">確定</button>`;
      this.status("請選擇音軌");
      this.onCancel = () => reject(new Canceled("已取消"));
      box.querySelector("button").onclick = () => { const v = +box.querySelector("select").value; box.innerHTML = ""; this.onCancel = null; resolve(v); };
    });
  }
}

const extOf = (name) => (name.match(/\.[^.]+$/) || [""])[0].toLowerCase();

async function ensureLimits() {
  if (!limits) limits = (await api("/api/usage")).limits;
  return limits;
}

async function processFile(f, row) {
  try {
    const lim = await ensureLimits();
    const ext = extOf(f.name);
    if (ext === ".srt") {
      await uploadChunked(f, f.name, "srt", row, "上傳字幕");
    } else if (AUDIO_EXTS.has(ext) && f.size <= lim.audio_bytes) {
      await uploadChunked(f, f.name, "original", row, "上傳音檔");
    } else {
      let audio = null;
      const wantOriginal = !AUDIO_EXTS.has(ext) && (FORCE_ORIGINAL || prefs.upload_mode === "original");
      if (wantOriginal && lim.original_allowed && f.size <= lim.original_bytes) {
        await uploadChunked(f, f.name, "original", row, "上傳原檔(伺服器抽聲音)");
        row.done(); log(`已上傳 ${f.name}(原檔)`);
        await refresh();
        return;
      }
      if (!FORCE_ORIGINAL) {
        try {
          audio = await browserExtract(f, row);
        } catch (e) {
          if (row.canceled) throw new Canceled("已取消");
          if (e instanceof Canceled || e instanceof NoFallback) throw e;
          log(`${f.name}:瀏覽器無法抽音訊(${e.message}),改傳原檔`);
        }
      }
      if (audio) {
        await uploadChunked(audio, f.name, "audio", row, "上傳音訊");
      } else {
        if (!lim.original_allowed) throw new NoFallback(NO_ORIGINAL);
        if (f.size > lim.original_bytes) throw new NoFallback(`瀏覽器無法處理,而原檔超過 ${fmtSize(lim.original_bytes)} 的上限`);
        await uploadChunked(f, f.name, "original", row, "瀏覽器無法處理,改傳原檔");
      }
    }
    row.done(); log(`已上傳 ${f.name}`);
    await refresh();
  } catch (e) {
    if (!(e instanceof Canceled)) { row.fail(e.message); log(`上傳失敗 ${f.name}:${e.message}`); }
  }
}

// ---- 瀏覽器抽音訊(ffmpeg.wasm,第一次使用時下載約 32 MB 的元件,之後有快取)
let ffmpeg = null, ffLog = null;
async function getFFmpeg(row) {
  if (ffmpeg) return ffmpeg;
  row.status("載入抽音訊元件(第一次約 32 MB,請稍候)…", 0);
  const { FFmpeg } = await import("/vendor/ffmpeg/index.js");
  const f = new FFmpeg();
  f.on("log", ({ message }) => { if (ffLog) ffLog(message); });
  await f.load({ coreURL: "/vendor/core/ffmpeg-core.js", wasmURL: "/vendor/core/ffmpeg-core.wasm" });
  ffmpeg = f;
  return f;
}
function dropFFmpeg() { try { ffmpeg?.terminate(); } catch { /* 忽略 */ } ffmpeg = null; }

async function browserExtract(file, row) {
  if (typeof WebAssembly !== "object") throw new Error("瀏覽器不支援 WebAssembly");
  const ff = await getFFmpeg(row);
  row.check();
  const inName = "input" + (extOf(file.name) || ".bin");
  try { await ff.unmount("/in"); } catch { /* 沒掛載過 */ }
  try { await ff.createDir("/in"); } catch { /* 已存在 */ }
  if (!(await ff.mount("WORKERFS", { blobs: [{ name: inName, data: file }] }, "/in"))) throw new Error("無法讀取檔案");
  row.onCancel = () => dropFFmpeg();
  try {
    // 1. 讀檔案資訊:音軌與長度
    row.status("讀取檔案資訊…");
    const lines = []; let inInput = true;
    ffLog = (m) => { if (/^Output #0/.test(m)) inInput = false; if (inInput) lines.push(m); };
    await ff.exec(["-hide_banner", "-i", "/in/" + inName]);          // 沒有輸出檔,只為了讀資訊
    ffLog = null;
    row.check();
    const dm = lines.join("\n").match(/Duration:\s*(\d+):(\d+):(\d+\.?\d*)/);
    const duration = dm ? +dm[1] * 3600 + +dm[2] * 60 + +dm[3] : 0;
    const tracks = lines.filter((l) => /Stream #0:\d+.*Audio:/.test(l)).map((l, i) => {
      const lang = (l.match(/Stream #0:\d+\((\w+)\)/) || [])[1];
      const codec = (l.match(/Audio:\s*([\w-]+)/) || [])[1] || "";
      return `第 ${i + 1} 軌` + (lang && lang !== "und" ? ` · ${lang}` : "") + (codec ? ` · ${codec}` : "");
    });
    if (!lines.some((l) => /Input #0/.test(l))) throw new Error("格式不支援");
    if (!tracks.length) throw new NoFallback("這個檔案沒有音軌");
    if (duration > limits.duration_s) throw new NoFallback(`檔案長度超過 ${Math.round(limits.duration_s / 3600)} 小時的上限`);
    // 2. 選音軌
    const track = tracks.length > 1 ? await row.chooseTrack(tracks) : 0;
    row.onCancel = () => dropFFmpeg();
    row.check();
    // 3. 抽成單聲道 128 kbps MP3(和桌面版轉 MP3 的參數相同)
    const t0 = performance.now();
    const prog = ({ progress }) => {
      const p = Math.max(0, Math.min(1, progress));
      row.status(`在瀏覽器抽音訊中 ${(p * 100).toFixed(0)}%(影片不會上傳)`, p * 100);
    };
    ff.on("progress", prog);
    let code;
    try {
      code = await ff.exec(["-hide_banner", "-i", "/in/" + inName, "-map", `0:a:${track}`, "-vn", "-sn", "-dn",
        "-ac", "1", "-codec:a", "libmp3lame", "-b:a", "128k", "out.mp3"]);
    } finally { ff.off("progress", prog); }
    row.check();
    if (code !== 0) throw new Error(`抽音訊失敗(代碼 ${code})`);
    const data = await ff.readFile("out.mp3");
    try { await ff.deleteFile("out.mp3"); } catch { /* 忽略 */ }
    log(`${file.name}:瀏覽器抽音訊 ${((performance.now() - t0) / 1000).toFixed(0)} 秒,${fmtSize(data.length)}`);
    return new Blob([data], { type: "audio/mpeg" });
  } catch (e) {
    if (e instanceof Canceled || e instanceof NoFallback) throw e;
    dropFFmpeg();                                   // 出錯後重新載入比較保險(例如記憶體不足)
    throw e;
  } finally {
    ffLog = null; row.onCancel = null;
    if (ffmpeg) { try { await ffmpeg.unmount("/in"); } catch { /* 忽略 */ } }
  }
}

// ---- 分塊上傳(可續傳):重新選同一個檔案時,從伺服器已收到的位置接著傳
async function uploadChunked(blob, name, content, row, label) {
  row.check();
  let up = (await api("/api/uploads")).find((u) => u.name === name && u.size === blob.size && u.content === content);
  if (!up) up = await api("/api/uploads", { method: "POST", json: { name, size: blob.size, content } });
  else log(`${name}:從上次的 ${fmtSize(up.received)} 接著傳`);
  const chunk = limits.chunk_bytes;
  let off = up.received || 0, fails = 0;
  const t0 = performance.now(), start = off;
  row.onCancel = () => { api(`/api/uploads/${up.id}`, { method: "DELETE" }).catch(() => {}); };
  while (off < blob.size) {
    row.check();
    let r;
    try {
      r = await fetch(`/api/uploads/${up.id}?offset=${off}`, { method: "PUT", body: blob.slice(off, off + chunk),
        headers: { "X-CSRF-Token": csrf } });
    } catch {
      if (++fails > 8) throw new Error("網路中斷,請稍後重新選擇同一個檔案,會從中斷的地方接著傳");
      row.status(`網路中斷,${fails * 2} 秒後重試…`);
      await new Promise((ok) => setTimeout(ok, fails * 2000));
      off = (await api(`/api/uploads/${up.id}`)).received; continue;
    }
    const d = await r.json().catch(() => ({}));
    if (r.status === 409 && d.received != null) { off = d.received; continue; }
    if (!r.ok) throw new Error(d.error || `上傳失敗(${r.status})`);
    fails = 0; off = d.received;
    const mbps = (off - start) / 2 ** 20 / Math.max(0.001, (performance.now() - t0) / 1000);
    row.status(`${label} ${fmtSize(off)} / ${fmtSize(blob.size)}(${mbps.toFixed(1)} MB/s)`, off / blob.size * 100);
  }
  row.onCancel = null;
  row.status("伺服器檢查檔案中…", 100);
  await api(`/api/uploads/${up.id}/finish`, { method: "POST" });
}

// ------------------------------------------------------------------ 檔案清單
let pollTimer = null;
async function refresh() {
  const st = await api("/api/state");
  items = st.items; jobs = st.jobs;
  for (const id of [...selected]) if (!items.some((i) => i.id === id)) selected.delete(id);
  render();
  refreshUsage();
  // 伺服器抽音訊(備援路線)沒有進度推播,抽的時候每 2 秒更新一次
  clearTimeout(pollTimer);
  if (items.some((i) => i.state === "extracting")) pollTimer = setTimeout(refresh, 2000);
}
function showUploadHint() {
  const can = !!(limits && limits.original_allowed);
  $("rowUpload").hidden = !can;
  $("uploadHint").hidden = !can || $("o_upload").value !== "original";
  $("dropHint").textContent = can && $("o_upload").value === "original"
    ? "影片會整檔上傳,由伺服器抽出聲音後立即刪除影片。"
    : "影片會先在你的瀏覽器裡抽出聲音再上傳,影片本身不會離開你的電腦;瀏覽器處理不了時才改傳原檔。";
}
async function refreshUsage() {
  try {
    const u = await api("/api/usage");
    limits = u.limits;
    showUploadHint();
    const h = (sec) => (sec >= 3600 ? (sec / 3600).toFixed(1) + " 小時" : Math.round(sec / 60) + " 分鐘");
    const meter = (icon, label, used, limit, text) => {
      const pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
      return `<div class="meter${pct >= 80 ? " warn" : ""}"><svg class="ic" aria-hidden="true"><use href="#${icon}"/></svg>
        <div class="meter-body"><div class="meter-label"><span>${label}</span><b>${text}</b></div>
        <div class="meter-bar" role="progressbar" aria-label="${label}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(pct)}"><i style="width:${pct.toFixed(1)}%"></i></div></div></div>`;
    };
    $("usage").innerHTML = `<span class="tier"><span class="badge ${u.tier === "paid" ? "run" : ""}">${esc(u.tier_label)}</span>${u.priority ? '<span class="mute small">優先排隊</span>' : ""}</span>`
      + meter("i-clock", "今天已處理", u.today_seconds, u.daily_limit, `${h(u.today_seconds)} / ${h(u.daily_limit)}`)
      + meter("i-hdd", "使用空間", u.storage_bytes, u.storage_limit, `${fmtSize(u.storage_bytes)} / ${fmtSize(u.storage_limit)}`);
  } catch { /* 用量只是提示 */ }
}
let refreshTimer = null;
function refreshSoon() { clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, 250); }

function render() {
  $("empty").hidden = items.length > 0;
  $("files").hidden = items.length === 0;
  $("rows").innerHTML = items.map(rowHtml).join("");
  $("selAll").checked = items.length > 0 && items.every((i) => selected.has(i.id));
  $("bulk").hidden = selected.size === 0;
  $("bulkCount").textContent = `已選 ${selected.size} 個`;
  renderStatus();
}
// 到期清除(第 5 階段):每筆檔案顯示還剩多久
function fmtLeft(ts) {
  const sec = ts - Date.now() / 1000;
  if (sec <= 60) return "即將";
  if (sec < 3600) return `${Math.ceil(sec / 60)} 分鐘後`;
  if (sec < 86400) return `${Math.ceil(sec / 3600)} 小時後`;
  return `${Math.ceil(sec / 86400)} 天後`;
}
function expiryText(it) {
  const e = it.expires; if (!e || ACTIVE.has(it.state) || it.state === "extracting") return it.audio_expired ? "音訊已到期刪除" : "";
  if (it.state === "choose_track") return `沒有選音軌的話,${fmtLeft(e.audio)}刪除`;
  const out = [];
  if (it.audio_expired) out.push("音訊已到期刪除");
  else if (e.audio && e.srt) out.push(`音訊 ${fmtLeft(e.audio)}刪除`);
  if (e.srt) out.push(`字幕 ${fmtLeft(e.srt)}刪除`);
  else if (e.audio) out.push(`${fmtLeft(e.audio)}刪除(還沒處理)`);
  return out.join(" · ");
}
function rowHtml(it) {
  const [label, cls] = STATE[it.state] || [it.state, ""];
  const pct = it.progress == null ? 0 : Math.round(it.progress * 100);
  const busy = ACTIVE.has(it.state);
  const preparing = it.state === "extracting" || it.state === "choose_track";
  const meta = [fmtSize(it.size), fmtDur(it.duration), it.kind === "srt" ? "字幕檔" : ""].filter(Boolean).join(" · ");
  const subs = [];
  const link = (which, text) => `<button class="link" data-act="preview" data-which="${which}" data-id="${it.id}">預覽</button>`
    + `<a class="link" href="/api/items/${it.id}/download?which=${which}">下載</a>`;
  if (it.kind === "srt") subs.push(`<div><span class="lbl">原文</span> <button class="link" data-act="preview" data-which="srt" data-id="${it.id}">預覽</button></div>`);
  if (it.outputs.includes("srt")) subs.push(`<div><span class="lbl">原文</span> ${link("srt")}</div>`);
  if (it.outputs.includes("translated")) subs.push(`<div><span class="lbl">譯文</span> ${link("translated")}</div>`);
  if (it.outputs.includes("txt")) subs.push(`<div><span class="lbl">TXT</span> <a class="link" href="/api/items/${it.id}/download?which=txt">下載</a></div>`);
  const exp = expiryText(it);
  return `<tr data-id="${it.id}">
    <td class="c-check"><input type="checkbox" data-sel="${it.id}" ${selected.has(it.id) ? "checked" : ""} aria-label="選取 ${esc(it.name)}"></td>
    <td><div class="fname">${esc(it.name)}</div><div class="meta">${esc(meta)}</div>${exp ? `<div class="meta exp"><svg class="ic" style="width:14px;height:14px" aria-hidden="true"><use href="#i-clock"/></svg>${esc(exp)}</div>` : ""}</td>
    <td class="c-state"><span class="badge ${cls}">${esc(label)}</span>${it.message && it.message !== label ? `<div class="msg">${esc(it.message)}</div>` : ""}
      ${it.state === "choose_track" ? `<div class="track"><select data-track="${it.id}">${(it.tracks || []).map((t, i) => `<option value="${i}">${esc(t)}</option>`).join("")}</select>
        <button class="btn small-btn" data-act="track" data-id="${it.id}">開始抽音訊</button></div>` : ""}</td>
    <td class="c-prog">${it.state === "uploaded" || it.state === "choose_track" ? "" : `<div class="prog" title="${pct}%"><i style="width:${pct}%"></i></div>`}</td>
    <td class="c-act"><div class="subs">${subs.join("") || '<span class="mute">—</span>'}</div></td>
    <td class="c-more"><details class="more"><summary aria-label="更多動作">⋯</summary><div class="menu">
      <button class="link" data-act="translate" data-id="${it.id}" ${busy || preparing ? "disabled" : ""}>只翻譯</button>
      <button class="link" data-act="retranslate" data-id="${it.id}" ${busy || preparing ? "disabled" : ""}>重新翻譯(覆蓋舊譯文)</button>
      ${it.kind === "media" ? `<button class="link" data-act="retranscribe" data-id="${it.id}" ${busy || preparing || it.audio_expired ? "disabled" : ""}
        ${it.audio_expired ? 'title="音訊已到期刪除,要重新轉錄請重新上傳"' : ""}>重新轉錄(覆蓋舊字幕)</button>` : ""}
      <button class="link bad" data-act="delete" data-id="${it.id}" ${busy ? "disabled" : ""}>刪除</button>
    </div></details></td></tr>`;
}
function renderStatus() {
  const active = jobs.filter((j) => ACTIVE.has(j.status));
  $("cancel").hidden = active.length === 0;
  $("notifyBtn").hidden = !(active.length && "Notification" in window && Notification.permission === "default");
  const s = $("status");
  if (!active.length) { s.hidden = true; return; }
  const parts = active.map((j) => {
    const n = j.item_ids.length;
    if (j.status === "running") return `處理中:${n} 個檔案`;
    const ahead = Math.max(0, (j.position || 1) - 1);
    const head = ahead ? `排隊中:前面還有 ${ahead} 個工作` : "排隊中:下一個就輪到你";
    return head + (j.wait_seconds != null ? `,預估${fmtWait(j.wait_seconds)}後開始` : "");
  });
  s.hidden = false; s.textContent = parts.join(";");
}

$("rows").addEventListener("change", (e) => {
  const id = e.target.dataset.sel; if (!id) return;
  e.target.checked ? selected.add(id) : selected.delete(id);
  render();
});
$("selAll").addEventListener("change", (e) => {
  items.forEach((i) => (e.target.checked ? selected.add(i.id) : selected.delete(i.id)));
  render();
});
$("rows").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]"); if (!b) return;
  const id = b.dataset.id, act = b.dataset.act;
  b.closest("details")?.removeAttribute("open");
  if (act === "preview") return openPreview(id, b.dataset.which);
  if (act === "track") {
    const stream = +document.querySelector(`select[data-track="${id}"]`).value;
    try { await api(`/api/items/${id}/track`, { method: "POST", json: { stream } }); } catch (x) { toast(x.message); }
    return refresh();
  }
  if (act === "delete") return deleteItems([id]);
  return startJob(act, [id]);
});
document.addEventListener("click", (e) => {   // 點別處時關閉「更多」選單
  document.querySelectorAll("details.more[open]").forEach((d) => { if (!d.contains(e.target)) d.removeAttribute("open"); });
});
$("bulk").addEventListener("click", (e) => {
  const act = e.target.dataset.bulk; if (!act) return;
  const ids = items.filter((i) => selected.has(i.id)).map((i) => i.id);
  if (act === "zip") { location.href = "/api/download-zip?ids=" + encodeURIComponent(ids.join(",")); return; }
  if (act === "delete") return deleteItems(ids);
  startJob(act, ids);
});

async function deleteItems(ids) {
  const names = items.filter((i) => ids.includes(i.id)).map((i) => i.name);
  if (!confirm(`刪除 ${names.length} 個檔案和它們的字幕?\n\n${names.slice(0, 8).join("\n")}`)) return;
  for (const id of ids) {
    try { await api("/api/items/" + id, { method: "DELETE" }); selected.delete(id); } catch (x) { toast(x.message); }
  }
  await refresh();
}

// ------------------------------------------------------------------ 處理
$("start").onclick = () => startJob("auto", items.map((i) => i.id));
async function startJob(mode, ids) {
  if (!ids.length) { toast("請先加入檔案"); return; }
  const o = currentOptions();
  if ((mode === "translate" || mode === "retranslate") && !o.translate_to) { toast("請先在「處理選項」選擇翻譯目標"); return; }
  if (mode === "retranscribe" && !confirm("重新轉錄會覆蓋原本的字幕檔,確定嗎?")) return;
  try {
    const r = await api("/api/jobs", { method: "POST", json: { item_ids: ids, mode, options: o } });
    r.skipped.forEach((s) => log(`略過 ${s.name}:${s.reason}`));
    await refresh();
    follow(r.job.id);
  } catch (x) { toast(x.message, 7000); }
}
$("cancel").onclick = async () => {
  const active = jobs.filter((j) => ACTIVE.has(j.status));
  if (!active.length || !confirm("取消所有排隊中與處理中的工作?")) return;
  for (const j of active) { try { await api(`/api/jobs/${j.id}/cancel`, { method: "POST" }); } catch { /* 可能剛好結束 */ } }
  log("已要求取消。");
  refreshSoon();
};

function follow(jobId) {
  if (streams.has(jobId)) return;
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  streams.set(jobId, es);
  es.addEventListener("log", (e) => log(JSON.parse(e.data).text));
  es.addEventListener("file", (e) => {
    const d = JSON.parse(e.data), it = items.find((i) => i.id === d.item_id);
    if (!it) return;
    it.state = d.state; if (d.progress != null) it.progress = d.progress; it.message = d.message;
    patchRow(it);
  });
  es.addEventListener("outputs", refreshSoon);
  es.addEventListener("queue", (e) => {
    const d = JSON.parse(e.data), j = jobs.find((x) => x.id === jobId);
    if (j) { j.position = d.position; j.wait_seconds = d.wait_seconds; renderStatus(); }
  });
  es.addEventListener("job", (e) => {
    const d = JSON.parse(e.data), j = jobs.find((x) => x.id === jobId);
    if (j) { j.status = d.status; renderStatus(); }
  });
  es.addEventListener("end", async (e) => {
    es.close(); streams.delete(jobId);
    const st = JSON.parse(e.data).status;
    log("工作結束:" + ({ done: "完成", failed: "失敗", canceled: "已取消" }[st] || st));
    await refresh();
    finished(st);
  });
}
function patchRow(it) {
  const tr = document.querySelector(`tr[data-id="${it.id}"]`);
  if (!tr) return render();
  const tmp = document.createElement("tbody"); tmp.innerHTML = rowHtml(it);
  const open = tr.querySelector("details[open]");
  if (!open) tr.replaceWith(tmp.firstElementChild);       // 選單打開時不要重畫,避免使用者點到一半
}

// 完成通知:分頁標題 + (使用者同意時)瀏覽器通知
function finished(status) {
  if (jobs.some((j) => ACTIVE.has(j.status))) return;
  const text = status === "done" ? "字幕處理完成" : status === "failed" ? "處理失敗,請看日誌" : "已取消";
  if (document.hidden) {
    document.title = (status === "done" ? "✓ " : "! ") + text + " · " + APP_TITLE;
    if ("Notification" in window && Notification.permission === "granted") new Notification(APP_TITLE, { body: text });
  }
  if (status !== "canceled") toast(text);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) document.title = APP_TITLE; });
$("notifyBtn").onclick = async () => { await Notification.requestPermission(); renderStatus(); };

// ------------------------------------------------------------------ 預覽
async function openPreview(id, which) {
  const it = items.find((i) => i.id === id);
  try {
    const d = await api(`/api/items/${id}/preview?which=${which}`);
    $("pvTitle").textContent = d.name;
    $("pvInfo").textContent = `共 ${d.total} 條` + (d.flagged ? `,${d.flagged} 條以黃色標示需要留意(滑鼠移上去看原因)` : "");
    $("pvRows").innerHTML = d.cues.map((c) => `<tr class="${c.flags.length ? "flag" : ""}" title="${esc(c.flags.join("、"))}">
      <td class="ts">${c.n}</td><td class="ts">${esc(c.start)}</td><td class="ts">${esc(c.end)}</td><td>${esc(c.text)}</td></tr>`).join("");
    const other = which === "srt" ? "translated" : "srt";
    const hasOther = it && (it.outputs.includes(other) || (other === "srt" && it.kind === "srt"));
    $("pvSwitch").innerHTML = hasOther ? `<button class="btn" id="pvOther">看${other === "srt" ? "原文" : "譯文"}</button>` : "";
    if (hasOther) $("pvOther").onclick = () => openPreview(id, other);
    const dlg = $("pv"); if (!dlg.open) dlg.showModal();
    dlg.querySelector(".pv-body").scrollTop = 0;
  } catch (x) { toast(x.message); }
}
$("pvClose").onclick = () => $("pv").close();

// ------------------------------------------------------------------ 設定頁
async function loadSettings() {
  const p = await api("/api/prefs");
  $("s_cjk").value = p.max_chars_cjk ?? 18;
  $("s_latin").value = p.max_chars_latin ?? 42;
  $("segMsg").textContent = ""; $("glossMsg").textContent = ""; $("wipeMsg").textContent = "";
  $("gloss").value = (await api("/api/glossary")).text;
  loadKeys();
  try {
    const u = await api("/api/usage"), l = u.limits;
    $("myTier").innerHTML = `等級:<strong>${esc(u.tier_label)}</strong>${u.priority ? " · 優先排隊" : ""}${me.is_admin ? " · 管理者" : ""}`;
    const yes = (b) => (b ? "可以" : "不可以");
    $("myLimits").innerHTML = [
      `瀏覽器抽出的音訊:單檔 ${fmtSize(l.audio_bytes)}`,
      `瀏覽器處理不了時改傳原檔:${l.original_allowed ? "可以,單檔 " + fmtSize(l.original_bytes) : "不可以"}`,
      `單一檔案長度:${fmtMin(l.duration_s / 60)}`,
      `每天處理時數:${fmtMin(u.daily_limit / 60)}`,
      `空間:${fmtSize(u.storage_limit)}`,
      `同時排隊的工作:${l.queued_jobs} 個`,
      `保存天數:音訊 ${l.keep_audio_days} 天、字幕 ${l.keep_srt_days} 天`,
      `離線翻譯:${yes(l.offline_translate)} · API 翻譯:${yes(l.api_translate)}`,
    ].map((t) => `<li>${esc(t)}</li>`).join("");
  } catch { /* 只是說明 */ }
}
// ------------------------------------------------------------------ API 金鑰
async function loadKeys() {
  try {
    const d = await api("/api/keys");
    $("keysOff").hidden = d.allowed; $("keysForms").hidden = !d.allowed;
    document.querySelectorAll(".keyform").forEach((f) => {
      const k = d.keys[f.dataset.provider];
      f.querySelector(".keystate").textContent = k.set ? ` 已設定(末四碼 ${k.last4}${k.model ? ",模型 " + k.model : ""})` : " 尚未設定";
      f.querySelector(".keydel").hidden = !k.set;
      f.querySelector("[name=key]").value = "";
      f.querySelector("[name=key]").placeholder = k.set ? "要換金鑰才需要貼上新的" : f.querySelector("[name=key]").dataset.ph || f.querySelector("[name=key]").placeholder;
      const m = f.querySelector("[name=model]"); if (m) m.value = k.model || "";
    });
  } catch { /* 只是設定頁的一部分 */ }
}
document.querySelectorAll(".keyform").forEach((f) => {
  const keyEl = f.querySelector("[name=key]"); keyEl.dataset.ph = keyEl.placeholder;
  const msg = f.querySelector(".keymsg");
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = { key: keyEl.value.trim() || null };
    const m = f.querySelector("[name=model]"); if (m) body.model = m.value.trim();
    msg.className = "keymsg small mute"; msg.textContent = "確認金鑰中…";
    try {
      const r = await api(`/api/keys/${f.dataset.provider}`, { method: "PUT", json: body });
      await loadKeys();
      msg.className = "keymsg small ok"; msg.textContent = r.checked ? "已儲存,金鑰有效" : "已儲存(這台伺服器設定為不先確認金鑰)";
    } catch (x) { msg.className = "keymsg small bad"; msg.textContent = x.message; }
  });
  f.querySelector(".keydel").onclick = async () => {
    if (!confirm("刪除這把金鑰?之後要用這個 API 翻譯得重新貼上。")) return;
    try { await api(`/api/keys/${f.dataset.provider}`, { method: "DELETE" }); await loadKeys(); msg.className = "keymsg small"; msg.textContent = "已刪除"; }
    catch (x) { msg.className = "keymsg small bad"; msg.textContent = x.message; }
  };
});
function fmtMin(m) {
  m = Math.round(m);
  if (m < 60) return m + " 分鐘";
  return m % 60 ? `${Math.floor(m / 60)} 小時 ${m % 60} 分` : `${m / 60} 小時`;
}
$("saveSeg").onclick = async () => {
  try {
    prefs = await api("/api/prefs", { method: "PUT", json: { max_chars_cjk: +$("s_cjk").value, max_chars_latin: +$("s_latin").value } });
    $("s_cjk").value = prefs.max_chars_cjk; $("s_latin").value = prefs.max_chars_latin;
    $("segMsg").textContent = "已儲存,下次處理時生效。";
  } catch (x) { $("segMsg").textContent = x.message; }
};
$("saveGloss").onclick = async () => {
  try {
    const r = await api("/api/glossary", { method: "PUT", json: { text: $("gloss").value } });
    $("glossMsg").textContent = `已儲存:${r.terms} 個詞,其中 ${r.fixed} 個有固定譯法。下次處理時生效。`;
  } catch (x) { $("glossMsg").textContent = x.message; }
};
$("wipe").onclick = async () => {
  if (!confirm("確定要刪除你的所有檔案、字幕、處理紀錄與詞彙表?這個動作無法復原。")) return;
  try {
    await api("/api/my-data", { method: "DELETE" });
    $("wipeMsg").textContent = "已刪除。"; $("wipeMsg").className = "small ok";
    selected.clear(); logLines = []; $("log").textContent = "";
    prefs = {}; await refresh(); await loadSettings();
  } catch (x) { $("wipeMsg").textContent = x.message; $("wipeMsg").className = "small bad"; }
};

// ------------------------------------------------------------------ 管理頁(核准、等級、個別設定、佇列)
const USER_STATE = { pending: ["等待核准", "warn"], approved: ["可使用", "ok"], blocked: ["已停用", "bad"] };
let admin = null;            // /api/admin/users 的結果
function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000), p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function fmtField(f, v) {
  if (f.type === "bool") return v ? "可以" : "不可以";
  if (f.unit === "MB") return v >= 1024 ? +(v / 1024).toFixed(1) + " GB" : v + " MB";
  if (f.unit === "分鐘") return fmtMin(v);
  return `${v} ${f.unit}`;
}
const hrs = (sec) => (sec >= 3600 ? (sec / 3600).toFixed(1) + " 小時" : Math.round(sec / 60) + " 分");
async function refreshPendingBadge(data) {
  try {
    data = data || await api("/api/admin/users");
    const n = data.users.filter((u) => u.status === "pending").length;
    $("pendingBadge").hidden = n === 0; $("pendingBadge").textContent = n;
  } catch { /* 只是提示 */ }
}
async function loadAdmin() {
  admin = await api("/api/admin/users");
  refreshPendingBadge(admin);
  $("adminNote").hidden = admin.require_approval;
  $("adminNote").textContent = "目前的設定是「新帳號不需核准」(.env 的 WEB_REQUIRE_APPROVAL=0),登入後就能直接使用。";
  renderPending(); renderUsers(); renderTiers(); loadQueue();
  $("dayNote").textContent = admin.day_seconds !== 86400 ? `目前是驗收模式:「1 天」= ${admin.day_seconds} 秒(.env 的 WEB_DAY_SECONDS),正式使用前請拿掉。` : "";
}
const who = (u) => `<div class="fname">${esc(u.name)}</div><div class="meta">${esc(u.email || (u.provider === "dev" ? "開發用登入" : ""))}</div>`;
function renderPending() {
  const pend = admin.users.filter((u) => u.status === "pending");
  $("pendingCount").textContent = pend.length ? `(${pend.length} 個)` : "";
  $("pendingEmpty").hidden = pend.length > 0;
  $("pendingRows").innerHTML = pend.map((u) => `<tr><td>${who(u)}</td><td class="meta">${fmtTime(u.created_at)}</td>
    <td class="actions-cell"><button class="btn primary small-btn" data-uid="${u.id}" data-act="approve-free">核准為一般會員</button>
    <button class="btn primary small-btn" data-uid="${u.id}" data-act="approve-paid">核准為付費會員</button>
    <button class="btn small-btn" data-uid="${u.id}" data-act="block">停用</button></td></tr>`).join("");
}
function renderUsers() {
  const filter = $("userFilter").value;
  $("userRows").innerHTML = admin.users.filter((u) => !filter || u.status === filter).map((u) => {
    const [label, cls] = USER_STATE[u.status] || [u.status, ""];
    const self = u.id === me.id;
    const nOver = Object.keys(u.overrides || {}).length;
    const act = [`<button class="btn small-btn" data-uid="${u.id}" data-act="edit">個別設定</button>`];
    if (!self) {
      if (u.status === "blocked") act.push(`<button class="btn small-btn" data-uid="${u.id}" data-act="restore">恢復</button>`);
      else if (u.status === "pending") act.push(`<button class="btn primary small-btn" data-uid="${u.id}" data-act="approve-free">核准</button>`);
      else act.push(`<button class="btn small-btn danger" data-uid="${u.id}" data-act="block">停用</button>`);
    }
    const near = u.today_seconds > u.daily_limit * 0.8 || u.storage_bytes > u.storage_limit * 0.8;
    return `<tr><td>${who(u)}${self ? '<span class="mute small">(你自己)</span>' : ""}</td>
      <td><span class="badge ${cls}">${label}</span> <span class="badge">${esc(admin.tier_labels[u.tier] || u.tier)}</span>
        ${u.is_admin ? '<span class="badge run">管理者</span>' : ""}${nOver ? `<div class="meta">個別設定 ${nOver} 項</div>` : ""}</td>
      <td class="meta${near ? " warn" : ""}">今天 ${hrs(u.today_seconds)} / ${hrs(u.daily_limit)}<br>${fmtSize(u.storage_bytes)} / ${fmtSize(u.storage_limit)} · ${u.items} 個檔案</td>
      <td class="meta">${fmtTime(u.last_seen)}</td>
      <td class="actions-cell">${act.join("")}</td></tr>`;
  }).join("");
}
$("userFilter").onchange = () => renderUsers();
async function updateUser(uid, body) {
  try { await api(`/api/admin/users/${uid}`, { method: "POST", json: body }); return true; } catch (x) { toast(x.message); return false; }
}
$("view-admin").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]"); if (!b) return;
  const uid = +b.dataset.uid, act = b.dataset.act;
  if (act === "edit") return openOverrides(admin.users.find((u) => u.id === uid));
  if (act === "block" && !confirm("停用這個帳號?對方會立即無法使用,排隊中與處理中的工作也會取消。")) return;
  const body = { "approve-free": { status: "approved", tier: "free" }, "approve-paid": { status: "approved", tier: "paid" },
    block: { status: "blocked" }, restore: { status: "approved" } }[act];
  if (body) { await updateUser(uid, body); loadAdmin(); }
});

// 等級預設值:一張表同時編輯一般與付費
function fieldInput(f, v, name, placeholder) {
  if (f.type === "bool") {
    const opt = (val, t) => `<option value="${val}"${String(v) === val ? " selected" : ""}>${t}</option>`;
    return `<span class="pv-input"><select name="${name}">${placeholder !== undefined ? opt("", "照等級(" + placeholder + ")") : ""}${opt("true", "可以")}${opt("false", "不可以")}</select></span>`;
  }
  return `<span class="pv-input"><input type="number" name="${name}" min="${f.min}" max="${f.max}" step="1" value="${v ?? ""}"
    ${placeholder !== undefined ? `placeholder="${esc(placeholder)}"` : ""}><span class="unit">${esc(f.unit)}</span></span>`;
}
function renderTiers() {
  $("tierMsg").textContent = "";
  $("tierRows").innerHTML = admin.fields.map((f) => `<tr><td>${esc(f.label)}</td>
    <td data-label="一般會員">${fieldInput(f, admin.tiers.free[f.key], "free." + f.key)}</td><td data-label="付費會員">${fieldInput(f, admin.tiers.paid[f.key], "paid." + f.key)}</td></tr>`).join("");
}
function readField(f, el) {
  if (!el || el.value === "") return null;
  return f.type === "bool" ? el.value === "true" : Number(el.value);
}
$("saveTiers").onclick = async () => {
  try {
    for (const tier of ["free", "paid"]) {
      const vals = {};
      for (const f of admin.fields) vals[f.key] = readField(f, document.querySelector(`#tierRows [name="${tier}.${f.key}"]`));
      await api(`/api/admin/tiers/${tier}`, { method: "PUT", json: vals });
    }
    await loadAdmin();
    $("tierMsg").textContent = "已儲存,立即套用";
  } catch (x) { $("tierMsg").textContent = x.message; }
};

$("runCleanup").onclick = async () => {
  $("cleanupMsg").textContent = "清除中…";
  try {
    const r = await api("/api/admin/cleanup", { method: "POST" });
    $("cleanupMsg").textContent = `完成:刪除 ${r.items_deleted} 個檔案、${r.audio_deleted} 個檔案的音訊、${r.uploads_deleted} 個沒傳完的上傳,釋放 ${r.bytes_freed ? fmtSize(r.bytes_freed) : "0 MB"}`;
    loadAdmin();
  } catch (x) { $("cleanupMsg").textContent = x.message; }
};

// 個別設定視窗
let ovUser = null;
function openOverrides(u) {
  ovUser = u;
  const self = u.id === me.id, tierVals = admin.tiers;
  $("ovTitle").textContent = `個別設定:${u.name}`;
  $("ovInfo").textContent = u.email || "";
  $("ovStatus").value = u.status; $("ovStatus").disabled = self;
  $("ovTier").value = u.tier;
  $("ovAdmin").checked = u.is_admin; $("ovAdmin").disabled = self;
  $("ovMsg").textContent = self ? "這是你自己的帳號:不能停用、也不能取消管理者身分。" : "";
  const draw = () => {
    const base = tierVals[$("ovTier").value];
    $("ovRows").innerHTML = admin.fields.map((f) => {
      const has = Object.prototype.hasOwnProperty.call(u.overrides, f.key);
      return `<tr class="${has ? "overridden" : ""}"><td>${esc(f.label)}</td>
        <td>${fieldInput(f, has ? u.overrides[f.key] : null, f.key, f.type === "bool" ? fmtField(f, base[f.key]) : String(base[f.key]))}</td>
        <td class="def">${esc(fmtField(f, base[f.key]))}</td></tr>`;
    }).join("");
  };
  $("ovTier").onchange = () => { u = { ...u, overrides: collectOverrides() }; ovUser = u; draw(); };
  draw();
  $("ov").showModal();
}
function collectOverrides() {
  const out = {};
  for (const f of admin.fields) { const v = readField(f, document.querySelector(`#ovRows [name="${f.key}"]`)); if (v !== null) out[f.key] = v; }
  return out;
}
$("ovClear").onclick = () => { document.querySelectorAll("#ovRows input, #ovRows select").forEach((el) => { el.value = ""; }); };
$("ovCancel").onclick = () => $("ov").close();
$("ovForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { tier: $("ovTier").value, overrides: collectOverrides() };
  if (ovUser.id !== me.id) { body.status = $("ovStatus").value; body.is_admin = $("ovAdmin").checked; }
  if (body.status === "blocked" && ovUser.status !== "blocked"
      && !confirm("停用這個帳號?對方會立即無法使用,排隊中與處理中的工作也會取消。")) return;
  try {
    await api(`/api/admin/users/${ovUser.id}`, { method: "POST", json: body });
    $("ov").close(); toast("已儲存"); loadAdmin();
  } catch (x) { $("ovMsg").textContent = x.message; }
});

// 佇列(只顯示是誰、幾個檔案、多長,不顯示檔名)
async function loadQueue() {
  try {
    const q = await api("/api/admin/queue");
    $("queueNote").textContent = `付費會員(或個別設為優先的人)的工作先處理;一般會員的工作等超過 ${q.priority_max_wait_min} 分鐘後就不再被插隊。`;
    $("queueEmpty").hidden = q.jobs.length > 0;
    let n = 0;
    $("queueRows").innerHTML = q.jobs.map((j) => {
      const pos = j.status === "running" ? '<span class="badge run">處理中</span>' : `第 ${++n} 個`;
      const st = j.status === "running" ? `開始於 ${fmtTime(j.started_at)}` : `送出於 ${fmtTime(j.created_at)}${j.wait_seconds != null ? ",預估" + fmtWait(j.wait_seconds) + "後開始" : ""}`;
      return `<tr><td>${pos}</td><td>${esc(j.user)}${j.priority ? ' <span class="badge ok">優先</span>' : ""}<div class="meta">${esc(j.email || "")}</div></td>
        <td class="meta">${j.files} 個 · ${fmtDur(j.seconds) || "—"}</td><td class="meta">${st}</td></tr>`;
    }).join("");
  } catch { /* 只是提示 */ }
}

boot();
