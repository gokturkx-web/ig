"use strict";

const $ = (id) => document.getElementById(id);

const STATUS_LABELS = {
  pending: "Bekliyor",
  available: "Boş",
  taken: "Alınmış",
  reserved: "Rezerve",
  blocked_term: "Yasaklı Kelime",
  invalid_format: "Geçersiz Biçim",
  rate_limited: "Rate Limit",
  unknown: "Bilinmiyor",
};

// Tek satır = bir kullanıcı adı + statüsü + IG cevabı.
// `prepared` aşamasında hepsi pending olur, scan başladıkça in-place güncellenir.
let rows = []; // {index, username, status, code, message, proxy, error}
let currentJobId = null;
let pollTimer = null;
let activeFilter = null;
let activeTab = "manual";

async function refreshProxyInfo() {
  try {
    const r = await fetch("/api/proxies");
    const j = await r.json();
    if (j.count > 0) {
      $("proxy-info").textContent = `(${j.count} proxy yüklü)`;
    } else {
      $("proxy-info").textContent = "(server'da proxy yok — kendi IP)";
      $("use-proxies").checked = false;
    }
  } catch {
    $("proxy-info").textContent = "(durum alınamadı)";
  }
}

function parseUsernames(text) {
  return text
    .split(/[\s,;]+/)
    .map((u) => u.trim().replace(/^@/, ""))
    .filter(Boolean);
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function isValidIgUsername(u) {
  return /^[A-Za-z0-9._]{1,30}$/.test(u);
}

function igProfileUrl(u) {
  return `https://www.instagram.com/${encodeURIComponent(u)}/`;
}

function rowHtml(r) {
  const status = r.status in STATUS_LABELS ? r.status : "unknown";
  const showIgLink =
    (status === "taken" || status === "reserved") && isValidIgUsername(r.username);
  const igLink = showIgLink
    ? `<a class="ig-link" href="${igProfileUrl(r.username)}" target="_blank" rel="noopener">ig →</a>`
    : "";
  return `
    <td>${r.index + 1}</td>
    <td class="nick">${escapeHtml(r.username)}${igLink}</td>
    <td><span class="badge ${status}">${escapeHtml(
    STATUS_LABELS[status] || status
  )}</span></td>
    <td class="code">${escapeHtml(r.code || "")}</td>
    <td>${escapeHtml(r.message || r.error || "")}</td>
    <td class="proxy">${escapeHtml(r.proxy || "—")}</td>
  `;
}

function renderAllRows() {
  const tbody = $("results").querySelector("tbody");
  tbody.innerHTML = "";
  for (const r of rows) {
    if (activeFilter && r.status !== activeFilter) continue;
    const tr = document.createElement("tr");
    tr.dataset.index = r.index;
    tr.innerHTML = rowHtml(r);
    tbody.appendChild(tr);
  }
}

function updateRow(r) {
  rows[r.index] = { ...rows[r.index], ...r };
  // Filter aktifse görünürlük değişebilir; tam render daha basit.
  if (activeFilter) {
    renderAllRows();
  } else {
    const tbody = $("results").querySelector("tbody");
    const tr = tbody.querySelector(`tr[data-index="${r.index}"]`);
    if (tr) tr.innerHTML = rowHtml(rows[r.index]);
    else {
      const ntr = document.createElement("tr");
      ntr.dataset.index = r.index;
      ntr.innerHTML = rowHtml(rows[r.index]);
      tbody.appendChild(ntr);
    }
  }
}

function updateSummary() {
  const counts = {};
  for (const r of rows) counts[r.status] = (counts[r.status] || 0) + 1;
  const order = [
    "pending",
    "available",
    "taken",
    "reserved",
    "blocked_term",
    "rate_limited",
    "invalid_format",
    "unknown",
  ];
  const html = [
    `<span class="pill ${activeFilter ? "" : "active"}" data-status="">Toplam <strong>${rows.length}</strong></span>`,
  ];
  for (const k of order) {
    if (counts[k]) {
      html.push(
        `<span class="pill ${activeFilter === k ? "active" : ""}" data-status="${k}"><span class="badge ${k}">${
          STATUS_LABELS[k] || k
        }</span><strong>${counts[k]}</strong></span>`
      );
    }
  }
  $("summary").innerHTML = html.join("");
  for (const el of $("summary").querySelectorAll(".pill")) {
    el.addEventListener("click", () => {
      activeFilter = el.dataset.status || null;
      updateSummary();
      renderAllRows();
    });
  }
}

function setPreparedList(usernames) {
  rows = usernames.map((u, i) => ({
    index: i,
    username: u,
    status: "pending",
    code: null,
    message: null,
    proxy: null,
    error: null,
  }));
  activeFilter = null;
  renderAllRows();
  updateSummary();
  setButtonsForPrepared();
  $("status").textContent = `${rows.length} nick hazırlandı. "Tara"ya basabilirsin.`;
}

function setButtonsForPrepared() {
  $("run").disabled = rows.length === 0;
  $("stop").disabled = true;
  $("csv").disabled = !rows.some((r) => r.status !== "pending");
  $("clear").disabled = rows.length === 0;
}

function setButtonsForRunning() {
  $("run").disabled = true;
  $("stop").disabled = false;
  $("csv").disabled = true;
  $("clear").disabled = true;
}

function setButtonsForDone() {
  $("run").disabled = rows.length === 0;
  $("stop").disabled = true;
  $("csv").disabled = !rows.some((r) => r.status !== "pending");
  $("clear").disabled = rows.length === 0;
}

async function prepareManual() {
  const list = parseUsernames($("usernames").value);
  if (list.length === 0) {
    $("status").textContent = "En az bir nick gir.";
    return;
  }
  // dedupe sırayı koru
  const seen = new Set();
  const cleaned = [];
  for (const u of list) {
    if (!seen.has(u)) {
      seen.add(u);
      cleaned.push(u);
    }
  }
  setPreparedList(cleaned);
}

async function generateList() {
  const length = parseInt($("gen-length").value, 10);
  const count = parseInt($("gen-count").value, 10);
  const alphabet = $("gen-alphabet").value;
  if (!Number.isFinite(count) || count < 1 || count > 500) {
    $("status").textContent = "Adet 1-500 aralığında olmalı.";
    return;
  }
  $("generate").disabled = true;
  $("status").textContent = "Üretiliyor...";
  try {
    const r = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ length, count, alphabet }),
    });
    if (!r.ok) {
      const text = await r.text();
      $("status").textContent = `Hata: ${text}`;
      return;
    }
    const j = await r.json();
    setPreparedList(j.usernames);
  } catch (e) {
    $("status").textContent = `Bağlantı hatası: ${e.message}`;
  } finally {
    $("generate").disabled = false;
  }
}

