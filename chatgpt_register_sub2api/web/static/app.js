const $ = (id) => document.getElementById(id);

const fields = {
  config_path: $("configPath"),
  outlook_enabled: $("outlookEnabled"),
  gmail_enabled: $("gmailEnabled"),
  mailboxes: $("mailboxes"),
  gmail_mailboxes: $("gmailMailboxes"),
  alias_enabled: $("aliasEnabled"),
  alias_limit_per_mailbox: $("aliasLimit"),
  proxy_url: $("proxyUrl"),
  workspace_ids: $("workspaceIds"),
  workspace_enabled: $("workspaceEnabled"),
  workspace_route: $("workspaceRoute"),
  re_login_enabled: $("reLoginEnabled"),
  sub2api_output_file: $("sub2apiOutputFile"),
  health_check: $("healthCheck"),
};

let polling = null;

function toast(message, kind = "ok") {
  const status = $("jobStatus");
  status.textContent = message;
  status.className = `status ${kind}`;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

function readConfigForm() {
  return {
    config_path: fields.config_path.value.trim() || "config.yaml",
    outlook_enabled: fields.outlook_enabled.checked,
    gmail_enabled: fields.gmail_enabled.checked,
    mailboxes: fields.mailboxes.value,
    gmail_mailboxes: fields.gmail_mailboxes.value,
    alias_enabled: fields.alias_enabled.checked,
    alias_limit_per_mailbox: Number(fields.alias_limit_per_mailbox.value || 5),
    proxy_url: fields.proxy_url.value.trim(),
    workspace_ids: fields.workspace_ids.value,
    workspace_enabled: fields.workspace_enabled.checked,
    workspace_route: fields.workspace_route.value,
    re_login_enabled: fields.re_login_enabled.checked,
    sub2api_output_file: fields.sub2api_output_file.value.trim() || "sub2api_bundle.json",
    health_check: fields.health_check.checked,
    archive_runs: true,
    runs_dir: "runs",
    log_level: "INFO",
  };
}

function writeConfigForm(data) {
  fields.config_path.value = data.config_path || "config.yaml";
  fields.outlook_enabled.checked = Boolean(data.outlook_enabled);
  fields.gmail_enabled.checked = Boolean(data.gmail_enabled);
  fields.mailboxes.value = data.mailboxes || "";
  fields.gmail_mailboxes.value = data.gmail_mailboxes || "";
  fields.alias_enabled.checked = Boolean(data.alias_enabled);
  fields.alias_limit_per_mailbox.value = data.alias_limit_per_mailbox || 5;
  fields.proxy_url.value = data.proxy_url || "";
  fields.workspace_ids.value = data.workspace_ids || "";
  fields.workspace_enabled.checked = data.workspace_enabled !== false;
  fields.workspace_route.value = data.workspace_route || "k12_request";
  fields.re_login_enabled.checked = Boolean(data.re_login_enabled);
  fields.sub2api_output_file.value = data.sub2api_output_file || "sub2api_bundle.json";
  fields.health_check.checked = data.health_check !== false;
}

async function loadConfig() {
  const path = encodeURIComponent(fields.config_path.value || "config.yaml");
  const data = await api(`/api/config?path=${path}`);
  writeConfigForm(data);
}

async function saveConfig() {
  await api("/api/config", {
    method: "POST",
    body: JSON.stringify(readConfigForm()),
  });
  toast("配置已保存");
}

async function startRun() {
  await saveConfig();
  const payload = {
    config_path: fields.config_path.value.trim() || "config.yaml",
    count: Number($("count").value || 1),
    threads: Number($("threads").value || 1),
    workspace_ids: fields.workspace_ids.value,
  };
  const data = await api("/api/run", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  toast(`运行中 ${data.job_id}`, "running");
  startPolling();
}

function renderSummary(summary = {}) {
  const box = $("summary");
  const values = [
    summary.registered ?? "-",
    summary.joined ?? "-",
    summary.refreshed ?? "-",
    summary.exported ?? "-",
  ];
  [...box.querySelectorAll("strong")].forEach((item, index) => {
    item.textContent = values[index];
  });

  const currentOutput = $("currentOutput");
  if (summary.output_file) {
    currentOutput.innerHTML = `<a href="/api/download?path=${encodeURIComponent(summary.output_file)}">下载本次 Sub2API JSON</a>`;
  } else {
    currentOutput.innerHTML = "";
  }
}

async function pollJob() {
  const data = await api("/api/job");
  const logs = $("logs");
  logs.textContent = (data.logs || []).join("\n");
  logs.scrollTop = logs.scrollHeight;
  renderSummary(data.summary);

  const status = data.status || "idle";
  if (status === "running" || status === "queued") {
    toast("运行中", "running");
    $("runBtn").disabled = true;
    return;
  }

  $("runBtn").disabled = false;
  if (status === "succeeded") {
    toast("完成", "ok");
    await loadRuns();
  } else if (status === "failed") {
    toast(data.error ? `失败：${data.error}` : "失败", "bad");
    await loadRuns();
  } else {
    toast("空闲");
  }

  if (polling && status !== "running" && status !== "queued") {
    clearInterval(polling);
    polling = null;
  }
}

function startPolling() {
  if (polling) clearInterval(polling);
  pollJob().catch((err) => toast(err.message, "bad"));
  polling = setInterval(() => {
    pollJob().catch((err) => toast(err.message, "bad"));
  }, 1500);
}

async function loadRuns() {
  const data = await api("/api/runs");
  const runs = $("runs");
  runs.innerHTML = "";
  if (!data.runs || data.runs.length === 0) {
    runs.innerHTML = `<div class="empty">暂无结果</div>`;
    return;
  }
  for (const run of data.runs.slice(0, 8)) {
    const item = document.createElement("div");
    item.className = "run-item";
    const output = run.output_file
      ? `<a href="/api/download?path=${encodeURIComponent(run.output_file)}">下载 Sub2API JSON</a>`
      : `<span>无导出</span>`;
    const accounts = run.accounts_file
      ? `<a href="/api/download?path=${encodeURIComponent(run.accounts_file)}">账号记录</a>`
      : `<span>无账号</span>`;
    item.innerHTML = `
      <strong>${run.name}</strong>
      <span>${run.accounts_count} 个账号</span>
      <div>${output}${accounts}</div>
    `;
    runs.appendChild(item);
  }
}

function setupTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
      document.querySelectorAll(".tab-pane").forEach((item) => item.classList.remove("active"));
      tab.classList.add("active");
      $(`tab-${tab.dataset.tab}`).classList.add("active");
    });
  });
}

async function boot() {
  setupTabs();
  $("saveBtn").addEventListener("click", () => saveConfig().catch((err) => toast(err.message, "bad")));
  $("runBtn").addEventListener("click", () => startRun().catch((err) => toast(err.message, "bad")));
  $("refreshRunsBtn").addEventListener("click", () => loadRuns().catch((err) => toast(err.message, "bad")));
  await loadConfig();
  await loadRuns();
  await pollJob();
}

boot().catch((err) => toast(err.message, "bad"));
