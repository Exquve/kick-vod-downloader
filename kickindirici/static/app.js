"use strict";
const $ = (s) => document.querySelector(s);
const STATUS = {
  queued: "Sırada", preparing: "Hazırlanıyor", downloading: "İndiriliyor",
  merging: "Birleştiriliyor", splitting: "Bölünüyor", done: "Bitti",
  error: "Hata", canceled: "İptal edildi", paused: "Durduruldu",
};
const ACTIVE = new Set(["preparing", "downloading", "merging", "splitting"]);

function fmtBytes(n) {
  if (n == null) return "";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i >= 2 ? 1 : 0) + " " + u[i];
}
function fmtTime(s) {
  if (s == null || !isFinite(s)) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return h ? `${h} sa ${m} dk` : m ? `${m} dk ${x} sn` : `${x} sn`;
}
function hms(s) {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}`;
}
// "1:23:45", "83:00" (dk:sn) ya da "90" (dakika) -> saniye
function parseTime(t) {
  t = String(t || "").trim().replace(/[.,]/g, ":");
  if (!t) return null;
  if (!/^\d+(:\d{1,2}){0,2}$/.test(t)) return NaN;
  const p = t.split(":").map(Number);
  if (p.length === 1) return p[0] * 60;
  if (p.length === 2) return p[0] * 60 + p[1];
  return p[0] * 3600 + p[1] * 60 + p[2];
}
function esc(t) {
  return String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  let data = {};
  try { data = await r.json(); } catch (_) {}
  if (!r.ok) throw Object.assign(new Error(data.error || ("HTTP " + r.status)), { data });
  return data;
}

function fileActs(f, partIndex) {
  const del = partIndex != null
    ? `<button class="del" data-act="delpart" data-idx="${partIndex}" title="Bu parçayı sil" aria-label="Parçayı sil">🗑</button>` : "";
  return `<div class="acts"><a class="btn primary" href="${esc(f.download)}" download>İndir</a>
    <a class="btn" href="${esc(f.play)}">▶ Oynat</a>${del}</div>`;
}
function partHtml(p, n) {
  const label = n > 1 ? `Parça ${String(p.index).padStart(2, "0")}` : "Video";
  const rng = p.end != null ? `${hms(p.start)} – ${hms(p.end)}` : "";
  let st = { queued: "Sırada", downloading: `İniyor %${(p.percent || 0).toFixed(0)}`, done: "Hazır", deleted: "Silindi" }[p.status] || p.status;
  if (p.status === "downloading" && p.finalizing) st = "Kaydediliyor…";
  if (p.status === "queued" && p.percent > 0) st = `Yarım · %${Math.floor(p.percent)}`;
  if (p.status === "done" && p.size) st = "Hazır · " + fmtBytes(p.size);
  let h = `<div class="part"><div class="ph"><div><span class="pn">${label}</span> <span class="pt">${rng}</span></div>
    <span class="ps ${p.status}">${esc(st)}</span></div>`;
  if (p.status === "downloading" || (p.status === "queued" && p.percent > 0)) h += `<div class="mini ${p.status}"><i style="width:${p.percent || 0}%"></i></div>`;
  if (p.status === "done" && p.download) h += fileActs(p, p.index);
  return h + `</div>`;
}
function jobHtml(j) {
  const st = STATUS[j.status] || j.status;
  const pct = Math.max(0, Math.min(100, j.percent || 0));
  const running = ACTIVE.has(j.status);
  const parts = j.parts || [];
  let html = `<div class="card job" data-id="${j.id}">
    <div class="top"><div><div class="title">${esc(j.title || (running || j.status === "queued" ? "Bilgi alınıyor…" : "Başlıksız video"))}</div>
    <div class="url">${esc(j.url)}</div></div>
    <span class="badge ${j.status}">${st}</span></div>`;
  const stats = [];
  if (j.end != null || j.start) stats.push(`Aralık: ${hms(j.start)} – ${j.end != null ? hms(j.end) : "son"}`);
  if (running || j.status === "queued") {
    if (j.status !== "queued") stats.unshift(`%${pct.toFixed(1)}`);
    if (j.part_index && parts.length > 1) stats.push(`Parça ${j.part_index}/${parts.length}`);
    if (j.status === "downloading") {
      if (j.speed) stats.push(`${(j.speed / 1e6).toFixed(1)} MB/sn · ${Math.round(j.speed * 8 / 1e6)} Mbps`);
      if (j.eta != null) stats.push("Kalan: " + fmtTime(j.eta));
    }
    stats.push(j.chunk ? j.chunk + " dk parçalar" : "Bölme yok");
    if (j.status !== "queued") html += `<div class="bar"><i style="width:${pct}%"></i></div>`;
  }
  if (stats.length) html += `<div class="stats" ${j.status === "queued" || !running ? 'style="margin-top:8px"' : ""}>${stats.map((s) => `<span>${esc(s)}</span>`).join("")}</div>`;
  if (j.error) html += `<div class="err-text">${esc(j.error)}</div>`;
  if (parts.length) {
    html += `<div class="parts">` + parts.map((p) => partHtml(p, parts.length)).join("") + `</div>`;
  } else if (j.files && j.files.length) {
    html += `<div class="files">` + j.files.map((f) => `<div class="file">
      <div class="name">${esc(f.name)}</div><div class="size">${fmtBytes(f.size)}</div>${fileActs(f)}</div>`).join("") + `</div>`;
  }
  html += `<div class="job-acts">`;
  if (running || j.status === "queued") html += `<button data-act="pause">⏸ Durdur</button>`;
  if (j.status === "paused") html += `<button data-act="retry" class="primary">▶ Devam et</button>`;
  const started = parts.some((p) => p.status === "done" || p.percent > 0);
  if (j.status === "error" || j.status === "canceled") html += `<button data-act="retry">${started ? "Kaldığı yerden devam" : "Tekrar dene"}</button>`;
  if (!running) html += `<button data-act="delete" class="danger">Sil</button>`;
  html += `</div></div>`;
  return html;
}

let lastJson = "";
async function refresh() {
  try {
    const s = await api("/api/state");
    const json = JSON.stringify(s.jobs);
    if (json !== lastJson) {
      lastJson = json;
      $("#jobs").innerHTML = s.jobs.length ? s.jobs.map(jobHtml).join("") : `<div class="card empty">Henüz iş yok. Bir Kick VOD linki yapıştırın.</div>`;
    }
    const notes = [];
    if (!s.ytdlp_ready) notes.push(s.ytdlp_error ? "yt-dlp indirilemedi: " + esc(s.ytdlp_error) : "İlk açılış: yt-dlp hazırlanıyor…");
    const u = s.update || {};
    if (u.status === "downloading") notes.push(`Yeni sürüm v${esc(u.latest)} indiriliyor${u.progress != null ? " %" + Math.round(u.progress) : ""}…`);
    if (u.status === "ready") notes.push(`Yeni sürüm v${esc(u.latest)} hazır. İndirmeler bitince uygulama kendini yeniden başlatacak.`);
    if (u.status === "applying") notes.push(`v${esc(u.latest)} sürümüne geçiliyor, sayfa birkaç saniye içinde kendine gelecek…`);
    $("#notice").innerHTML = notes.map((n) => `<div class="card warn">${n}</div>`).join("");
    showUpdate(u);
  } catch (e) {
    $("#notice").innerHTML = `<div class="card warn">PC'ye ulaşılamıyor: ${esc(e.message)}. Uygulama açık mı?</div>`;
  }
}

