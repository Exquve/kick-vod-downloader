"use strict";
const $ = (s) => document.querySelector(s);
const STATUS = {
  queued: "Sırada", preparing: "Hazırlanıyor", downloading: "İndiriliyor",
  merging: "Birleştiriliyor", splitting: "Bölünüyor", done: "Bitti",
  error: "Hata", canceled: "İptal edildi",
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

function jobHtml(j) {
  const st = STATUS[j.status] || j.status;
  const pct = Math.max(0, Math.min(100, j.percent || 0));
  let html = `<div class="card job" data-id="${j.id}">
    <div class="top"><div><div class="title">${esc(j.title || (ACTIVE.has(j.status) || j.status === "queued" ? "Bilgi alınıyor…" : "Başlıksız video"))}</div>
    <div class="url">${esc(j.url)}</div></div>
    <span class="badge ${j.status}">${st}</span></div>`;
  if (ACTIVE.has(j.status) || j.status === "queued") {
    const stats = [];
    if (j.status !== "queued") stats.push(`%${pct.toFixed(1)}`);
    if (j.status === "downloading") {
      if (j.speed) stats.push(fmtBytes(j.speed) + "/sn");
      if (j.eta != null) stats.push("Kalan: " + fmtTime(j.eta));
      if (j.total) stats.push(fmtBytes(j.downloaded) + " / " + fmtBytes(j.total));
    }
    if (j.chunk) stats.push(j.chunk + " dk parçalar"); else stats.push("Bölme yok");
    html += (j.status === "queued" ? `<div class="stats" style="margin-top:10px">` : `<div class="bar"><i style="width:${pct}%"></i></div><div class="stats">`) + `${stats.map((s) => `<span>${esc(s)}</span>`).join("")}</div>`;
  }
  if (j.error) html += `<div class="err-text">${esc(j.error)}</div>`;
  if (j.files && j.files.length) {
    html += `<div class="files">` + j.files.map((f) => `<div class="file">
      <div class="name">${esc(f.name)}</div><div class="size">${fmtBytes(f.size)}</div>
      <div class="acts"><a class="btn primary" href="${esc(f.download)}" download>İndir</a>
      <a class="btn" href="${esc(f.infuse)}">Infuse</a></div></div>`).join("") + `</div>`;
  }
  html += `<div class="job-acts">`;
  if (ACTIVE.has(j.status) || j.status === "queued") html += `<button data-act="cancel" class="danger">İptal</button>`;
  if (j.status === "error" || j.status === "canceled") html += `<button data-act="retry">Tekrar dene</button>`;
  if (!ACTIVE.has(j.status)) html += `<button data-act="delete" class="danger">Sil</button>`;
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
    $("#notice").innerHTML = !s.ytdlp_ready
      ? `<div class="card warn">${s.ytdlp_error ? "yt-dlp indirilemedi: " + esc(s.ytdlp_error) : "İlk açılış: yt-dlp hazırlanıyor…"}</div>` : "";
  } catch (e) {
    $("#notice").innerHTML = `<div class="card warn">PC'ye ulaşılamıyor: ${esc(e.message)}. Uygulama açık mı?</div>`;
  }
}

$("#jobs").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button[data-act]");
  if (!b) return;
  const id = b.closest(".job").dataset.id, act = b.dataset.act;
  if (act === "delete" && !confirm("İş ve indirilen dosyaları silinsin mi?")) return;
  if (act === "cancel" && !confirm("İndirme iptal edilsin mi?")) return;
  b.disabled = true;
  try {
    if (act === "delete") await api(`/api/jobs/${id}`, { method: "DELETE" });
    else await api(`/api/jobs/${id}/${act}`, { method: "POST" });
  } catch (e) { alert(e.message); }
  lastJson = ""; refresh();
});

$("#addForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const msg = $("#addMsg");
  msg.className = "msg"; msg.textContent = "Ekleniyor…";
  const chunk = document.querySelector("input[name=chunk]:checked").value;
  try {
    await api("/api/jobs", { method: "POST", body: JSON.stringify({ url: $("#url").value, chunk: +chunk }) });
    $("#url").value = ""; msg.textContent = "Sıraya eklendi.";
    setTimeout(() => (msg.textContent = ""), 2500);
    lastJson = ""; refresh();
  } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
});

$("#pasteBtn").addEventListener("click", async () => {
  try { $("#url").value = (await navigator.clipboard.readText()).trim(); }
  catch (_) { $("#url").focus(); }
});

async function loadSettings() {
  const s = await api("/api/settings");
  $("#dir").value = s.download_dir; $("#cookies").value = s.cookies_browser;
  $("#defChunk").value = s.default_chunk; $("#remote").checked = s.remote_enabled;
}
$("#saveSettings").addEventListener("click", async () => {
  const msg = $("#setMsg"); msg.className = "msg";
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify({
      download_dir: $("#dir").value, cookies_browser: $("#cookies").value,
      default_chunk: +$("#defChunk").value, remote_enabled: $("#remote").checked }) });
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
