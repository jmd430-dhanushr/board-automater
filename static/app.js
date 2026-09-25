/* ── Board Automater UI ────────────────────────────────────────── */

const LS_KEY = "board_automater_config_v1";

// Empty defaults — the user enters their own board details.
// Tokens/passwords are never pre-filled; the user must enter them.
const BOARD_DEFAULTS = {
  jira: {
    base_url:    "",
    email:       "",
    api_token:   "",
    project_key: "",
  },
  azure: {
    org:     "",
    project: "",
    team:    "",
    pat:     "",
  },
};

// ── State held in memory (mirrored to localStorage) ──────────────
let cfg = {
  jira:    { ...BOARD_DEFAULTS.jira },
  azure:   { ...BOARD_DEFAULTS.azure },
  mapping: { story_status_rules: [], task_status_rules: [], priority_rules: [], assignee_overrides: {} },
};

// ── Helpers ──────────────────────────────────────────────────────

function show(id)  { document.getElementById(id).classList.remove("hidden"); }
function hide(id)  { document.getElementById(id).classList.add("hidden"); }

function setResult(id, content, cls = "") {
  const el = document.getElementById(id);
  el.className = "result " + cls;
  el.innerHTML = content;
}

function setNavActive(id) {
  document.querySelectorAll(".nav-link").forEach(b => b.classList.remove("active"));
  const btn = document.getElementById("nav-" + id);
  if (btn) btn.classList.add("active");
}

function toggleSection(id) {
  const el = document.getElementById("section-" + id);
  if (!el) return;
  const wasOpen = el.classList.contains("open");
  // Close all sections first (true accordion)
  document.querySelectorAll(".accord").forEach(acc => acc.classList.remove("open"));
  // If it was closed, open it now; if it was open, leave it closed
  if (!wasOpen) el.classList.add("open");
  setNavActive(id);
}

function openSection(id) {
  // Close all sections, then open just the target
  document.querySelectorAll(".accord").forEach(acc => acc.classList.remove("open"));
  const el = document.getElementById("section-" + id);
  if (!el) return;
  el.classList.add("open");
  setTimeout(() => el.scrollIntoView({ behavior: "smooth", block: "nearest" }), 30);
  setNavActive(id);
}

function collectCreds() {
  cfg.jira.base_url    = document.getElementById("jira_base_url").value.trim().replace(/\/+$/, "");
  cfg.jira.email       = document.getElementById("jira_email").value.trim();
  cfg.jira.api_token   = document.getElementById("jira_api_token").value.trim();
  cfg.jira.project_key = document.getElementById("jira_project_key").value.trim();
  cfg.azure.org        = document.getElementById("azure_org").value.trim();
  cfg.azure.project    = document.getElementById("azure_project").value.trim();
  cfg.azure.team       = document.getElementById("azure_team").value.trim();
  cfg.azure.pat        = document.getElementById("azure_pat").value.trim();
}

function collectMapping() {
  // Story rules
  cfg.mapping.story_status_rules = [];
  document.querySelectorAll("#story-rules-container .rule-row").forEach(card => {
    const match    = card.querySelector(".rule-match").value.trim();
    const targets  = card.querySelector(".rule-targets").value.trim();
    const noAssign = card.querySelector(".rule-no-assignee").value;
    if (match && targets) {
      const rule = {
        match:         match.split(",").map(s => s.trim()).filter(Boolean),
        target_states: targets.split(",").map(s => s.trim()).filter(Boolean),
        no_assignee:   noAssign === "true" ? true : noAssign === "false" ? false : null,
      };
      cfg.mapping.story_status_rules.push(rule);
    }
  });

  // Task rules
  cfg.mapping.task_status_rules = [];
  document.querySelectorAll("#task-rules-container .rule-row").forEach(card => {
    const match    = card.querySelector(".rule-match").value.trim();
    const targets  = card.querySelector(".rule-targets").value.trim();
    const noAssign = card.querySelector(".rule-no-assignee").value;
    if (match && targets) {
      const rule = {
        match:         match.split(",").map(s => s.trim()).filter(Boolean),
        target_states: targets.split(",").map(s => s.trim()).filter(Boolean),
        no_assignee:   noAssign === "true" ? true : noAssign === "false" ? false : null,
      };
      cfg.mapping.task_status_rules.push(rule);
    }
  });

  // Priority
  cfg.mapping.priority_rules = [];
  document.querySelectorAll("#priority-tbody tr").forEach(row => {
    const cells = row.querySelectorAll("input");
    if (cells.length >= 3) {
      const jp = cells[0].value.trim();
      const num = parseInt(cells[1].value.trim(), 10);
      const lbl = cells[2].value.trim();
      if (jp && !isNaN(num) && lbl) {
        cfg.mapping.priority_rules.push({ jira_priority: jp, numeric: num, custom_label: lbl });
      }
    }
  });

  // Assignee overrides
  cfg.mapping.assignee_overrides = {};
  document.querySelectorAll("#assignee-tbody tr").forEach(row => {
    const cells = row.querySelectorAll("input");
    if (cells.length >= 2) {
      const from = cells[0].value.trim();
      const to   = cells[1].value.trim();
      if (from && to) cfg.mapping.assignee_overrides[from] = to;
    }
  });
}