$("#jobs").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button[data-act]");
  if (!b) return;
  const id = b.closest(".job").dataset.id, act = b.dataset.act;
  if (act === "delpart") {
    const label = "Parça " + String(b.dataset.idx).padStart(2, "0");
    if (!confirm(`${label} silinsin mi? Dosya bilgisayardan da silinir.`)) return;
    b.disabled = true;
    try { await api(`/api/jobs/${id}/parts/${b.dataset.idx}`, { method: "DELETE" }); }
    catch (e) { alert(e.message); }
    lastJson = ""; refresh();
    return;
  }
  if (act === "delete" && !confirm("İş ve indirilen dosyaları silinsin mi?")) return;
  if (act === "cancel" && !confirm("İndirme iptal edilsin mi?")) return;
  // Durdur onay istemez: hiçbir şey kaybolmaz, "Devam et" kaldığı yerden sürdürür
  b.disabled = true;
  try {
    if (act === "delete") await api(`/api/jobs/${id}`, { method: "DELETE" });
    else await api(`/api/jobs/${id}/${act}`, { method: "POST" });
  } catch (e) { alert(e.message); }
  lastJson = ""; refresh();
});

// ---------- link -> önizleme -> aralık seçimi ----------
let video = null;      // {title, duration, thumbnail, uploader, url}
let probeSeq = 0;
const MIN_TAIL = 60;

