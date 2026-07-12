let currentJobId = null;
let pollTimer = null;

function setStatus(text, type = "") {
  const el = document.getElementById("statusText");
  el.textContent = text;
  el.className = "status " + type;
}

function show(id) {
  document.getElementById(id).classList.remove("hidden");
}

function hide(id) {
  document.getElementById(id).classList.add("hidden");
}

async function upload() {
  const fileInput = document.getElementById("fileInput");
  const file = fileInput.files[0];
  if (!file) {
    setStatus("Please choose a file first.", "error");
    return;
  }

  ["report-section", "segments-section", "revenue-section", "download-section", "chat-section"].forEach(hide);

  const btn = document.getElementById("uploadBtn");
  btn.disabled = true;
  setStatus("Uploading " + file.name + "...");
  document.getElementById("progressBar").classList.remove("hidden");

  const formData = new FormData();
  formData.append("file", file);

  try {
    const res = await fetch("/api/upload", { method: "POST", body: formData });
    const data = await res.json();

    if (!res.ok) {
      setStatus("Error: " + (data.detail || "upload failed"), "error");
      btn.disabled = false;
      document.getElementById("progressBar").classList.add("hidden");
      return;
    }

    currentJobId = data.job_id;
    setStatus("Processing... this can take a minute for larger files.");
    pollTimer = setInterval(() => pollStatus(currentJobId), 2000);
  } catch (err) {
    setStatus("Network error: " + err.message, "error");
    btn.disabled = false;
  }
}

async function pollStatus(jobId) {
  try {
    const res = await fetch(`/api/status/${jobId}`);
    const data = await res.json();

    if (data.status === "queued" || data.status === "processing") {
      setStatus("Status: " + data.status + "...");
      return;
    }

    clearInterval(pollTimer);
    document.getElementById("progressBar").classList.add("hidden");
    document.getElementById("uploadBtn").disabled = false;

    if (data.status === "ok") {
      setStatus("Done! Segments ready below.", "success");
      renderReport(data.result.clean_report);
      renderSegments(data.result.metrics, data.result.segments);
      await loadRevenue(jobId);
      renderDownload(jobId);
      show("chat-section");
    } else if (data.status === "rejected") {
      setStatus("File rejected: check the messages below.", "error");
      renderRejection(data.result.report);
    } else {
      setStatus("Error: " + (data.error || "unknown error"), "error");
    }
  } catch (err) {
    clearInterval(pollTimer);
    setStatus("Network error while polling: " + err.message, "error");
    document.getElementById("uploadBtn").disabled = false;
  }
}

function renderReport(report) {
  if (!report) return;
  show("report-section");
  const metricsEl = document.getElementById("reportMetrics");
  metricsEl.innerHTML = `
    ${metricBox("Rows in", report.rows_in)}
    ${metricBox("Rows kept", report.rows_out)}
    ${metricBox("Customers", report.n_customers)}
    ${metricBox("Orders", report.n_orders)}
  `;
  const warnEl = document.getElementById("reportWarnings");
  warnEl.innerHTML = (report.warnings || [])
    .map(w => `<div class="warning-item">${escapeHtml(w)}</div>`)
    .join("");
}

function renderRejection(report) {
  show("report-section");
  document.getElementById("reportMetrics").innerHTML = "";
  const msgs = (report && report.messages) || ["This file could not be processed."];
  document.getElementById("reportWarnings").innerHTML = msgs
    .map(m => `<div class="warning-item">${escapeHtml(m)}</div>`)
    .join("");
}

function renderSegments(metrics, segments) {
  show("segments-section");
  const metricsEl = document.getElementById("clusterMetrics");
  metricsEl.innerHTML = `
    ${metricBox("Segments (k)", metrics.k)}
    ${metricBox("Silhouette", metrics.silhouette)}
    ${metricBox("Davies-Bouldin", metrics.davies_bouldin)}
  `;

  const listEl = document.getElementById("segmentsList");
  listEl.innerHTML = (segments || []).map(seg => {
    const avg = seg.averages || {};
    const priorityClass = "priority-" + (seg.priority || "monitor");
    return `
      <div class="segment-card">
        <div class="segment-top">
          <span class="segment-persona">${escapeHtml(seg.persona)}</span>
          <span class="priority-badge ${priorityClass}">${escapeHtml(seg.priority || "")}</span>
        </div>
        <div class="segment-meta">
          ${seg.size} customers (${seg.share_pct}%) &middot;
          Action: ${escapeHtml(seg.action || "")} via ${escapeHtml(seg.channel || "")}
        </div>
        <div class="segment-meta">
          Avg recency: ${fmtNum(avg.recency)}d &middot;
          frequency: ${fmtNum(avg.frequency)} &middot;
          monetary: ${fmtCurrency(avg.monetary)} &middot;
          AOV: ${fmtCurrency(avg.aov)}
        </div>
        ${seg.reasoning ? `<div class="segment-reasoning">${escapeHtml(seg.reasoning)}</div>` : ""}
      </div>
    `;
  }).join("");
}

