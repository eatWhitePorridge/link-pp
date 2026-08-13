(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const LINE_H = 24;
  const MAX_LOG = 2000;
  const BATCH_POLL_MS = 1000;
  const HISTORY_KEY = "handoff_history";
  const PREFS_KEY = "handoff_prefs";
  const MAX_HISTORY = 200;

  // ─── Utilities ─────────────────────────────────────────────────────
  function esc(s) { return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
  function nowTime() { return new Date().toLocaleTimeString("zh-CN", { hour12: false }); }
  function nowISO() { return new Date().toISOString().slice(0, 19).replace("T", " "); }

  const APP_BASE = window.location.pathname === "/"
    ? ""
    : window.location.pathname.replace(/\/+$/, "");
  const appUrl = path => `${APP_BASE}${path.startsWith("/") ? path : `/${path}`}`;

  async function api(url, opts = {}) {
    const r = await fetch(appUrl(url), { ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } });
    const ct = r.headers.get("content-type") || "";
    const body = ct.includes("json") ? await r.json() : null;
    if (!r.ok) throw new Error(body?.error || `HTTP ${r.status}`);
    return body;
  }

  // ─── Toast ─────────────────────────────────────────────────────────
  const toastContainer = $("toastContainer");
  function toast(message, type = "info") {
    const el = document.createElement("div");
    el.className = `toast toast-${type}`;
    el.textContent = message;
    toastContainer.appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 200); }, 3000);
  }

  async function copyText(value) {
    const text = String(value || "");
    if (!text) throw new Error("没有可复制的内容");
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch (_) {
        // HTTP IP pages commonly reject the modern Clipboard API.
      }
    }
    const input = document.createElement("textarea");
    input.value = text;
    input.setAttribute("readonly", "");
    input.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0;";
    document.body.appendChild(input);
    input.focus();
    input.select();
    input.setSelectionRange(0, input.value.length);
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } finally {
      input.remove();
    }
    if (!copied) throw new Error("浏览器拒绝了剪贴板操作");
  }

  // ─── AT Parser ─────────────────────────────────────────────────────
  function parseATProfile(token) {
    try {
      const t = token.trim().replace(/^bearer\s+/i, "");
      const parts = t.split(".");
      if (parts.length !== 3) return null;
      const payload = JSON.parse(atob(parts[1].replace(/-/g,"+").replace(/_/g,"/")));
      const profile = payload["https://api.openai.com/profile"] || {};
      const email = profile.email || payload.email || "";
      const name = profile.name || payload.name || "";
      if (!email || !email.includes("@")) return null;
      return { email, name: name || email.split("@")[0] };
    } catch { return null; }
  }

  // ─── Proxy Parser ─────────────────────────────────────────────────
  const SUPPORTED_SCHEMES = new Set(["socks5", "socks5h", "http", "https"]);
  const SCHEME_ALIASES = { socket5: "socks5", socks: "socks5" };

  function parseProxyLine(line, defaultScheme) {
    const value = line.trim();
    if (!value || value.startsWith("#")) return null;
    try {
      let url = value;
      if (!url.includes("://")) {
        if (url.includes("@")) {
          url = `${defaultScheme}://${url}`;
        } else {
          const parts = url.split(":");
          if (parts.length === 2) {
            url = `${defaultScheme}://${parts[0]}:${parts[1]}`;
          } else if (parts.length >= 4) {
            const [host, port, user, ...passParts] = parts;
            const pass = passParts.join(":");
            url = `${defaultScheme}://${encodeURIComponent(user)}:${encodeURIComponent(pass)}@${host}:${port}`;
          } else {
            return { error: "格式无法识别" };
          }
        }
      }
      const parsed = new URL(url);
      let scheme = (SCHEME_ALIASES[parsed.protocol.replace(":", "").toLowerCase()] || parsed.protocol.replace(":", "")).toLowerCase();
      if (!SUPPORTED_SCHEMES.has(scheme)) return { error: `不支持协议 ${scheme}` };
      const host = parsed.hostname;
      const port = parseInt(parsed.port, 10);
      if (!host) return { error: "缺少主机" };
      if (!port || port < 1 || port > 65535) return { error: "端口无效" };
      const hasAuth = !!parsed.username;
      return { scheme, host, port, hasAuth, valid: true };
    } catch (e) {
      return { error: e.message || "解析失败" };
    }
  }
  function renderProxyPreview(textareaId, bodyId, previewId, countId, scheme) {
    const text = $(textareaId).value;
    const lines = text.split(/\r?\n/);
    const body = $(bodyId);
    const preview = $(previewId);
    let validCount = 0;
    let invalidCount = 0;
    const invalidRows = [];
    let idx = 0;
    for (const line of lines) {
      if (!line.trim() || line.trim().startsWith("#")) continue;
      idx++;
      const result = parseProxyLine(line, scheme);
      if (result && result.valid) {
        validCount++;
      } else {
        invalidCount++;
        const err = result ? result.error : "空行";
        if (invalidRows.length < 5) {
          invalidRows.push(`<tr class="proxy-invalid"><td>${idx}</td><td>${esc(line.trim().slice(0, 48))}</td><td class="proxy-status-err">${esc(err)}</td></tr>`);
        }
      }
    }
    if (invalidRows.length) {
      if (invalidCount > invalidRows.length) {
        invalidRows.push(`<tr class="proxy-overflow"><td colspan="3">另有 ${invalidCount - invalidRows.length} 条异常未展开</td></tr>`);
      }
      body.innerHTML = invalidRows.join("");
      preview.hidden = false;
    } else {
      body.innerHTML = "";
      preview.hidden = true;
    }
    $(countId).textContent = `${validCount} 条有效` + (invalidCount ? ` / ${invalidCount} 条无效（仅展示前 5 条）` : "");
  }

  // ─── VLog (Virtual Scrolling Log) ─────────────────────────────────
  class VLog {
    constructor(container) {
      this.container = container;
      this.lines = [];
      this.filter = "all";
      this.pad = document.createElement("div");
      this.pad.style.cssText = "pointer-events:none;";
      this.content = document.createElement("div");
      this.content.style.cssText = "position:absolute;left:0;right:0;";
      this.container.innerHTML = "";
      this.container.style.position = "relative";
      this.container.append(this.pad, this.content);
      this.atBottom = true;
      this.rafId = 0;
      this.dirty = false;
      container.addEventListener("scroll", () => {
        const gap = container.scrollHeight - container.scrollTop - container.clientHeight;
        this.atBottom = gap < LINE_H * 2;
        this.scheduleRender();
      });
    }
    get filteredLines() {
      if (this.filter === "all") return this.lines;
      return this.lines.filter(l => l.level === this.filter);
    }
    push(data) {
      this.lines.push(data);
      if (this.lines.length > MAX_LOG) this.lines.splice(0, this.lines.length - MAX_LOG);
      this.dirty = true;
      this.scheduleRender();
    }
    clear() { this.lines = []; this.content.innerHTML = ""; this.pad.style.height = "0"; this.container.scrollTop = 0; this.atBottom = true; }
    setFilter(f) { this.filter = f; this.dirty = true; this.scheduleRender(); }
    scheduleRender() { if (this.rafId) return; this.rafId = requestAnimationFrame(() => { this.rafId = 0; this.render(); }); }
    render() {
      const lines = this.filteredLines;
      const total = lines.length;
      const viewH = this.container.clientHeight;
      const totalH = total * LINE_H;
      const scrollTop = this.atBottom && this.dirty ? Math.max(0, totalH - viewH) : this.container.scrollTop;
      const startIdx = Math.max(0, Math.floor(scrollTop / LINE_H) - 5);
      const visible = Math.ceil(viewH / LINE_H) + 10;
      const endIdx = Math.min(total, startIdx + visible);
      this.pad.style.height = totalH + "px";
      this.content.style.top = (startIdx * LINE_H) + "px";
      const frag = document.createDocumentFragment();
      for (let i = startIdx; i < endIdx; i++) {
        const d = lines[i];
        const row = document.createElement("div");
        row.className = "log-line";
        row.dataset.level = d.level || "info";
        if (d.highlight) row.classList.add("log-highlight");
        row.innerHTML = `<span class="log-time">${esc(d.time)}</span><span class="log-stage">${esc(d.stage || "job")}</span><span class="log-msg">${esc(d.message || "")}</span>`;
        frag.appendChild(row);
      }
      this.content.replaceChildren(frag);
      if (this.atBottom && this.dirty) this.container.scrollTop = totalH;
      this.dirty = false;
    }
  }

  // ─── State ─────────────────────────────────────────────────────────
  const state = {
    countries: new Map(),
    meta: null,
    currentTab: "single",
    // Single
    jobId: "",
    es: null,
    // Batch
    batchId: "",
    batch: null,
    batchTimer: null,
    batchRequest: null,
    batchRevision: 0,
    bjJobId: "",
    batchLogRequest: null,
  };

  const mainLog = new VLog($("logContainer"));
  let batchLog = null;

  // ─── Tab Router ────────────────────────────────────────────────────
  function switchTab(tab) {
    if (tab !== "batch") {
      stopBatchPolling();
      state.batchLogRequest?.abort();
      state.batchLogRequest = null;
    }
    state.currentTab = tab;
    document.querySelectorAll(".tab").forEach(t => {
      t.classList.toggle("active", t.dataset.tab === tab);
      t.setAttribute("aria-selected", t.dataset.tab === tab ? "true" : "false");
    });
    document.querySelectorAll(".tab-panel").forEach(p => {
      p.classList.toggle("active", p.id === `panel${tab.charAt(0).toUpperCase() + tab.slice(1)}`);
      p.hidden = !p.classList.contains("active");
    });
    if (tab === "history") renderHistory();
    if (tab === "batch") {
      refreshBatchList()
        .then(() => state.currentTab === "batch" && refreshBatch({ force: true }))
        .catch(() => {});
    }
  }
  document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => switchTab(t.dataset.tab)));

  // ─── Country Picker ────────────────────────────────────────────────
  const REGIONS = { "美洲": ["US","CA","BR","MX","AR","CL","CO","PE"], "欧洲": ["GB","DE","FR","ES","IT","NL","BE","IE","PT","AT","CH","SE","NO","DK","FI","PL","CZ","RO","HU","GR"], "亚太": ["JP","KR","SG","MY","TH","PH","ID","IN","TW","HK","AU","NZ"], "中东/非洲": ["IL","AE","ZA"] };
  const COUNTRY_FLAGS = {US:"🇺🇸",BR:"🇧🇷",GB:"🇬🇧",FR:"🇫🇷",DE:"🇩🇪",JP:"🇯🇵",CA:"🇨🇦",AU:"🇦🇺",NZ:"🇳🇿",MX:"🇲🇽",AR:"🇦🇷",CL:"🇨🇱",CO:"🇨🇴",PE:"🇵🇪",ES:"🇪🇸",IT:"🇮🇹",NL:"🇳🇱",BE:"🇧🇪",IE:"🇮🇪",PT:"🇵🇹",AT:"🇦🇹",CH:"🇨🇭",SE:"🇸🇪",NO:"🇳🇴",DK:"🇩🇰",FI:"🇫🇮",PL:"🇵🇱",CZ:"🇨🇿",RO:"🇷🇴",HU:"🇭🇺",GR:"🇬🇷",SG:"🇸🇬",MY:"🇲🇾",TH:"🇹🇭",PH:"🇵🇭",ID:"🇮🇩",IN:"🇮🇳",KR:"🇰🇷",TW:"🇹🇼",HK:"🇭🇰",IL:"🇮🇱",AE:"🇦🇪",ZA:"🇿🇦"};

  function setupCountryPicker(pickerBtnId, labelId, codeInputId, dropdownId, searchId, listId, detailId, detailTextId) {
    const btn = $(pickerBtnId);
    const label = $(labelId);
    const codeInput = $(codeInputId);
    const dropdown = $(dropdownId);
    const searchInput = $(searchId);
    const list = $(listId);
    const detail = detailId ? $(detailId) : null;
    const detailText = detailTextId ? $(detailTextId) : null;

    function setValue(c) {
      if (!c) { codeInput.value = ""; label.textContent = "选择国家"; if (detail) detail.hidden = true; return; }
      codeInput.value = c.code;
      label.textContent = `${COUNTRY_FLAGS[c.code] || ""} ${c.name} (${c.code})`;
      if (detail && detailText) {
        detailText.textContent = c.checkout_country === c.code
          ? `${c.currency} · ${c.locale} · ${c.timezone} · ${c.processor_entity}`
          : `${c.code} 出口 · ${c.checkout_country}/${c.checkout_currency} 账单`;
        detail.hidden = false;
      }
      dropdown.hidden = true;
    }

    function renderList(query) {
      const q = (query || "").trim().toLowerCase();
      let html = "";
      for (const [region, codes] of Object.entries(REGIONS)) {
        const items = codes.map(code => state.countries.get(code)).filter(Boolean).filter(c =>
          !q || [c.name, c.code, c.currency, c.locale].some(v => v.toLowerCase().includes(q))
        );
        if (!items.length) continue;
        html += `<div class="picker-group-title">${esc(region)}</div>`;
        for (const c of items) {
          const sel = c.code === codeInput.value ? " selected" : "";
          html += `<button type="button" class="picker-item${sel}" data-code="${c.code}"><span>${COUNTRY_FLAGS[c.code] || ""} ${esc(c.name)} (${c.code})</span><span class="picker-item-right">${c.currency}</span></button>`;
        }
      }
      list.innerHTML = html || '<div class="empty-state">无匹配</div>';
    }

    btn.addEventListener("click", (e) => { e.stopPropagation(); dropdown.hidden = !dropdown.hidden; if (!dropdown.hidden) { renderList(""); searchInput.value = ""; searchInput.focus(); } });
    searchInput.addEventListener("input", () => renderList(searchInput.value));
    list.addEventListener("click", (e) => {
      const item = e.target.closest(".picker-item");
      if (!item) return;
      const c = state.countries.get(item.dataset.code);
      if (c) setValue(c);
      savePrefs();
    });
    document.addEventListener("click", (e) => { if (!e.target.closest(`#${pickerBtnId}`) && !e.target.closest(`#${dropdownId}`)) dropdown.hidden = true; });
    return setValue;
  }

  const setCk = setupCountryPicker("ckPickerBtn","ckPickerLabel","ckCode","ckDropdown","ckSearch","ckList","ckDetail","ckDetailText");
  const setBCk = setupCountryPicker("bCkPickerBtn","bCkPickerLabel","bCkCode","bCkDropdown","bCkSearch","bCkList",null,null);

  // ─── Status ────────────────────────────────────────────────────────
  function setStatus(s, t) { $("statusChip").dataset.status = s; $("statusText").textContent = t; }

  // ─── AT Input (Single) ─────────────────────────────────────────────
  const atInput = $("atInput");
  const atProfile = $("atProfile");
  function updateATProfile() {
    const p = parseATProfile(atInput.value);
    if (p) {
      $("atEmail").textContent = p.email;
      $("atName").textContent = p.name;
      atProfile.hidden = false;
    } else {
      atProfile.hidden = true;
    }
  }
  atInput.addEventListener("input", updateATProfile);
  atInput.addEventListener("paste", () => setTimeout(updateATProfile, 0));
  $("atToggle").addEventListener("click", () => { atInput.type = atInput.type === "password" ? "text" : "password"; });

  // ─── AT Batch Preview ──────────────────────────────────────────────
  const batchTokensEl = $("batchTokens");
  function updateBatchTokenPreview() {
    const lines = batchTokensEl.value.split(/\r?\n/);
    let count = 0;
    let html = "";
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      if (!line || line.startsWith("#")) continue;
      count++;
      const p = parseATProfile(line);
      if (p) {
        html += `<div class="at-batch-item"><span class="at-line-num">${count}</span><span class="at-line-email">${esc(p.email)}</span></div>`;
      } else {
        html += `<div class="at-batch-item invalid"><span class="at-line-num">${count}</span><span>格式无效</span></div>`;
      }
      if (count >= 50) break; // limit preview
    }
    $("batchTokenCount").textContent = `${count} 条`;
    const preview = $("atBatchPreview");
    if (html) { $("atBatchList").innerHTML = html; preview.hidden = false; }
    else { preview.hidden = true; }
  }
  batchTokensEl.addEventListener("input", updateBatchTokenPreview);

  // ─── Proxy Input Handlers ──────────────────────────────────────────
  let ckProxyTimer;
  $("ckProxies").addEventListener("input", () => { clearTimeout(ckProxyTimer); ckProxyTimer = setTimeout(() => renderProxyPreview("ckProxies","ckProxyBody","ckProxyPreview","ckCount",$("ckScheme").value), 300); });
  $("ckScheme").addEventListener("change", () => renderProxyPreview("ckProxies","ckProxyBody","ckProxyPreview","ckCount",$("ckScheme").value));

  // ─── Log Filters ───────────────────────────────────────────────────
  $("logFilters").addEventListener("click", (e) => {
    const btn = e.target.closest(".filter-btn");
    if (!btn) return;
    $("logFilters").querySelectorAll(".filter-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    mainLog.setFilter(btn.dataset.level);
  });
  $("clearLog").addEventListener("click", () => mainLog.clear());

  // ─── Single Job Flow ───────────────────────────────────────────────
  function closeES() { state.es?.close(); state.es = null; }

  function singlePayload() {
    if (!$("ckCode").value) throw new Error("请选择国家");
    if (!$("ckProxies").value.trim()) throw new Error("请填写代理");
    return {
      country: $("ckCode").value,
      proxy_scheme: $("ckScheme").value,
      proxies: $("ckProxies").value,
      checkout_attempts: $("ckAttempts").value, provider_attempts: $("pvAttempts").value,
    };
  }

  const HIGHLIGHT_MARKERS = ["0 元已确认", "已取得 PayPal BA", "BA 链接"];

  function followJob(jobId) {
    closeES();
    state.jobId = jobId;
    $("singleStopBtn").disabled = false;
    mainLog.clear();
    $("singleResultCard").hidden = true;
    setStatus("running", "提链中");
    const src = new EventSource(appUrl(`/api/jobs/${jobId}/events`));
    state.es = src;
    const labels = { queued:"排队中", running:"提链中", stopping:"停止中", success:"成功", failed:"失败", cancelled:"已停止" };
    src.addEventListener("state", e => {
      const d = JSON.parse(e.data).data;
      setStatus(d.status || "running", labels[d.status] || d.status);
    });
    src.addEventListener("log", e => {
      const d = JSON.parse(e.data).data;
      const highlight = HIGHLIGHT_MARKERS.some(m => (d.message || "").includes(m));
      mainLog.push({ time: nowTime(), level: d.level, stage: d.stage, message: d.message, highlight });
    });
    src.addEventListener("result", e => {
      const r = JSON.parse(e.data).data;
      renderResult(r);
      addHistory({ type: "single", status: "success", email: parseATProfile(atInput.value)?.email || "", result: r, time: nowISO() });
    });
    src.addEventListener("done", e => {
      closeES(); state.jobId = "";
      $("singleStopBtn").disabled = true; $("singleStartBtn").disabled = false;
      const d = JSON.parse(e.data).data;
      if (d.status === "failed" || d.status === "cancelled") {
        addHistory({ type: "single", status: d.status, email: parseATProfile(atInput.value)?.email || "", error: "任务" + (d.status === "failed" ? "失败" : "停止"), time: nowISO() });
      }
    });
    src.onerror = () => { if (state.jobId) mainLog.push({ time: nowTime(), level: "warn", stage: "stream", message: "连接中断，尝试重连..." }); };
  }

  function renderResult(r) {
    setLink($("rPaypal"), r.paypal_approve_url);
    setLink($("rProvider"), r.provider_redirect_url);
    setLink($("rCheckout"), r.checkout_url);
    $("rBaToken").textContent = r.ba_token || "-";
    $("rSession").textContent = r.session_id || "-";
    $("rRoute").textContent = `${r.proxy_country || ""} 出口 · ${r.country} · ${r.currency}`;
    $("singleResultCard").hidden = false;
  }
  function setLink(a, url) { url = String(url || ""); a.href = url || "#"; a.textContent = url || "—"; }

  $("singleForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("singleFormErr").hidden = true;
    $("singleStartBtn").disabled = true;
    try {
      const t = atInput.value.trim();
      if (!t) throw new Error("请填写 AT");
      const body = { access_token: t, ...singlePayload() };
      const res = await api("/api/jobs", { method: "POST", body: JSON.stringify(body) });
      followJob(res.job_id);
      toast("任务已提交", "success");
    } catch (err) {
      $("singleFormErr").textContent = err.message; $("singleFormErr").hidden = false;
      $("singleStartBtn").disabled = false;
      setStatus("failed", "提交失败");
    }
  });

  $("singleStopBtn").addEventListener("click", async () => {
    if (!state.jobId) return;
    try { await api(`/api/jobs/${state.jobId}/cancel`, { method: "POST", body: "{}" }); toast("已发送停止请求", "info"); }
    catch (err) { toast(err.message, "error"); }
  });

  // ─── Copy Buttons ──────────────────────────────────────────────────
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".btn-copy");
    if (!btn) return;
    e.stopPropagation();
    const target = $(btn.dataset.copy);
    const encoded = btn.dataset.copyText;
    const val = encoded
      ? decodeURIComponent(encoded)
      : (target?.href && !target.href.endsWith("#") ? target.href : target?.textContent);
    if (val && val !== "—") {
      const original = btn.textContent;
      try {
        await copyText(val);
      } catch (_) {
        btn.textContent = "复制失败";
        toast("剪贴板不可用", "error");
        setTimeout(() => { btn.textContent = original; }, 1500);
        return;
      }
      btn.textContent = "✓"; btn.classList.add("copied");
      toast("已复制到剪贴板", "success");
      setTimeout(() => { btn.textContent = original; btn.classList.remove("copied"); }, 1500);
    }
  });

  // ─── Batch Tab ─────────────────────────────────────────────────────
  function batchPayload() {
    if (!$("bCkCode").value) throw new Error("请选择国家");
    if (!$("bCkProxies").value.trim()) throw new Error("请填写代理");
    return {
      country: $("bCkCode").value,
      proxy_scheme: $("bScheme").value,
      proxies: $("bCkProxies").value,
      checkout_attempts: $("bCkAttempts").value, provider_attempts: $("bPvAttempts").value,
    };
  }

  $("batchForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("batchFormErr").hidden = true;
    $("batchStartBtn").disabled = true;
    try {
      if (!batchTokensEl.value.trim()) throw new Error("请填写 AT 列表");
      const body = {
        access_tokens: batchTokensEl.value,
        batch_name: $("batchName").value.trim(),
        concurrency: $("bConcurrency").value,
        ...batchPayload(),
      };
      const res = await api("/api/batches", { method: "POST", body: JSON.stringify(body) });
      state.batchId = res.id;
      state.batchRevision = 0;
      toast("批次已提交", "success");
      await refreshBatchList();
      await refreshBatch({ force: true });
      addHistory({ type: "batch", status: "running", name: $("batchName").value.trim() || `批次 ${res.id.slice(0,8)}`, total: res.total, time: nowISO(), batchId: res.id });
    } catch (err) {
      $("batchFormErr").textContent = err.message; $("batchFormErr").hidden = false;
      toast(err.message, "error");
    } finally { $("batchStartBtn").disabled = false; }
  });

  async function refreshBatchList() {
    const res = await api("/api/batches");
    const list = res.batches || [];
    const sel = $("batchSel");
    sel.innerHTML = "";
    if (!list.length) {
      stopBatchPolling();
      state.batchId = "";
      state.batchRevision = 0;
      state.batch = null;
      sel.append(new Option("暂无批次", ""));
      return;
    }
    const nextBatchId = list.some(b => b.id === state.batchId) ? state.batchId : list[0].id;
    if (nextBatchId !== state.batchId) {
      stopBatchPolling();
      state.batchId = nextBatchId;
      state.batchRevision = 0;
      state.batch = null;
    }
    list.forEach(b => sel.append(new Option(`${b.name} · ${b.status}`, b.id)));
    sel.value = state.batchId;
  }

  function terminalReason(error) {
    const text = String(error || "").trim();
    const marker = "仍未取得 PayPal 链接：";
    const markerAt = text.lastIndexOf(marker);
    return markerAt >= 0 ? text.slice(markerAt + marker.length) : (text || "—");
  }

  function setText(id, value) {
    const el = $(id);
    const text = String(value);
    if (el.textContent !== text) el.textContent = text;
  }

  function updateBatchJobRow(row, job) {
    const resultUrl = job.result_url || job.result?.paypal_approve_url || job.result?.provider_redirect_url || "";
    const reason = job.failure_reason || terminalReason(job.error);
    const active = job.id === state.bjJobId;
    const signature = JSON.stringify([job.label, job.status, job.attempt, resultUrl, reason, active]);
    if (row.dataset.signature === signature) return;
    row.dataset.signature = signature;
    row.classList.toggle("active", active);
    row.querySelector(".job-account").textContent = job.label || "#" + job.batch_index;
    const status = row.querySelector(".job-status");
    status.dataset.s = job.status;
    status.textContent = job.status;
    row.querySelector(".job-attempt").textContent = `#${job.attempt || 1}`;
    const result = row.querySelector(".job-result");
    if (resultUrl) {
      const button = document.createElement("button");
      button.className = "btn btn-sm btn-copy";
      button.dataset.copyText = encodeURIComponent(resultUrl);
      button.textContent = "复制BA";
      result.replaceChildren(button);
    } else {
      const message = document.createElement("span");
      message.className = "job-reason";
      message.title = reason;
      message.textContent = reason;
      result.replaceChildren(message);
    }
  }

  function createBatchJobRow(job) {
    const row = document.createElement("div");
    row.className = "batch-job-item";
    row.dataset.job = job.id;
    row.innerHTML = '<span class="job-account"></span><span class="job-status"></span><span class="job-attempt"></span><span class="job-result"></span>';
    updateBatchJobRow(row, job);
    return row;
  }

  function renderBatchJobs(batch) {
    const filter = $("batchJobFilter").value;
    const jobs = (batch.jobs || []).filter(job => filter === "all" || job.status === filter);
    const list = $("batchJobList");
    if (!jobs.length) {
      if (!list.querySelector(".empty-state")) list.innerHTML = '<div class="empty-state">暂无匹配任务</div>';
      return;
    }

    const existing = new Map(
      [...list.querySelectorAll(".batch-job-item")].map(row => [row.dataset.job, row])
    );
    const currentIds = [...existing.keys()];
    const structureChanged = currentIds.length !== jobs.length
      || jobs.some((job, index) => currentIds[index] !== job.id);
    if (structureChanged) {
      const fragment = document.createDocumentFragment();
      for (const job of jobs) {
        const row = existing.get(job.id) || createBatchJobRow(job);
        updateBatchJobRow(row, job);
        fragment.appendChild(row);
      }
      list.replaceChildren(fragment);
      return;
    }
    for (const job of jobs) updateBatchJobRow(existing.get(job.id), job);
  }

  function renderBatch(b) {
    state.batch = b;
    // Progress
    const total = b.total || 0;
    const done = (b.counts?.success || 0) + (b.counts?.failed || 0) + (b.counts?.cancelled || 0);
    $("batchProgress").hidden = false;
    $("progressFill").style.width = total ? `${(done / total * 100).toFixed(1)}%` : "0";
    setText("progressText", `${done}/${total}`);
    // Stats
    $("batchStats").hidden = false;
    setText("sTotal", total);
    setText("sRunning", (b.counts?.running || 0) + (b.counts?.queued || 0));
    setText("sSuccess", b.counts?.success || 0);
    setText("sFailed", b.counts?.failed || 0);
    setText("sQueued", b.counts?.queued || 0);
    // Actions
    const term = ["success","failed","cancelled","partial"].includes(b.status);
    $("batchCancel").disabled = term;
    $("batchRetry").disabled = !(b.retryable_count > 0 && term);
    $("batchDl").href = appUrl(`/api/batches/${b.id}/results.csv`);
    $("batchDl").setAttribute("aria-disabled", "false");
    renderBatchJobs(b);
  }

  function stopBatchPolling() {
    clearTimeout(state.batchTimer);
    state.batchTimer = null;
    state.batchRequest?.abort();
    state.batchRequest = null;
  }

  function scheduleBatchRefresh() {
    clearTimeout(state.batchTimer);
    if (state.currentTab !== "batch") return;
    state.batchTimer = setTimeout(() => refreshBatch(), BATCH_POLL_MS);
  }

  async function refreshBatch({ force = false } = {}) {
    const batchId = state.batchId;
    if (!batchId) return;
    if (force) stopBatchPolling();
    if (state.batchRequest) return;
    const controller = new AbortController();
    state.batchRequest = controller;
    try {
      const revision = force ? 0 : state.batchRevision;
      const query = new URLSearchParams({ compact: "1" });
      if (revision) query.set("after_revision", revision);
      const b = await api(`/api/batches/${batchId}?${query}`, { signal: controller.signal });
      if (batchId !== state.batchId) return;
      state.batchRevision = b.revision || state.batchRevision;
      if (b.unchanged) {
        scheduleBatchRefresh();
        return;
      }
      renderBatch(b);
      if (!["success","failed","cancelled","partial"].includes(b.status)) {
        scheduleBatchRefresh();
      } else {
        updateHistoryBatch(batchId, b);
      }
    } catch (error) {
      if (error.name !== "AbortError") scheduleBatchRefresh();
    } finally {
      if (state.batchRequest === controller) state.batchRequest = null;
    }
  }

  async function selectBatchJob(jobId) {
    state.bjJobId = jobId;
    if (state.batch) renderBatchJobs(state.batch);
    $("bjLabel").textContent = "任务 " + jobId.slice(0, 8);
    $("bjRefresh").disabled = false;
    if (!batchLog) batchLog = new VLog($("bjLogContainer"));
    batchLog.clear();
    state.batchLogRequest?.abort();
    const controller = new AbortController();
    state.batchLogRequest = controller;
    try {
      const res = await api(`/api/jobs/${jobId}/events.json?after=0`, { signal: controller.signal });
      if (state.bjJobId !== jobId) return;
      (res.events || []).filter(e => e.event === "log").forEach(e => {
        batchLog.push({ time: e.timestamp?.slice(11, 19) || "", level: e.data?.level, stage: e.data?.stage, message: e.data?.message });
      });
    } finally {
      if (state.batchLogRequest === controller) state.batchLogRequest = null;
    }
  }

  $("batchRefresh").addEventListener("click", async () => { await refreshBatchList(); await refreshBatch({ force: true }); });
  $("batchSel").addEventListener("change", () => {
    stopBatchPolling();
    state.batchId = $("batchSel").value;
    state.batchRevision = 0;
    state.batch = null;
    refreshBatch({ force: true });
  });
  $("batchJobFilter").addEventListener("change", () => { if (state.batch) renderBatchJobs(state.batch); });
  $("batchCancel").addEventListener("click", async () => {
    if (!state.batchId) return;
    await api(`/api/batches/${state.batchId}/cancel`, { method: "POST", body: "{}" });
    toast("已发送停止请求", "info");
    await refreshBatch({ force: true });
  });
  $("batchRetry").addEventListener("click", async () => {
    if (!state.batchId) return;
    const res = await api(`/api/batches/${state.batchId}/retry`, { method: "POST", body: "{}" });
    toast(`已重试 ${res.created} 个任务`, "success");
    await refreshBatch({ force: true });
  });
  $("bjRefresh").addEventListener("click", () => { if (state.bjJobId) selectBatchJob(state.bjJobId).catch(() => {}); });
  $("batchJobList").addEventListener("click", (e) => {
    const item = e.target.closest(".batch-job-item");
    if (item && item.dataset.job) selectBatchJob(item.dataset.job).catch(() => {});
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopBatchPolling();
    } else if (state.currentTab === "batch" && state.batchId) {
      refreshBatch({ force: true });
    }
  });

  // ─── History ───────────────────────────────────────────────────────
  function getHistory() {
    try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]"); } catch { return []; }
  }
  function saveHistory(list) {
    try { localStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, MAX_HISTORY))); } catch {}
  }
  function addHistory(entry) {
    const list = getHistory();
    list.unshift(entry);
    saveHistory(list);
  }
  function updateHistoryBatch(batchId, batchData) {
    const list = getHistory();
    const idx = list.findIndex(h => h.batchId === batchId);
    if (idx >= 0) {
      list[idx].status = batchData.status;
      list[idx].success = batchData.counts?.success || 0;
      list[idx].failed = batchData.counts?.failed || 0;
      saveHistory(list);
    }
  }

  function renderHistory() {
    const list = getHistory();
    const search = ($("historySearch").value || "").toLowerCase();
    const statusFilter = $("historyStatusFilter").value;
    const filtered = list.filter(h => {
      if (statusFilter !== "all" && h.status !== statusFilter) return false;
      if (search) {
        const text = `${h.email || ""} ${h.name || ""} ${h.result?.ba_token || ""} ${h.result?.paypal_approve_url || ""}`.toLowerCase();
        if (!text.includes(search)) return false;
      }
      return true;
    });
    const container = $("historyList");
    if (!filtered.length) { container.innerHTML = '<div class="empty-state">暂无匹配记录</div>'; return; }
    let html = "";
    for (const h of filtered) {
      const statusColor = h.status === "success" ? "var(--accent)" : h.status === "failed" ? "var(--danger)" : "var(--warn)";
      const statusLabel = h.status === "success" ? "成功" : h.status === "failed" ? "失败" : h.status === "cancelled" ? "停止" : h.status;
      if (h.type === "batch") {
        html += `<div class="history-item">
          <span class="history-time">${esc(h.time || "")}</span>
          <span class="history-account">${esc(h.name || "批次")}<span class="history-ba">${h.success || 0}/${h.total || 0} 成功</span></span>
          <span class="history-status" style="color:${statusColor}">${esc(statusLabel)}</span>
          <span class="history-actions"><button class="btn btn-sm" data-history-detail='${esc(JSON.stringify(h))}'>详情</button></span>
        </div>`;
      } else {
        const ba = h.result?.ba_token || "";
        html += `<div class="history-item">
          <span class="history-time">${esc(h.time || "")}</span>
          <span class="history-account">${esc(h.email || "—")}<span class="history-ba">${esc(ba)}</span></span>
          <span class="history-status" style="color:${statusColor}">${esc(statusLabel)}</span>
          <span class="history-actions">
            ${h.result?.paypal_approve_url ? `<button class="btn btn-sm btn-copy" data-copy-text="${encodeURIComponent(h.result.paypal_approve_url)}">复制BA</button>` : ""}
            <button class="btn btn-sm" data-history-detail='${esc(JSON.stringify(h))}'>详情</button>
          </span>
        </div>`;
      }
    }
    container.innerHTML = html;
  }

  $("historySearch").addEventListener("input", renderHistory);
  $("historyStatusFilter").addEventListener("change", renderHistory);
  $("historyClear").addEventListener("click", () => {
    if (confirm("确定清空所有历史记录？")) { saveHistory([]); renderHistory(); toast("历史已清空", "info"); }
  });
  $("historyExport").addEventListener("click", () => {
    const list = getHistory().filter(h => h.status === "success" && h.result);
    if (!list.length) { toast("没有可导出的成功记录", "error"); return; }
    let csv = "﻿时间,邮箱,BA Token,PayPal链接,国家\n";
    for (const h of list) {
      const r = h.result || {};
      csv += `"${h.time}","${h.email}","${r.ba_token || ""}","${r.paypal_approve_url || ""}","${r.country || ""}"\n`;
    }
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a"); a.href = url; a.download = `history-${Date.now()}.csv`; a.click();
    URL.revokeObjectURL(url);
    toast("CSV 已下载", "success");
  });

  // History detail overlay
  $("historyList").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-history-detail]");
    if (!btn) return;
    try {
      const h = JSON.parse(btn.dataset.historyDetail);
      showHistoryDetail(h);
    } catch {}
  });
  function showHistoryDetail(h) {
    const body = $("historyDetailBody");
    let html = '<div class="detail-section"><h4>基本信息</h4><dl class="detail-kv">';
    html += `<dt>时间</dt><dd>${esc(h.time || "")}</dd>`;
    html += `<dt>类型</dt><dd>${h.type === "batch" ? "批量" : "单任务"}</dd>`;
    html += `<dt>状态</dt><dd>${esc(h.status || "")}</dd>`;
    if (h.email) html += `<dt>账号</dt><dd>${esc(h.email)}</dd>`;
    if (h.error) html += `<dt>错误</dt><dd>${esc(h.error)}</dd>`;
    html += "</dl></div>";
    if (h.result) {
      html += '<div class="detail-section"><h4>结果</h4><dl class="detail-kv">';
      html += `<dt>BA Token</dt><dd>${esc(h.result.ba_token || "")}</dd>`;
      html += `<dt>PayPal</dt><dd>${esc(h.result.paypal_approve_url || "")}</dd>`;
      html += `<dt>Provider</dt><dd>${esc(h.result.provider_redirect_url || "")}</dd>`;
      html += `<dt>Session</dt><dd>${esc(h.result.session_id || "")}</dd>`;
      html += `<dt>链路</dt><dd>${esc(h.result.proxy_country || "")} 出口 · ${esc(h.result.country || "")} · ${esc(h.result.currency || "")}</dd>`;
      html += "</dl></div>";
    }
    body.innerHTML = html;
    $("historyDetailTitle").textContent = h.type === "batch" ? (h.name || "批次详情") : (h.email || "任务详情");
    $("historyDetailOverlay").hidden = false;
  }
  $("historyDetailClose").addEventListener("click", () => { $("historyDetailOverlay").hidden = true; });
  $("historyDetailOverlay").addEventListener("click", (e) => { if (e.target === $("historyDetailOverlay")) $("historyDetailOverlay").hidden = true; });

  // ─── Preferences ───────────────────────────────────────────────────
  function savePrefs() {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify({
        country: $("ckCode").value,
        batchCountry: $("bCkCode").value,
        proxyScheme: $("ckScheme").value,
        batchProxyScheme: $("bScheme").value,
        proxies: $("ckProxies").value,
        batchProxies: $("bCkProxies").value,
        ckAttempts: $("ckAttempts").value, pvAttempts: $("pvAttempts").value,
        bCkAttempts: $("bCkAttempts").value, bPvAttempts: $("bPvAttempts").value,
        bConcurrency: $("bConcurrency").value,
      }));
    } catch {}
  }
  function loadPrefs() {
    try {
      const p = JSON.parse(localStorage.getItem(PREFS_KEY));
      if (!p) return null;
      const normalized = {
        ...p,
        country: p.country || p.ckCode || "",
        batchCountry: p.batchCountry || p.bCkCode || p.country || p.ckCode || "",
        proxyScheme: p.proxyScheme || p.ckScheme || "",
        batchProxyScheme: p.batchProxyScheme || p.proxyScheme || p.ckScheme || "",
        proxies: p.proxies || p.ckProxies || "",
        batchProxies: p.batchProxies || p.bCkProxies || "",
      };
      if (normalized.proxyScheme) $("ckScheme").value = normalized.proxyScheme;
      if (normalized.batchProxyScheme) $("bScheme").value = normalized.batchProxyScheme;
      if (normalized.proxies) $("ckProxies").value = normalized.proxies;
      if (normalized.batchProxies) $("bCkProxies").value = normalized.batchProxies;
      if (p.ckAttempts) $("ckAttempts").value = p.ckAttempts;
      if (p.pvAttempts) $("pvAttempts").value = p.pvAttempts;
      if (p.bCkAttempts) $("bCkAttempts").value = p.bCkAttempts;
      if (p.bPvAttempts) $("bPvAttempts").value = p.bPvAttempts;
      if (p.bConcurrency) $("bConcurrency").value = p.bConcurrency;
      return normalized;
    } catch { return null; }
  }

  // Auto-save on changes
  let prefsTimer = null;
  const scheduleSavePrefs = () => {
    clearTimeout(prefsTimer);
    prefsTimer = setTimeout(savePrefs, 250);
  };
  ["ckScheme","bScheme","ckProxies","bCkProxies","ckAttempts","pvAttempts","bCkAttempts","bPvAttempts","bConcurrency"].forEach(id => {
    $(id).addEventListener("change", savePrefs);
    if ($(id).tagName === "TEXTAREA") $(id).addEventListener("input", scheduleSavePrefs);
  });

  // ─── Init ──────────────────────────────────────────────────────────
  const prefs = loadPrefs();

  api("/api/meta").then(meta => {
    state.meta = meta;
    meta.countries.forEach(c => state.countries.set(c.code, c));
    // Set defaults
    const ckDefault = prefs?.country || meta.defaults.country;
    setCk(state.countries.get(ckDefault));
    setBCk(state.countries.get(prefs?.batchCountry || ckDefault));
    if (!prefs?.proxyScheme) $("ckScheme").value = meta.defaults.proxy_scheme;
    if (!prefs?.batchProxyScheme) $("bScheme").value = meta.defaults.proxy_scheme;
    if (!prefs) {
      $("ckAttempts").value = meta.defaults.checkout_attempts;
      $("pvAttempts").value = meta.defaults.provider_attempts;
      $("bCkAttempts").value = meta.defaults.checkout_attempts;
      $("bPvAttempts").value = meta.defaults.provider_attempts;
      $("bConcurrency").value = meta.defaults.batch_concurrency;
    }
    $("bConcurrency").max = meta.max_batch_concurrency;
    // Render proxy previews for restored values
    if ($("ckProxies").value) renderProxyPreview("ckProxies","ckProxyBody","ckProxyPreview","ckCount",$("ckScheme").value);
  }).catch(err => toast("加载配置失败: " + err.message, "error"));

})();