function chunkMin() { return +document.querySelector("input[name=chunk]:checked").value; }

function planBounds(a, b, chunkSec) {
  const len = b - a;
  if (!chunkSec || len < chunkSec + MIN_TAIL) return [a, b];
  const cuts = [];
  for (let t = chunkSec; t < len; t += chunkSec) cuts.push(t);
  while (cuts.length && len - cuts[cuts.length - 1] < MIN_TAIL) cuts.pop();
  return [a, ...cuts.map((c) => a + c), b];
}

function getRange() {
  const s = parseTime($("#tStart").value), e = parseTime($("#tEnd").value);
  return { s: s == null ? 0 : s, e: e == null ? (video && video.duration) || null : e };
}

function updatePlan() {
  const out = $("#planInfo");
  if (!video) return;
  const { s, e } = getRange();
  const dur = video.duration;
  if (Number.isNaN(s) || Number.isNaN(e)) { out.innerHTML = `<span class="err-text">Süreyi 1:23:45 biçiminde yazın.</span>`; return; }
  if (dur && (e > dur + 1 || s >= e)) { out.innerHTML = `<span class="err-text">Aralık geçersiz (video ${hms(dur)}).</span>`; return; }
  if (!dur) { out.innerHTML = "Süre bilinmiyor: video bütün olarak indirilecek."; return; }
  const b = planBounds(s, e, chunkMin() * 60);
  const n = b.length - 1;
  const list = [];
  for (let i = 0; i < n && i < 6; i++) list.push(`${hms(b[i])}–${hms(b[i + 1])}`);
  if (n > 6) list.push("…");
  out.innerHTML = `<b>${n} parça</b> · toplam ${fmtTime(e - s)}<br>${list.join(" · ")}`;
}

function syncSliders() {
  if (!video || !video.duration) return;
  const { s, e } = getRange();
  if (!Number.isNaN(s)) $("#rStart").value = Math.min(s, video.duration);
  if (!Number.isNaN(e) && e != null) $("#rEnd").value = Math.min(e, video.duration);
  paintTrack();
}
function paintTrack() {
  const max = +$("#rEnd").max || 1;
  const a = +$("#rStart").value, b = +$("#rEnd").value;
  $("#sel").style.left = (a / max * 100) + "%";
  $("#sel").style.width = (Math.max(0, b - a) / max * 100) + "%";
}
function setRange(a, b) {
  $("#tStart").value = hms(a); $("#tEnd").value = hms(b);
  syncSliders(); updatePlan();
}

["rStart", "rEnd"].forEach((id) => $("#" + id).addEventListener("input", () => {
  let a = +$("#rStart").value, b = +$("#rEnd").value;
  if (a > b - 1) { if (id === "rStart") a = b - 1; else b = a + 1; }
  setRange(Math.max(0, a), Math.min(+$("#rEnd").max, b));
}));
["tStart", "tEnd"].forEach((id) => $("#" + id).addEventListener("input", () => { syncSliders(); updatePlan(); }));
document.querySelectorAll("input[name=chunk]").forEach((r) => r.addEventListener("change", updatePlan));
document.querySelectorAll(".quick button").forEach((b) => b.addEventListener("click", () => {
  const d = video && video.duration; if (!d) return;
  if (b.dataset.q === "all") setRange(0, d);
  if (b.dataset.q === "first") setRange(0, Math.min(3600, d));
  if (b.dataset.q === "last") setRange(Math.max(0, d - 3600), d);
}));

function resetPick() { video = null; $("#pick").hidden = true; }

async function probe(url) {
  const seq = ++probeSeq, msg = $("#probeMsg");
  resetPick();
  if (!/^https?:\/\/\S+$/i.test(url)) { msg.className = "msg"; msg.textContent = ""; return; }
  msg.className = "msg"; msg.textContent = "Video bilgisi alınıyor…";
  try {
    const info = await api("/api/probe", { method: "POST", body: JSON.stringify({ url }) });
    if (seq !== probeSeq) return;
    video = { ...info, url };
    msg.textContent = "";
    $("#vtitle").textContent = info.title;
    $("#vmeta").textContent = [info.uploader, info.duration ? "Süre: " + hms(info.duration) : "Süre bilinmiyor"].filter(Boolean).join(" · ");
    const th = $("#thumb");
    if (info.thumbnail) { th.src = info.thumbnail; th.hidden = false; } else th.hidden = true;
    const d = Math.round(info.duration || 0);
    $("#rStart").max = $("#rEnd").max = d || 100;
    $("#dual").hidden = !d;
    document.querySelector(".times").hidden = !d;
    document.querySelector(".quick").hidden = !d;
    $("#pick").hidden = false;
    setRange(0, d);
  } catch (e) {
    if (seq !== probeSeq) return;
    msg.className = "msg err";
    msg.innerHTML = esc(e.message) + ` <button type="button" id="retryProbe">Tekrar dene</button>`;
    $("#retryProbe").onclick = () => probe(url);
  }
}