function buildRequest() {
  collectCreds();
  collectMapping();
  const req = JSON.parse(JSON.stringify(cfg));
  req.date_filter = getDateFilter();  // always present — date range is mandatory
  return req;
}

// ── localStorage ─────────────────────────────────────────────────

function saveConfig() {
  collectCreds();
  collectMapping();
  localStorage.setItem(LS_KEY, JSON.stringify(cfg));
  setResult("conn-result", "✓ Configuration saved to browser storage.", "ok");
}

function resetCreds() {
  localStorage.removeItem(LS_KEY);
  // Restore board defaults (tokens cleared for security)
  applyCredsToForm(BOARD_DEFAULTS.jira, BOARD_DEFAULTS.azure);
  document.getElementById("jira_api_token").value = "";
  document.getElementById("azure_pat").value = "";
  setResult("conn-result", "Fields reset to board defaults. Enter your API tokens and save.", "");
}

function loadConfig() {
  const raw = localStorage.getItem(LS_KEY);
  if (!raw) return;
  try { cfg = JSON.parse(raw); } catch { return; }

  applyCredsToForm(cfg.jira || {}, cfg.azure || {});

  if (cfg.mapping) {
    if (cfg.mapping.story_status_rules?.length)  renderStoryRules(cfg.mapping.story_status_rules);
    if (cfg.mapping.task_status_rules?.length)   renderTaskRules(cfg.mapping.task_status_rules);
    if (cfg.mapping.priority_rules?.length)      renderPriorityTable(cfg.mapping.priority_rules);
    renderAssigneeTable(cfg.mapping.assignee_overrides || {});
  }
}

// ── Status rule rendering ─────────────────────────────────────────

function ruleRowHtml(rule) {
  const match   = (rule.match || []).join(", ");
  const targets = (rule.target_states || []).join(", ");
  const naVal   = rule.no_assignee === true  ? "true"
                : rule.no_assignee === false ? "false"
                : "";
  return `<div class="rule-row">
    <div>
      <div class="rule-col-label">Keywords</div>
      <input type="text" class="field-input rule-match" value="${escHtml(match)}" placeholder="in progress">
    </div>
    <div class="rule-arrow">→</div>
    <div>
      <div class="rule-col-label">Azure states</div>
      <input type="text" class="field-input rule-targets" value="${escHtml(targets)}" placeholder="Doing, In Progress">
    </div>
    <div>
      <div class="rule-col-label">Assignee filter</div>
      <select class="rule-select rule-no-assignee">
        <option value=""      ${naVal===""     ?"selected":""}>Any</option>
        <option value="true"  ${naVal==="true" ?"selected":""}>No assignee</option>
        <option value="false" ${naVal==="false"?"selected":""}>Has assignee</option>
      </select>
    </div>
    <button class="btn-del" onclick="this.closest('.rule-row').remove()" title="Remove rule">✕</button>
  </div>`;
}

function renderStoryRules(rules) {
  document.getElementById("story-rules-container").innerHTML = rules.map(ruleRowHtml).join("");
}
function renderTaskRules(rules) {
  document.getElementById("task-rules-container").innerHTML = rules.map(ruleRowHtml).join("");
}

function addStatusRule(type) {
  const el  = document.getElementById(type + "-rules-container");
  const div = document.createElement("div");
  div.innerHTML = ruleRowHtml({ match: [], target_states: [], no_assignee: null });
  el.appendChild(div.firstElementChild);
}

// ── Priority table ────────────────────────────────────────────────

function renderPriorityTable(rules) {
  const tbody = document.getElementById("priority-tbody");
  tbody.innerHTML = "";
  rules.forEach(r => tbody.insertAdjacentHTML("beforeend", priorityRowHtml(r)));
}