function clearList() {
  rows = [];
  activeFilter = null;
  $("results").querySelector("tbody").innerHTML = "";
  $("summary").innerHTML = "";
  $("status").textContent = "";
  setButtonsForPrepared();
}

async function startScan() {
  if (rows.length === 0) return;
  // Sıfırla: pending'lerin durumunu koru, sadece scan başlat.
  // Önceki sonuçları silmek istersen Temizle'ye basıyorsun zaten.
  // Burada tüm satırları pending'e geri çekmek gereksiz; biz hep
  // backend'den index sırasıyla cevap alacağız.
  for (const r of rows) {
    r.status = "pending";
    r.code = null;
    r.message = null;
    r.error = null;
    r.proxy = null;
  }
  renderAllRows();
  updateSummary();

  setButtonsForRunning();
  $("status").textContent = "Başlatılıyor...";

  const useProxies = $("use-proxies").checked;
  const delay = parseFloat($("delay").value) || 2.5;
  const body = {
    mode: "manual",
    usernames: rows.map((r) => r.username),
    use_server_proxies: useProxies,
    per_request_delay: delay,
  };

  let resp;
  try {
    resp = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    setButtonsForDone();
    $("status").textContent = `Bağlantı hatası: ${e.message}`;
    return;
  }
  if (!resp.ok) {
    setButtonsForDone();
    const text = await resp.text();
    $("status").textContent = `Hata (${resp.status}): ${text}`;
    return;
  }
  const j = await resp.json();
  currentJobId = j.job_id;
  $("status").textContent = `${j.total} nick taranıyor (${j.proxy_count} proxy)...`;
  pollOnce();
}