let probeTimer = null;
$("#url").addEventListener("input", () => {
  clearTimeout(probeTimer);
  probeTimer = setTimeout(() => probe($("#url").value.trim()), 500);
});

$("#pasteBtn").addEventListener("click", async () => {
  try { $("#url").value = (await navigator.clipboard.readText()).trim(); probe($("#url").value); }
  catch (_) { $("#url").focus(); }
});

$("#startBtn").addEventListener("click", async () => {
  const msg = $("#addMsg");
  if (!video) return;
  const { s, e } = getRange();
  if (Number.isNaN(s) || Number.isNaN(e) || (e != null && s >= e)) { msg.className = "msg err"; msg.textContent = "Aralığı kontrol edin."; return; }
  const full = !video.duration || (s <= 0 && e >= video.duration - 0.5);
  msg.className = "msg"; msg.textContent = "Ekleniyor…";
  try {
    await api("/api/jobs", { method: "POST", body: JSON.stringify({
      url: video.url, chunk: chunkMin(), start: s, end: full ? null : e, title: video.title }) });
    $("#url").value = ""; resetPick();
    msg.textContent = "Sıraya eklendi. İlk parça hazır olunca aşağıda görünecek.";
    setTimeout(() => (msg.textContent = ""), 4000);
    lastJson = ""; refresh();
  } catch (err) { msg.className = "msg err"; msg.textContent = err.message; }
});

const UPD = { checking: "Kontrol ediliyor…", uptodate: "Güncel", downloading: "İndiriliyor", ready: "Kurulmaya hazır",
  applying: "Kuruluyor", error: "Hata", idle: "", disabled: "Geliştirme sürümü (otomatik güncelleme kapalı)" };
function showUpdate(u) {
  if (!u || !$("#updInfo")) return;
  let t = `Sürüm ${esc(u.current || "")}`;
  if (u.latest && u.latest !== u.current) t += ` · en son: v${esc(u.latest)}`;
  if (UPD[u.status]) t += ` · ${UPD[u.status]}`;
  if (u.status === "downloading" && u.progress != null) t += ` %${Math.round(u.progress)}`;
  if (u.status === "error" && u.error) t += `: ${esc(u.error)}`;
  $("#updInfo").innerHTML = t;
  $("#updApply").hidden = u.status !== "ready";
}
$("#updCheck").addEventListener("click", async () => {
  try { await api("/api/update/check", { method: "POST" }); refresh(); } catch (e) { alert(e.message); }
});
$("#updApply").addEventListener("click", async () => {
  try { await api("/api/update/apply", { method: "POST" }); refresh(); } catch (e) { alert(e.message); }
});

async function loadSettings() {
  const s = await api("/api/settings");
  $("#dir").value = s.download_dir; $("#cookies").value = s.cookies_browser;
  $("#defChunk").value = s.default_chunk; $("#remote").checked = s.remote_enabled;
  $("#autoUpdate").checked = s.auto_update;
}
$("#saveSettings").addEventListener("click", async () => {
  const msg = $("#setMsg"); msg.className = "msg";
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify({
      download_dir: $("#dir").value, cookies_browser: $("#cookies").value,
      default_chunk: +$("#defChunk").value, remote_enabled: $("#remote").checked,
      auto_update: $("#autoUpdate").checked }) });
    msg.textContent = "Kaydedildi.";
  } catch (e) {
    msg.className = "msg err";
    msg.textContent = e.data && e.data.errors ? Object.values(e.data.errors).join(" ") : e.message;
  }
});
const od = $("#openDir");
if (od) od.addEventListener("click", () => api("/api/open-folder", { method: "POST" }));

loadSettings().catch(() => {});
refresh();
setInterval(refresh, 2000);
