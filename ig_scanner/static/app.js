"use strict";

const $ = (id) => document.getElementById(id);

const STATUS_LABELS = {
  available: "Boş",
  taken: "Alınmış",
  reserved: "Rezerve",
  blocked_term: "Yasaklı Kelime",
  invalid_format: "Geçersiz Biçim",
  rate_limited: "Rate Limit",
  unknown: "Bilinmiyor",
};

let currentJobId = null;
let pollTimer = null;
let allResults = [];
let activeFilter = null;
let activeTab = "manual";

async function refreshProxyInfo() {
  try {
    const r = await fetch("/api/proxies");
    const j = await r.json();
    if (j.count > 0) {
      $("proxy-info").textContent = `(${j.count} proxy yüklü)`;
    } else {
      $("proxy-info").textContent = "(server'da proxies.txt yok — kendi IP)";
      $("use-proxies").checked = false;
    }
  } catch (e) {
    $("proxy-info").textContent = "(durum alınamadı)";
  }
}

function parseUsernames(text) {
  return text
    .split(/[\s,;]+/)
    .map((u) => u.trim())
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

function renderResults() {
  const tbody = $("results").querySelector("tbody");
  tbody.innerHTML = "";
  const filtered = activeFilter
    ? allResults.filter((r) => r.status === activeFilter)
    : allResults;
  for (const r of filtered) {
    const tr = document.createElement("tr");
    const status = r.status in STATUS_LABELS ? r.status : "unknown";
    tr.innerHTML = `
      <td>${r.index + 1}</td>
      <td class="nick">${escapeHtml(r.username)}</td>
      <td><span class="badge ${status}">${escapeHtml(
      STATUS_LABELS[status] || status
    )}</span></td>
      <td class="code">${escapeHtml(r.code || "")}</td>
      <td>${escapeHtml(r.message || r.error || "")}</td>
      <td class="proxy">${escapeHtml(r.proxy || "kendi IP")}</td>
    `;
    tbody.appendChild(tr);
  }
}

function updateSummary() {
  const counts = {};
  for (const r of allResults) counts[r.status] = (counts[r.status] || 0) + 1;
  const order = [
    "available",
    "taken",
    "reserved",
    "blocked_term",
    "rate_limited",
    "invalid_format",
    "unknown",
  ];
  const html = [
    `<span class="pill ${activeFilter ? "" : "active"}" data-status="">Toplam <strong>${allResults.length}</strong></span>`,
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
      renderResults();
    });
  }
}

async function startScan() {
  const body = buildJobRequest();
  if (!body) return;

  $("results").querySelector("tbody").innerHTML = "";
  $("summary").innerHTML = "";
  allResults = [];
  activeFilter = null;
  setRunning(true);
  $("status").textContent = "Başlatılıyor...";

  let resp;
  try {
    resp = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    setRunning(false);
    $("status").textContent = `Bağlantı hatası: ${e.message}`;
    return;
  }
  if (!resp.ok) {
    setRunning(false);
    const text = await resp.text();
    $("status").textContent = `Hata (${resp.status}): ${text}`;
    return;
  }
  const j = await resp.json();
  currentJobId = j.job_id;
  $("status").textContent = `${j.total} nick taranıyor (${j.proxy_count} proxy)...`;
  pollOnce();
}

function buildJobRequest() {
  const useProxies = $("use-proxies").checked;
  const delay = parseFloat($("delay").value) || 2.5;
  if (activeTab === "manual") {
    const usernames = parseUsernames($("usernames").value);
    if (usernames.length === 0) {
      $("status").textContent = "En az bir nick gir.";
      return null;
    }
    return {
      mode: "manual",
      usernames,
      use_server_proxies: useProxies,
      per_request_delay: delay,
    };
  }
  // generated
  const length = parseInt($("gen-length").value, 10);
  const count = parseInt($("gen-count").value, 10);
  if (!Number.isFinite(count) || count < 1) {
    $("status").textContent = "Adet 1 veya daha büyük olmalı.";
    return null;
  }
  return {
    mode: "generated",
    length,
    count,
    alphabet: $("gen-alphabet").value,
    use_server_proxies: useProxies,
    per_request_delay: delay,
  };
}

async function pollOnce() {
  if (!currentJobId) return;
  let resp;
  try {
    resp = await fetch(`/api/jobs/${currentJobId}`);
  } catch (e) {
    $("status").textContent = `Polling hatası: ${e.message} — tekrar deniyorum`;
    pollTimer = setTimeout(pollOnce, 2000);
    return;
  }
  if (!resp.ok) {
    $("status").textContent = `Polling HTTP ${resp.status}`;
    setRunning(false);
    return;
  }
  const job = await resp.json();
  // Sadece yeni sonuçları rendere ekle
  const prevLen = allResults.length;
  allResults = job.results;
  if (allResults.length !== prevLen || activeFilter) renderResults();
  updateSummary();

  if (job.state === "running" || job.state === "queued") {
    $("status").textContent = `${job.processed}/${job.total} işlendi · ${
      job.proxy_count
    } proxy · durum: ${job.state}`;
    pollTimer = setTimeout(pollOnce, 1500);
  } else {
    setRunning(false);
    if (job.state === "done") {
      $("status").textContent = `Bitti. ${job.processed} sonuç.`;
    } else if (job.state === "cancelled") {
      $("status").textContent = `Durduruldu. ${job.processed} sonuç.`;
    } else {
      $("status").textContent = `Hata: ${job.error || job.state}`;
    }
  }
}

async function stopScan() {
  if (!currentJobId) return;
  try {
    await fetch(`/api/jobs/${currentJobId}/cancel`, { method: "POST" });
  } catch (e) {
    /* yoksay */
  }
}

function setRunning(running) {
  $("run").disabled = running;
  $("stop").disabled = !running;
  $("csv").disabled = running || allResults.length === 0;
  if (!running) {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }
}

function downloadCsv() {
  const rows = [["username", "status", "code", "message", "proxy"]];
  for (const r of allResults) {
    rows.push([
      r.username,
      r.status,
      r.code || "",
      r.message || r.error || "",
      r.proxy || "",
    ]);
  }
  const csv = rows
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
  $("run").addEventListener("click", startScan);
  $("stop").addEventListener("click", stopScan);
  $("csv").addEventListener("click", downloadCsv);
});