async function loadRevenue(jobId) {
  try {
    const res = await fetch(`/api/revenue/${jobId}`);
    if (!res.ok) return;
    const data = await res.json();
    show("revenue-section");
    renderRevenue(data);
  } catch (err) {
    console.error("revenue load failed", err);
  }
}

function renderRevenue(data) {
  const conc = data.concentration || [];
  const atRisk = data.at_risk || {};

  const totalRevenue = conc.reduce((s, r) => s + (r.total_revenue || 0), 0);
  const totalCustomers = conc.reduce((s, r) => s + (r.customer_count || 0), 0);

  document.getElementById("revenueKpis").innerHTML = `
    ${metricBox("Total revenue", fmtCurrency(totalRevenue))}
    ${metricBox("CLV at risk", fmtCurrency(atRisk.total_clv_at_risk))}
    ${metricBox("Customers at risk", atRisk.customer_count_at_risk || 0)}
    ${metricBox("Total customers", totalCustomers)}
  `;

  const alertEl = document.getElementById("atRiskCallout");
  if (atRisk.any_at_risk) {
    alertEl.innerHTML = `<div class="at-risk-alert">
      ${fmtCurrency(atRisk.total_clv_at_risk)} (${atRisk.pct_of_total_clv_at_risk}% of total CLV)
      sits in at-risk segments across ${atRisk.customer_count_at_risk} customers:
      ${(atRisk.matched_segments || []).join(", ")}.
    </div>`;
  } else {
    alertEl.innerHTML = "";
  }

  const table = document.getElementById("revenueTable");
  const rows = conc.map(r => `
    <tr>
      <td>${escapeHtml(r.segment)}</td>
      <td>${r.customer_count}</td>
      <td>${r.pct_of_customers}%</td>
      <td>${fmtCurrency(r.total_revenue)}</td>
      <td>${r.pct_of_revenue}%</td>
      <td>${fmtCurrency(r.avg_revenue_per_customer)}</td>
    </tr>
  `).join("");
  table.innerHTML = `
    <thead><tr>
      <th>Segment</th><th>Customers</th><th>% Customers</th>
      <th>Revenue</th><th>% Revenue</th><th>Avg/Customer</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  `;
}

function renderDownload(jobId) {
  show("download-section");
  document.getElementById("downloadLink").href = `/api/download/${jobId}`;
}

async function sendChat() {
  const input = document.getElementById("chatInput");
  const question = input.value.trim();
  if (!question || !currentJobId) return;

  appendChatMsg("user", question);
  input.value = "";
  appendChatMsg("assistant", "Thinking...", true);

  try {
    const res = await fetch(`/api/chat/${currentJobId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await res.json();
    removeThinkingMsg();

    if (data.error) {
      appendChatMsg("assistant", "Error: " + data.error);
    } else if (data.out_of_scope) {
      appendChatMsg("assistant", data.explanation);
    } else {
      let text = data.explanation || "";
      if (data.result !== undefined && data.result !== null) {
        text += `\n\nResult: ${JSON.stringify(data.result)}`;
      }
      appendChatMsg("assistant", text);
    }
  } catch (err) {
    removeThinkingMsg();
    appendChatMsg("assistant", "Network error: " + err.message);
  }
}

function appendChatMsg(role, text, isThinking = false) {
  const history = document.getElementById("chatHistory");
  const div = document.createElement("div");
  div.className = "chat-msg " + role;
  if (isThinking) div.id = "thinkingMsg";
  div.textContent = text;
  history.appendChild(div);
  history.scrollTop = history.scrollHeight;
}

function removeThinkingMsg() {
  const el = document.getElementById("thinkingMsg");
  if (el) el.remove();
}

document.getElementById("chatInput").addEventListener("keypress", (e) => {
  if (e.key === "Enter") sendChat();
});

function metricBox(label, value) {
  return `<div class="metric-box"><div class="label">${escapeHtml(label)}</div><div class="value">${escapeHtml(String(value))}</div></div>`;
}

function fmtNum(v) {
  return (v === undefined || v === null) ? "-" : Number(v).toFixed(1);
}

function fmtCurrency(v) {
  if (v === undefined || v === null) return "$0.00";
  return "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}