async function pollOnce() {
  if (!currentJobId) return;
  let resp;
  try {
    resp = await fetch(`/api/jobs/${currentJobId}`);
  } catch (e) {
    $("status").textContent = `Polling hatası: ${e.message} — tekrar deniyorum`;
    pollTimer = setTimeout(pollOnce, 2500);
    return;
  }
  if (!resp.ok) {
    $("status").textContent = `Polling HTTP ${resp.status}`;
    setButtonsForDone();
    return;
  }
  const job = await resp.json();
  // Sadece henüz pending olan satırları sunucu sonuçlarıyla güncelle
  for (const res of job.results) {
    const cur = rows[res.index];
    if (!cur) continue;
    if (cur.status === "pending" || cur.status === "rate_limited") {
      updateRow({
        index: res.index,
        username: res.username,
        status: res.status,
        code: res.code,
        message: res.message,
        proxy: res.proxy,
        error: res.error,
      });
    }
  }
  updateSummary();

  if (job.state === "running" || job.state === "queued") {
    $("status").textContent = `${job.processed}/${job.total} işlendi · ${job.proxy_count} proxy`;
    pollTimer = setTimeout(pollOnce, 1500);
  } else {
    setButtonsForDone();
    if (job.state === "done")
      $("status").textContent = `Tamamlandı. ${job.processed} sonuç.`;
    else if (job.state === "cancelled")
      $("status").textContent = `Durduruldu. ${job.processed} sonuç.`;
    else $("status").textContent = `Hata: ${job.error || job.state}`;
    currentJobId = null;
  }
}

async function stopScan() {
  if (!currentJobId) return;
  try {
    await fetch(`/api/jobs/${currentJobId}/cancel`, { method: "POST" });
  } catch {
    /* yoksay */
  }
}

function downloadCsv() {
  const data = [["#", "username", "status", "code", "message", "proxy"]];
  for (const r of rows) {
    data.push([
      r.index + 1,
      r.username,
      r.status,
      r.code || "",
      r.message || r.error || "",
      r.proxy || "",
    ]);
  }
  const csv = data
    .map((row) =>
      row.map((v) => `"${String(v).replace(/"/g, '""')}"`).join(",")
    )
    .join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "ig-results.csv";
  a.click();
  URL.revokeObjectURL(a.href);
}

function updateGenHint() {
  const length = parseInt($("gen-length").value, 10);
  const alphabet = $("gen-alphabet").value;
  const sizes = { alnum: 36, alpha: 26, num: 10, alnum_dot: 38 };
  const space = Math.pow(sizes[alphabet] || 36, length);
  $("gen-hint").textContent = `Toplam mümkün kombinasyon: ${space.toLocaleString(
    "tr-TR"
  )}`;
}

function setupTabs() {
  for (const t of document.querySelectorAll(".tab")) {
    t.addEventListener("click", () => {
      activeTab = t.dataset.tab;
      for (const x of document.querySelectorAll(".tab"))
        x.classList.toggle("active", x === t);
      $("tab-manual").classList.toggle("hidden", activeTab !== "manual");
      $("tab-generated").classList.toggle("hidden", activeTab !== "generated");
    });
  }
}

document.addEventListener("DOMContentLoaded", () => {
  refreshProxyInfo();
  setupTabs();
  updateGenHint();
  $("gen-length").addEventListener("change", updateGenHint);
  $("gen-alphabet").addEventListener("change", updateGenHint);
  $("prepare-manual").addEventListener("click", prepareManual);
  $("generate").addEventListener("click", generateList);
  $("run").addEventListener("click", startScan);
  $("stop").addEventListener("click", stopScan);
  $("csv").addEventListener("click", downloadCsv);
  $("clear").addEventListener("click", clearList);
});