function priorityRowHtml(r) {
  return `<tr>
    <td><input type="text"   value="${escHtml(r.jira_priority)}"  placeholder="high"></td>
    <td><input type="number" value="${r.numeric}"                  placeholder="2" min="1" max="10" style="width:70px"></td>
    <td><input type="text"   value="${escHtml(r.custom_label)}"   placeholder="2 - High"></td>
    <td><button class="btn-del" onclick="this.closest('tr').remove()">✕</button></td>
  </tr>`;
}

function addPriorityRow() {
  document.getElementById("priority-tbody").insertAdjacentHTML(
    "beforeend", priorityRowHtml({ jira_priority: "", numeric: 3, custom_label: "" })
  );
}

// ── Assignee table ────────────────────────────────────────────────

function renderAssigneeTable(overrides) {
  const tbody = document.getElementById("assignee-tbody");
  tbody.innerHTML = "";
  Object.entries(overrides).forEach(([from, to]) => {
    tbody.insertAdjacentHTML("beforeend", assigneeRowHtml(from, to));
  });
}

function assigneeRowHtml(from, to) {
  return `<tr>
    <td><input type="email" value="${escHtml(from)}" placeholder="jira@example.com"></td>
    <td><input type="email" value="${escHtml(to)}"   placeholder="azure@company.com"></td>
    <td><button class="btn-del" onclick="this.closest('tr').remove()">✕</button></td>
  </tr>`;
}

function addAssigneeRow() {
  document.getElementById("assignee-tbody").insertAdjacentHTML("beforeend", assigneeRowHtml("", ""));
}

// ── Connection test ───────────────────────────────────────────────

async function testConnection() {
  collectCreds();
  setResult("conn-result", "⏳ Testing connection…", "");

  let res;
  try {
    const r = await fetch("/api/test-connection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jira: cfg.jira, azure: cfg.azure, mapping: cfg.mapping }),
    });
    res = await r.json();
  } catch (e) {
    setResult("conn-result", "❌ Request failed: " + e, "error");
    return;
  }

  if (res.all_ok) {
    setResult("conn-result",
      `✅ Jira OK — logged in as <strong>${escHtml(res.jira_user)}</strong><br>` +
      `✅ Azure OK — project <strong>${escHtml(res.azure_project)}</strong>`,
      "ok");
  } else {
    const errs = (res.errors || []).map(e => `• ${escHtml(e)}`).join("<br>");
    const jiraLine  = res.jira_ok  ? "✅ Jira OK"  : "❌ Jira FAILED";
    const azureLine = res.azure_ok ? "✅ Azure OK" : "❌ Azure FAILED";
    setResult("conn-result", `${jiraLine}<br>${azureLine}<br><br>${errs}`, "error");
  }
}

// ── Sync ─────────────────────────────────────────────────────────

function confirmSync() {
  if (!confirm("Run LIVE sync? This will create/update work items in Azure DevOps.")) return;
  runSync(false);
}

async function runSync(dryRun) {
  // Mandatory date range guard — never sync without bounds
  if (!_hasValidRange()) {
    setResult("sync-result",
      "⚠️ <strong>Date range is required.</strong> Choose a date range above before running a sync. " +
      "Syncing without date bounds risks migrating every Jira ticket to Azure DevOps.", "error");
    show("sync-result");
    return;
  }

  const endpoint = dryRun ? "/api/sync/preview" : "/api/sync/run";
  document.getElementById("sync-spinner").classList.add("active");
  hide("sync-result");

  let res;
  try {
    const r = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildRequest()),
    });
    res = await r.json();
  } catch (e) {
    document.getElementById("sync-spinner").classList.remove("active");
    setResult("sync-result", "❌ Request failed: " + e, "error");
    return;
  }

  document.getElementById("sync-spinner").classList.remove("active");

  if (res.status === "error") {
    setResult("sync-result", "❌ " + escHtml(res.error || "Unknown error"), "error");
    return;
  }

  const syncrDiv = document.getElementById("sync-result");
  syncrDiv.innerHTML = buildSyncResultHtml(res, dryRun);
  syncrDiv.className = "srp-host";
}

