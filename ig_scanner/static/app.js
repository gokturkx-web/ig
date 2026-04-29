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

let abortController = null;
let allResults = [];

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

function renderRow(payload) {
  const tbody = $("results").querySelector("tbody");
  const tr = document.createElement("tr");
  // payload.status sadece bilinen anahtarlardan biri (STATUS_LABELS), o
  // yüzden CSS sınıfı olarak doğrudan kullanılabilir; ama yine de
  // beklenmedik bir değer gelirse escape ediyoruz.
  const status = payload.status in STATUS_LABELS ? payload.status : "unknown";
  tr.innerHTML = `
    <td>${payload.index + 1}</td>
    <td class="nick">${escapeHtml(payload.username)}</td>
    <td><span class="badge ${status}">${escapeHtml(
    STATUS_LABELS[status] || status
  )}</span></td>
    <td class="code">${escapeHtml(payload.code || "")}</td>
    <td>${escapeHtml(payload.message || "")}</td>
    <td class="proxy">${escapeHtml(payload.proxy || "kendi IP")}</td>
  `;
  tbody.appendChild(tr);
  // En son satıra scroll et
  tr.scrollIntoView({ block: "nearest", behavior: "smooth" });
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
  const html = [`<span class="pill">Toplam <strong>${allResults.length}</strong></span>`];
  for (const k of order) {
    if (counts[k]) {
      html.push(
        `<span class="pill"><span class="badge ${k}">${
          STATUS_LABELS[k] || k
        }</span><strong>${counts[k]}</strong></span>`
      );
    }
  }
  $("summary").innerHTML = html.join("");
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

async function run() {
  const usernames = parseUsernames($("usernames").value);
  if (usernames.length === 0) {
    $("status").textContent = "En az bir nick gir.";
    return;
  }
  $("results").querySelector("tbody").innerHTML = "";
  $("summary").innerHTML = "";
  allResults = [];

  $("run").disabled = true;
  $("stop").disabled = false;
  $("csv").disabled = true;
  $("status").textContent = `Bağlanıyor...`;

  abortController = new AbortController();

  try {
    const resp = await fetch("/api/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        usernames,
        use_server_proxies: $("use-proxies").checked,
        per_request_delay: parseFloat($("delay").value) || 2.5,
      }),
      signal: abortController.signal,
    });

    if (!resp.ok) {
      const text = await resp.text();
      $("status").textContent = `Hata: HTTP ${resp.status} ${text}`;
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (!line) continue;
        const ev = JSON.parse(line);
        handleEvent(ev);
      }
    }
  } catch (e) {
    if (e.name === "AbortError") {
      $("status").textContent = "Durduruldu.";
    } else {
      $("status").textContent = `Hata: ${e.message}`;
    }
  } finally {
    $("run").disabled = false;
    $("stop").disabled = true;
    $("csv").disabled = allResults.length === 0;
    abortController = null;
  }
}

function handleEvent(ev) {
  if (ev.type === "start") {
    $("status").textContent = `${ev.total} nick taranıyor (proxy: ${ev.proxy_count})...`;
  } else if (ev.type === "result") {
    allResults.push(ev);
    renderRow(ev);
    updateSummary();
    $("status").textContent = `${allResults.length} sonuç işlendi.`;
  } else if (ev.type === "done") {
    $("status").textContent = `Bitti. ${allResults.length} sonuç.`;
  }
}

function downloadCsv() {
  const rows = [["username", "status", "code", "message", "proxy"]];
  for (const r of allResults) {
    rows.push([r.username, r.status, r.code || "", r.message || "", r.proxy || ""]);
  }
  const csv = rows
    .map((row) =>
      row
        .map((v) => `"${String(v).replace(/"/g, '""')}"`)
        .join(",")
    )
    .join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "ig-results.csv";
  a.click();
  URL.revokeObjectURL(a.href);
}

document.addEventListener("DOMContentLoaded", () => {
  refreshProxyInfo();
  $("run").addEventListener("click", run);
  $("stop").addEventListener("click", () => abortController && abortController.abort());
  $("csv").addEventListener("click", downloadCsv);
});