function buildSyncResultHtml(res, dryRun) {
  const s   = res.summary || {};
  const det = res.details || {};
  const created = det.created || [];
  const updated = det.updated || [];
  const skipped = det.skipped || [];
  const failed  = det.failed  || [];

  const totalFound = s.total_jira_tickets
    || (created.length + updated.length + skipped.length + failed.length);
  const now     = new Date();
  const timeStr = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  const badgeCls  = dryRun ? "srp-badge--preview" : "srp-badge--live";
  const badgeText = dryRun ? "Dry Run Preview"     : "Sync Completed";
  const cLabel    = dryRun ? "to create" : "created";
  const uLabel    = dryRun ? "to update" : "updated";

  let h = `<div class="srp">`;

  // ── header ──────────────────────────────────────────────────────
  h += `<div class="srp-head">
    <div class="srp-head-left">
      <span class="srp-badge ${badgeCls}">${badgeText}</span>
      <span class="srp-meta">${totalFound} ticket${totalFound !== 1 ? "s" : ""} found in Jira</span>
    </div>
    <span class="srp-meta">${timeStr}</span>
  </div>`;

  // ── filter bar ──────────────────────────────────────────────────
  if (res.date_filter_applied) {
    const df   = res.date_filter_applied;
    const fMap = { updated: "last updated", created: "created", either: "created or updated" };
    const fl   = fMap[df.field] || df.field;
    const rng  = df.from_date && df.to_date ? `${df.from_date} → ${df.to_date}`
               : df.from_date               ? `from ${df.from_date}`
               :                              `up to ${df.to_date}`;
    h += `<div class="srp-filter">Tickets <strong>${fl}</strong> &middot; <strong>${escHtml(rng)}</strong></div>`;
  }

  // ── stats ───────────────────────────────────────────────────────
  h += `<div class="srp-stats">
    <div class="srp-stat srp-stat--create">
      <div class="srp-stat-num">${created.length}</div>
      <div class="srp-stat-label">${cLabel}</div>
    </div>
    <div class="srp-stat srp-stat--update">
      <div class="srp-stat-num">${updated.length}</div>
      <div class="srp-stat-label">${uLabel}</div>
    </div>
    <div class="srp-stat srp-stat--skip">
      <div class="srp-stat-num">${skipped.length}</div>
      <div class="srp-stat-label">skipped</div>
    </div>
    <div class="srp-stat srp-stat--fail">
      <div class="srp-stat-num">${failed.length}</div>
      <div class="srp-stat-label">failed</div>
    </div>
  </div>`;

  // ── item lists ──────────────────────────────────────────────────
  const hasAny = created.length || updated.length || skipped.length || failed.length;
  if (!hasAny) {
    h += `<div class="srp-empty">No tickets matched the selected date range.</div>`;
  } else {
    const sections = [
      { items: created, cls: "create", label: dryRun ? "To Create" : "Created" },
      { items: updated, cls: "update", label: dryRun ? "To Update" : "Updated" },
      { items: skipped, cls: "skip",   label: "Skipped" },
      { items: failed,  cls: "fail",   label: "Failed"  },
    ];
    h += `<div class="srp-sections">`;
    sections.forEach(({ items, cls, label }) => {
      if (!items.length) return;
      h += `<div class="srp-section srp-section--${cls}">
        <div class="srp-section-hd">
          <span>${label}</span>
          <span class="srp-section-count">${items.length}</span>
        </div>
        <div class="srp-items">`;
      items.forEach(i => {
        const key   = escHtml(i.jira_key   || "");
        const title = escHtml(i.jira_title || "");
        const extra = i.error
          ? ` <span class="srp-err">${escHtml(i.error)}</span>`
          : (cls === "create" && i.result?.azure_id)
            ? ` <span class="srp-az">&middot; Azure #${i.result.azure_id}</span>`
            : "";
        h += `<div class="srp-row srp-row--${cls}">
          <span class="srp-key">${key}</span>
          <span class="srp-title">${title}${extra}</span>
          <span class="srp-tag srp-tag--${cls}">${label}</span>
        </div>`;
      });
      h += `</div></div>`;
    });
    h += `</div>`;
  }

  h += `</div>`; // .srp
  return h;
}

// ── Date Filter ───────────────────────────────────────────────────

// dfState.from/to are always set (date range is mandatory — never null after boot)
let dfState = { preset: "this-week", from: null, to: null };

function _fmt(d) {
  // Use local date parts — toISOString() is UTC and shows wrong date in IST (+5:30)
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function _toMonday(d) {
  const day = d.getDay() || 7;
  const mon = new Date(d);
  mon.setDate(d.getDate() - day + 1);
  return mon;
}

function _hasValidRange() {
  return !!(dfState.from || dfState.to);
}

function setPreset(preset) {
  dfState.preset = preset;
  document.querySelectorAll(".df-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.preset === preset);
  });
  const customRow = document.getElementById("df-custom");
  const today = new Date(); today.setHours(0, 0, 0, 0);

  if (preset === "custom") {
    customRow.classList.remove("hidden");
    dfState.from = document.getElementById("df-from").value || null;
    dfState.to   = document.getElementById("df-to").value   || null;
  } else {
    customRow.classList.add("hidden");
    switch (preset) {
      case "today":
        dfState.from = _fmt(today); dfState.to = _fmt(today); break;
      case "this-week": {
        dfState.from = _fmt(_toMonday(today)); dfState.to = _fmt(today); break;
      }
      case "last-week": {
        const thisWeekMon = _toMonday(today);
        const lastMon = new Date(thisWeekMon); lastMon.setDate(thisWeekMon.getDate() - 7);
        const lastSun = new Date(lastMon);     lastSun.setDate(lastMon.getDate() + 6);
        dfState.from = _fmt(lastMon); dfState.to = _fmt(lastSun); break;
      }
      case "7d": {
        const d = new Date(today); d.setDate(today.getDate() - 6);
        dfState.from = _fmt(d); dfState.to = _fmt(today); break;
      }
      case "30d": {
        const d = new Date(today); d.setDate(today.getDate() - 29);
        dfState.from = _fmt(d); dfState.to = _fmt(today); break;
      }
    }
  }
  updateDfSummary();
}

function onCustomDateChange() {
  dfState.from = document.getElementById("df-from").value || null;
  dfState.to   = document.getElementById("df-to").value   || null;
  updateDfSummary();
}

function updateDfSummary() {
  const el = document.getElementById("df-summary");
  if (!el) return;

  if (!dfState.from && !dfState.to) {
    el.className = "df-summary df-summary--error";
    el.innerHTML = "⚠️ Date range is required. Enter a From and/or To date to proceed.";
    const sub = document.getElementById("sync-header-sub");
    if (sub) sub.textContent = "No date range selected — required before syncing";
    return;
  }

  el.className = "df-summary";
  const field = document.getElementById("df-field-select")?.value || "updated";
  const labels = { updated: "last updated", created: "created", either: "created or updated" };
  const fLabel = labels[field] || "updated";
  let headerSub = "";
  if (dfState.from && dfState.to) {
    el.innerHTML = `Only tickets <strong>${fLabel}</strong> between `
      + `<strong>${dfState.from}</strong> and <strong>${dfState.to}</strong> will be synced.`;
    headerSub = `${dfState.from} → ${dfState.to}`;
  } else if (dfState.from) {
    el.innerHTML = `Only tickets <strong>${fLabel}</strong> on or after `
      + `<strong>${dfState.from}</strong> will be synced.`;
    headerSub = `From ${dfState.from}`;
  } else {
    el.innerHTML = `Only tickets <strong>${fLabel}</strong> on or before `
      + `<strong>${dfState.to}</strong> will be synced.`;
    headerSub = `Up to ${dfState.to}`;
  }
  const sub = document.getElementById("sync-header-sub");
  if (sub) sub.textContent = headerSub;
}

function getDateFilter() {
  // Always returns a filter object — date range is mandatory
  return {
    field:     document.getElementById("df-field-select")?.value || "updated",
    from_date: dfState.from || null,
    to_date:   dfState.to   || null,
  };
}

// ── Utilities ─────────────────────────────────────────────────────

function escHtml(s) {
  return String(s || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ── Boot ──────────────────────────────────────────────────────────

function applyCredsToForm(jira, azure) {
  const f = (id, val) => { const el = document.getElementById(id); if (el && val) el.value = val; };
  f("jira_base_url",    jira.base_url);
  f("jira_email",       jira.email);
  f("jira_api_token",   jira.api_token);
  f("jira_project_key", jira.project_key);
  f("azure_org",        azure.org);
  f("azure_project",    azure.project);
  f("azure_team",       azure.team);
  f("azure_pat",        azure.pat);
}

async function boot() {
  // 1. Apply known board details as editable defaults
  applyCredsToForm(BOARD_DEFAULTS.jira, BOARD_DEFAULTS.azure);

  // Sidebar: connection is open by default
  setNavActive("connection");

  // Date range is mandatory — default to this week so there's always a safe selection on load
  setPreset("this-week");

  // 2. Load mapping rule defaults from server
  let serverMapping = { story_status_rules: [], task_status_rules: [], priority_rules: [], assignee_overrides: {} };
  try {
    const r = await fetch("/api/default-config");
    serverMapping = await r.json();
  } catch { /* server might not be ready yet */ }

  renderStoryRules(serverMapping.story_status_rules || []);
  renderTaskRules(serverMapping.task_status_rules   || []);
  renderPriorityTable(serverMapping.priority_rules  || []);
  renderAssigneeTable(serverMapping.assignee_overrides || {});

  // 3. Overlay everything with saved localStorage config (if any)
  loadConfig();
}

document.addEventListener("DOMContentLoaded", boot);
