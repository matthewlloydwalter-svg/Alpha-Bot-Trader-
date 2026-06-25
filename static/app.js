let USER = null;   
let BOTS = [];     
let BOT_MODE = "auto";
let PRICE_HISTORY = {};
let ALL_NEWS = [];
let NEWS_FILTER = "all";

async function api(path, options = {}) {
  const resp = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let data = null;
  try { data = await resp.json(); } catch (_) {}
  if (!resp.ok) {
    throw new Error((data && data.detail) ? data.detail : `Error triggered (${resp.status})`);
  }
  return data;
}

function toast(msg, type = "") {
  if (typeof msg === 'object' && msg !== null) {
    msg = msg.detail || msg.message || JSON.stringify(msg);
  }
  const wrap = document.getElementById("toast-container");
  if (!wrap) return;
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function esc(str) {
  return String(str).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function toggleAuthMode(showLogin) {
  document.getElementById("login-card").classList.toggle("hidden", !showLogin);
  document.getElementById("register-card").classList.toggle("hidden", showLogin);
}

async function handleLogin(e) {
  e.preventDefault();
  const email = document.getElementById("login-email").value.trim();
  const password = document.getElementById("login-password").value;
  try {
    USER = await api("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
    toast("Welcome back!", "success");
    await enterApp();
  } catch (err) { toast(err, "error"); }
}

async function handleRegister(e) {
  e.preventDefault();
  const email = document.getElementById("reg-email").value.trim();
  const password = document.getElementById("reg-password").value;
  const confirm = document.getElementById("reg-confirm").value;
  const agreed = document.getElementById("reg-tos").checked;

  if (password !== confirm) return toast("Passwords do not match", "error");
  if (!agreed) return toast("You must agree to the Terms of Service", "error");

  try {
    USER = await api("/auth/signup", {
      method: "POST",
      body: JSON.stringify({ email, password, confirm_password: confirm, agreed_to_tos: true })
    });
    toast("Account setup successful!", "success");
    await enterApp();
  } catch (err) { toast(err, "error"); }
}

function openTosModal(e) {
  if (e) e.preventDefault();
  window.open('/terms', '_blank');
}

async function handleLogoutClick() {
  try { await api("/auth/logout", { method: "POST" }); location.reload(); } 
  catch (e) { location.reload(); }
}

async function enterApp() {
  document.getElementById("auth-screen").classList.add("hidden");
  document.getElementById("main-app").classList.remove("hidden");
  document.getElementById("user-email-display").textContent = USER.email;
  
  if (USER.is_admin) document.getElementById("admin-nav-btn").classList.remove("hidden");
  
  setupTabs();
  renderModeUI();
  renderBrokerUI();
  await refreshUserData();
  loadBrokerKeys();
  loadPortfolioPerformance();  // Portfolio is the default visible tab
}

function setupTabs() {
  document.querySelectorAll(".nav-tab").forEach(tab => {
    if (tab.id === "admin-nav-btn") return;
    tab.onclick = async () => {
      document.querySelectorAll(".nav-tab").forEach(t => t.classList.remove("active"));
      tab.classList.add("active");
      const target = tab.getAttribute("data-tab");
      document.querySelectorAll(".tab-view").forEach(v => v.classList.add("hidden"));
      document.getElementById(`view-${target}`).classList.remove("hidden");
      
      if (target === "portfolio") loadPortfolioPerformance();
      if (target === "assets") renderAssets();
      if (target === "bots") loadBots();
      if (target === "stocks") loadStocks();
      if (target === "history") loadTradeHistory();
      if (target === "account") { loadBrokerKeys(); renderOnboarding(); }
      if (target === "news" && ALL_NEWS.length === 0) loadNews();
    };
  });
}

async function refreshUserData() {
  try {
    USER = await api("/auth/me");
    renderAssets();

    const vText = document.getElementById("verification-text");
    const vBtn = document.getElementById("verify-email-btn");
    if (USER.email_verified) {
      vText.textContent = "Email verified.";
      vText.style.color = "var(--green)";
      vBtn.classList.add("hidden");
    } else {
      // NOTE: email verification is currently OPTIONAL — live trading is unlocked.
      vText.textContent = "Email not verified (optional for now — live trading is unlocked).";
      vText.style.color = "var(--t2)";
      vBtn.classList.remove("hidden");
    }
  } catch (e) { toast("Session sync failed.", "error"); }
}

/* --- BROKER & MODE CONFIG --- */
function renderModeUI() {
  const m = USER.trading_mode || "paper";
  document.getElementById("mode-badge").textContent = m === "live" ? "⚡ Live" : "● Paper";
  document.getElementById("mode-badge").className = `badge ${m === "live" ? "badge-red" : "badge-green"}`;
  document.getElementById("mode-paper").classList.toggle("active", m === "paper");
  document.getElementById("mode-live").classList.toggle("active", m === "live");
}

async function setTradingMode(mode) {
  try {
    const data = await api("/broker/trading-mode", { method: "POST", body: JSON.stringify({ mode }) });
    USER.trading_mode = data.trading_mode;
    renderModeUI();
    toast(`Switched to ${mode} trading`, "success");
  } catch (e) { toast(e, "error"); }
}

function renderBrokerUI() {
  const b = USER.active_broker || "alpaca";
  document.getElementById("broker-badge").textContent = b === "alpaca" ? "Alpaca" : "OKX";
  document.getElementById("broker-alpaca").classList.toggle("active", b === "alpaca");
  document.getElementById("broker-okx").classList.toggle("active", b === "okx");
  document.getElementById("alpaca-keys-card").classList.toggle("hidden", b !== "alpaca");
  document.getElementById("okx-keys-card").classList.toggle("hidden", b !== "okx");

  // Rename Markets ↔ Crypto tab and view header based on active broker
  const marketsTab = document.getElementById("markets-nav-tab");
  const marketsLabel = document.getElementById("markets-view-label");
  const marketsDesc  = document.getElementById("markets-view-desc");
  if (marketsTab) marketsTab.innerHTML = b === "okx" ? "₿ Crypto" : "📊 Markets";
  if (marketsLabel) marketsLabel.textContent = b === "okx" ? "Tracked Crypto Assets" : "Tracked Market Assets";
  if (marketsDesc)  marketsDesc.textContent  = b === "okx"
    ? "Live OKX pairs available for automated crypto trading."
    : "Real-time status of equity instruments queried from market endpoints.";

  renderOnboarding();
}

function renderOnboarding() {
  const b = (USER && USER.active_broker) || "alpaca";
  const alp = document.getElementById("onboarding-alpaca");
  const okx = document.getElementById("onboarding-okx");
  if (alp) alp.classList.toggle("hidden", b !== "alpaca");
  if (okx) okx.classList.toggle("hidden", b !== "okx");
}

async function setBroker(broker) {
  try {
    const data = await api("/broker/switch", { method: "POST", body: JSON.stringify({ broker }) });
    USER.active_broker = data.active_broker;
    renderBrokerUI();
    toast(`Switched to ${broker}`, "success");
  } catch (e) { toast(e, "error"); }
}

async function handleSaveAlpacaKeys(mode) {
  const api_key = document.getElementById(`alpaca-${mode}-key`).value.trim();
  const secret_key = document.getElementById(`alpaca-${mode}-secret`).value.trim();
  if (!api_key || !secret_key) return toast(`Enter both ${mode} Alpaca keys`, "error");
  try {
    await api("/broker/alpaca/keys", { method: "POST", body: JSON.stringify({ api_key, secret_key, mode }) });
    toast(`Alpaca ${mode} keys saved.`, "success");
  } catch (e) { toast(e, "error"); }
}

async function handleSaveOkxKeys(mode) {
  const api_key = document.getElementById(`okx-${mode}-key`).value.trim();
  const secret_key = document.getElementById(`okx-${mode}-secret`).value.trim();
  const passphrase = document.getElementById(`okx-${mode}-pass`).value.trim();
  if (!api_key || !secret_key || !passphrase) return toast(`Enter all ${mode} OKX fields`, "error");
  try {
    await api("/broker/okx/keys", { method: "POST", body: JSON.stringify({ api_key, secret_key, passphrase, mode }) });
    toast(`OKX ${mode} keys saved.`, "success");
  } catch (e) { toast(e, "error"); }
}

async function loadBrokerKeys() {
  // Auto-populate the key boxes with whatever is stored for this user.
  try {
    const k = await api("/broker/keys");
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v || ""; };
    set("alpaca-paper-key", k.alpaca.paper.api_key);
    set("alpaca-paper-secret", k.alpaca.paper.secret_key);
    set("alpaca-live-key", k.alpaca.live.api_key);
    set("alpaca-live-secret", k.alpaca.live.secret_key);
    set("okx-paper-key", k.okx.paper.api_key);
    set("okx-paper-secret", k.okx.paper.secret_key);
    set("okx-paper-pass", k.okx.paper.passphrase);
    set("okx-live-key", k.okx.live.api_key);
    set("okx-live-secret", k.okx.live.secret_key);
    set("okx-live-pass", k.okx.live.passphrase);
  } catch (e) { /* non-fatal — boxes just stay empty */ }
}

/* --- EMAIL VERIFICATION --- */
async function triggerEmailVerification() {
  const btn = document.getElementById("verify-email-btn");
  btn.disabled = true; btn.textContent = "Sending...";
  try {
    await api("/auth/trigger-verification", { method: "POST" });
    toast("Verification code sent to inbox!", "success");
    document.getElementById("verify-modal").classList.remove("hidden");
  } catch (e) { toast(e, "error"); } 
  finally { btn.disabled = false; btn.textContent = "📧 Send Verification Code"; }
}

function closeVerificationModal() { document.getElementById("verify-modal").classList.add("hidden"); }

async function submitVerificationCode() {
  const code = document.getElementById("verification-input-code").value.trim();
  if (code.length !== 6) return toast("Code must be 6 characters", "error");
  try {
    await api("/auth/confirm-verification", { method: "POST", body: JSON.stringify({ code }) });
    toast("Email verification complete!", "success");
    closeVerificationModal();
    await refreshUserData();
  } catch (e) { toast(e, "error"); }
}

/* --- ASSETS TAB (manual cash tracking) --- */
function renderAssets() {
  const dep = USER ? (USER.total_deposited || 0) : 0;
  const wit = USER ? (USER.total_withdrawn || 0) : 0;
  const net = dep - wit;
  const setTxt = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  setTxt("as-deposited", "$" + dep.toFixed(2));
  setTxt("as-withdrawn", "$" + wit.toFixed(2));
  const netEl = document.getElementById("as-net");
  if (netEl) {
    netEl.textContent = (net >= 0 ? "+" : "") + "$" + Math.abs(net).toFixed(2);
    netEl.className = "stat-value " + (net >= 0 ? "pup" : "pdn");
  }
}

/* --- PORTFOLIO TAB (bot performance graph) --- */
let PERF_CHART = null, PERF_SERIES = null, PERF_MARKERS_BY_TIME = {};

async function loadPortfolioPerformance() {
  const msg = document.getElementById("perf-chart-msg");
  hidePerfTooltip();
  if (msg) { msg.classList.remove("hidden"); msg.textContent = "Loading performance…"; }
  let data;
  try {
    data = await api("/api/portfolio/performance");
  } catch (e) {
    if (msg) { msg.classList.remove("hidden"); msg.textContent = `⚠ ${e.message}`; }
    return;
  }

  // Metrics
  const fa = document.getElementById("pf-funds-allocated");
  if (fa) fa.textContent = "$" + Number(data.funds_allocated || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const np = document.getElementById("pf-net-position");
  if (np) {
    const v = Number(data.net_position || 0);
    np.textContent = (v >= 0 ? "+" : "-") + "$" + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    np.className = "stat-value " + (v >= 0 ? "pup" : "pdn");
  }

  renderPerfChart(data);
}

function renderPerfChart(data) {
  const msg = document.getElementById("perf-chart-msg");
  const wrap = document.getElementById("perf-chart");
  if (typeof LightweightCharts === "undefined") {
    if (msg) { msg.classList.remove("hidden"); msg.textContent = "Chart library unavailable (offline)."; }
    return;
  }
  const series = data.series || [];
  if (!series.length) {
    if (msg) { msg.classList.remove("hidden"); msg.textContent = "No bot trades yet — the performance line will appear once your bots start trading."; }
    if (PERF_CHART) { try { PERF_CHART.remove(); } catch (_) {} PERF_CHART = null; }
    return;
  }
  if (msg) msg.classList.add("hidden");

  if (PERF_CHART) { try { PERF_CHART.remove(); } catch (_) {} PERF_CHART = null; }
  PERF_CHART = LightweightCharts.createChart(wrap, {
    layout: { background: { color: "#0d0f14" }, textColor: "#8b91a8" },
    grid: { vertLines: { color: "#1a1e28" }, horzLines: { color: "#1a1e28" } },
    rightPriceScale: { borderColor: "#2a2f3d" },
    timeScale: { borderColor: "#2a2f3d", timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0 },
    localization: {
      // Force UTC rendering on the time axis + crosshair label.
      timeFormatter: (t) => new Date(t * 1000).toUTCString().replace(" GMT", " UTC"),
    },
    autoSize: true,
  });

  PERF_SERIES = PERF_CHART.addAreaSeries({
    lineColor: "#4d9fff", topColor: "rgba(77,159,255,0.25)", bottomColor: "rgba(77,159,255,0.02)",
    lineWidth: 2, priceLineVisible: false,
  });
  PERF_SERIES.setData(series.map(p => ({ time: p.time, value: p.value })));

  // Build markers and a time->markers lookup for click tooltips.
  PERF_MARKERS_BY_TIME = {};
  const colorFor = { buy: "#ffb347", sell_profit: "#00d68f", sell_loss: "#ff4d6a" };
  const lwcMarkers = (data.markers || []).map(m => {
    (PERF_MARKERS_BY_TIME[m.time] = PERF_MARKERS_BY_TIME[m.time] || []).push(m);
    return {
      time: m.time,
      position: m.type === "buy" ? "belowBar" : "aboveBar",
      color: colorFor[m.type] || "#8b91a8",
      shape: "circle",
      size: 1.4,
    };
  });
  PERF_SERIES.setMarkers(lwcMarkers);

  PERF_CHART.timeScale().fitContent();

  PERF_CHART.subscribeClick((param) => {
    if (!param || !param.time) { hidePerfTooltip(); return; }
    const list = PERF_MARKERS_BY_TIME[param.time];
    if (!list || !list.length) { hidePerfTooltip(); return; }
    showPerfTooltip(list, param.point);
  });
}

function fmtUTC(iso) {
  if (!iso) return "—";
  // Render explicitly in UTC.
  const d = new Date(iso);
  return d.toUTCString().replace(" GMT", " UTC");
}

function showPerfTooltip(markers, point) {
  const tip = document.getElementById("perf-tooltip");
  if (!tip) return;
  const labelFor = { buy: "🟠 Bought", sell_profit: "🟢 Sold (profit)", sell_loss: "🔴 Sold (loss)" };
  tip.innerHTML = markers.map(m => {
    const rows = [
      `<div class="pt-row"><span>Bot</span><span>${esc(m.bot_name || "—")}</span></div>`,
      `<div class="pt-row"><span>Asset</span><span>${esc(m.ticker || "—")}</span></div>`,
      `<div class="pt-row"><span>Amount</span><span>$${Number(m.amount || 0).toLocaleString()}</span></div>`,
      `<div class="pt-row"><span>Price</span><span>$${Number(m.price || 0).toLocaleString()}</span></div>`,
    ];
    if (m.pnl !== null && m.pnl !== undefined) {
      const c = m.pnl >= 0 ? "var(--green)" : "var(--red)";
      rows.push(`<div class="pt-row"><span>P/L</span><span style="color:${c}">${m.pnl >= 0 ? "+" : ""}$${Number(m.pnl).toLocaleString()}</span></div>`);
    }
    rows.push(`<div class="pt-row"><span>Time</span><span>${esc(fmtUTC(m.datetime_utc))}</span></div>`);
    return `<div style="margin-bottom:${markers.length > 1 ? "10px" : "0"}">
        <div class="pt-title">${labelFor[m.type] || ""}</div>${rows.join("")}
      </div>`;
  }).join("");
  tip.innerHTML = `<span class="pt-close" onclick="hidePerfTooltip()">✕</span>` + tip.innerHTML;

  // Position the tooltip near the click, kept inside the chart wrapper.
  const wrap = document.getElementById("perf-chart-wrap");
  tip.classList.remove("hidden");
  const maxX = wrap.clientWidth - tip.offsetWidth - 8;
  const maxY = wrap.clientHeight - tip.offsetHeight - 8;
  let x = (point && point.x ? point.x + 12 : 12);
  let y = (point && point.y ? point.y + 12 : 12);
  tip.style.left = Math.max(8, Math.min(x, maxX)) + "px";
  tip.style.top = Math.max(8, Math.min(y, maxY)) + "px";
}

function hidePerfTooltip() {
  const tip = document.getElementById("perf-tooltip");
  if (tip) tip.classList.add("hidden");
}

async function doDeposit() {
  const amount = parseFloat(document.getElementById("dep-amt").value);
  if (!amount) return toast("Enter valid amount", "error");
  try {
    await api("/cash/deposit", { method: "POST", body: JSON.stringify({ amount }) });
    await refreshUserData();
    document.getElementById("dep-amt").value = "";
    toast("Deposit recorded", "success");
  } catch (e) { toast(e, "error"); }
}

async function doWithdraw() {
  const amount = parseFloat(document.getElementById("with-amt").value);
  if (!amount) return toast("Enter valid amount", "error");
  try {
    await api("/cash/withdraw", { method: "POST", body: JSON.stringify({ amount }) });
    await refreshUserData();
    document.getElementById("with-amt").value = "";
    toast("Withdrawal recorded", "success");
  } catch (e) { toast(e, "error"); }
}

async function loadBrokerAccount() {
  const el = document.getElementById("broker-account-info");
  el.innerHTML = '<span style="color:var(--t3)">Fetching from broker...</span>';
  try {
    const data = await api("/broker/account");
    el.innerHTML = Object.entries(data).map(([k, v]) =>
      `<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border);font-size:13px">
        <span style="color:var(--t2)">${k.replace(/_/g," ")}</span>
        <span style="font-weight:500">${typeof v === "number" ? "$" + Number(v).toLocaleString() : v}</span>
      </div>`
    ).join("");
  } catch (e) { el.innerHTML = `<span style="color:var(--red)">⚠ ${e.message}</span>`; }
}

/* --- BOTS --- */
function setBotMode(m) {
  BOT_MODE = m;
  document.getElementById("botmode-auto").classList.toggle("active", m === "auto");
  document.getElementById("botmode-manual").classList.toggle("active", m === "manual");
  document.getElementById("manual-limits").classList.toggle("hidden", m === "auto");
  // Fully autonomous mode needs only a funds amount — hide the ticker field.
  document.getElementById("ticker-field").classList.toggle("hidden", m === "auto");
  document.getElementById("auto-explainer").classList.toggle("hidden", m !== "auto");
}
function showCreateBotModal() { document.getElementById("modal-bot").classList.remove("hidden"); }
function hideCreateBotModal() { document.getElementById("modal-bot").classList.add("hidden"); }

async function createBot() {
  const name = document.getElementById("b-name").value.trim();
  const funds_allocated = parseFloat(document.getElementById("b-funds").value);
  // Fully autonomous mode sends NO ticker — the engine picks the asset.
  const ticker = BOT_MODE === "manual"
    ? (document.getElementById("b-ticker").value.trim().toUpperCase() || null)
    : null;
  const buy_limit = document.getElementById("b-buy").value ? +document.getElementById("b-buy").value : null;
  const sell_limit = document.getElementById("b-sell").value ? +document.getElementById("b-sell").value : null;

  if (!funds_allocated || funds_allocated <= 0) return toast("Enter a funds amount to allocate", "error");
  if (BOT_MODE === "manual" && !ticker) return toast("Manual bots need a ticker symbol", "error");

  try {
    await api("/bots", {
      method: "POST",
      body: JSON.stringify({
        name, ticker, funds_allocated,
        broker: USER ? (USER.active_broker || "alpaca") : "alpaca",
        timeframe: "1h",
        is_auto: BOT_MODE === "auto", buy_limit, sell_limit
      })
    });
    hideCreateBotModal();
    toast(BOT_MODE === "auto" ? "Autonomous bot launched — it will scan all markets" : "Bot launched", "success");
    await loadBots();
  } catch (e) { toast(e, "error"); }
}

async function loadBots() {
  try {
    const res = await api("/bots");
    BOTS = Array.isArray(res) ? res : (res.bots || []);
    renderBots();
  } catch (e) { toast(e, "error"); }
}

function renderBots() {
  const el = document.getElementById("bots-list-container");
  if (!BOTS.length) { el.innerHTML = `<div style="text-align:center;color:var(--t2);padding:20px">No bots yet.</div>`; return; }
  el.innerHTML = BOTS.map((b) => {
    const sigColor = b.last_signal === "BUY" ? "badge-green" : b.last_signal === "SELL" ? "badge-red" : "badge-amber";
    const pos = b.in_position
      ? `<span class="badge badge-blue">In position @ ${formatPrice(b.avg_entry_price)}</span>`
      : `<span class="badge badge-amber">Flat — scanning</span>`;
    const pnlColor = (b.realized_pnl || 0) >= 0 ? "var(--green)" : "var(--red)";
    const assetLabel = b.ticker ? esc(b.ticker) : (b.auto_select ? "🧠 Auto-select (all markets)" : "Auto");
    const chartBtn = b.ticker
      ? `<button class="btn btn-sm" onclick="openMarketDashboard('${esc(b.broker||'alpaca')}','${esc(b.ticker)}')">📈 Chart</button>`
      : "";
    return `
    <div class="bot-card ${b.running ? "running" : "paused"}">
      <div style="display:flex;justify-content:space-between;margin-bottom:10px">
        <div style="font-weight:700">${esc(b.name)}
          <span class="badge ${b.running?"badge-green":"badge-amber"}">${b.running?"Running":"Paused"}</span>
          ${b.auto_select ? `<span class="badge badge-purple">Autonomous</span>` : ""}
          ${b.last_signal ? `<span class="badge ${sigColor}">${esc(b.last_signal)}</span>` : ""}
        </div>
        <div style="display:flex;gap:6px">
          ${chartBtn}
          <button class="btn btn-sm" onclick="toggleBot(${b.id})">${b.running?"⏸":"▶"}</button>
          <button class="btn btn-sm btn-danger" onclick="deleteBot(${b.id})">🗑</button>
        </div>
      </div>
      <div style="color:var(--t2);font-size:12px;margin-bottom:8px">
        ${assetLabel} · ${esc((b.broker||"alpaca").toUpperCase())} · ${esc(b.timeframe||"1h")} |
        Funds: $${b.funds_allocated} | Trades: ${b.trade_count} |
        P&L: <span style="color:${pnlColor}">${(b.realized_pnl||0)>=0?"+":""}$${(b.realized_pnl||0).toFixed(2)}</span>
      </div>
      <div style="margin-bottom:8px">${pos}
        ${b.in_position && b.stop_price ? `<span class="ind-pill">Stop ${formatPrice(b.stop_price)}</span>` : ""}
        ${b.in_position && b.take_profit_price ? `<span class="ind-pill">Target ${formatPrice(b.take_profit_price)}</span>` : ""}
      </div>
      <div style="background:var(--bg2);padding:10px;border-radius:6px;border:1px solid var(--border)">
        <div style="font-size:11px;color:var(--t2);margin-bottom:8px">${esc(b.last_pattern_summary || "Awaiting first scan — run a scan to analyze the chart.")}</div>
        <button class="btn btn-sm btn-primary" onclick="runBotCycle(${b.id})">⚡ Run Pattern Scan</button>
        <div class="log-box" id="blog-${b.id}" style="display:none;margin-top:8px"></div>
      </div>
    </div>`;
  }).join("");
}

async function toggleBot(id) {
  try { await api(`/bots/${id}/toggle`, { method: "POST" }); await loadBots(); } catch (e) { toast(e, "error"); }
}
async function deleteBot(id) {
  if (!confirm("Delete bot?")) return;
  try { await api(`/bots/${id}`, { method: "DELETE" }); await loadBots(); } catch (e) { toast(e, "error"); }
}

async function runBotCycle(id) {
  const logEl = document.getElementById(`blog-${id}`);
  logEl.style.display = "block";
  logEl.innerHTML = `Fetching live chart and analyzing structure…`;
  try {
    const res = await api(`/bots/${id}/run-cycle`, { method: "POST" });
    const d = res.details || {};
    const sig = (d.analysis && d.analysis.signal) || {};
    logEl.innerHTML =
      `<span style="color:var(--blue)">Action: ${esc(d.action || "—")}</span><br>` +
      `${esc(d.reason || sig.headline || "")}` +
      (sig.action ? `<br><span style="color:var(--t3)">Signal ${esc(sig.action)} · strength ${sig.strength ?? "—"} · ${esc(sig.confidence || "")}</span>` : "");
    await loadBots();
  } catch (e) { logEl.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`; }
}

/* --- STOCKS / MARKETS --- */
let MARKETS = [];          // current exchange universe from backend
let MARKETS_EXCHANGE = "alpaca";

async function loadStocks() {
  const tbody = document.getElementById("stocks-table-body");
  const exchange = USER ? (USER.active_broker || "alpaca") : "alpaca";
  MARKETS_EXCHANGE = exchange;
  tbody.innerHTML = `<tr><td colspan="5" style="color:var(--t2)">Loading active markets…</td></tr>`;
  try {
    const data = await api(`/api/markets/${exchange}`);
    MARKETS = data.items || [];
    renderMarkets();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" style="color:var(--red)">Failed to load markets: ${esc(e.message)}</td></tr>`;
  }
}

function renderMarkets() {
  const tbody = document.getElementById("stocks-table-body");
  const filter = (document.getElementById("markets-search")?.value || "").trim().toUpperCase();
  const cls = MARKETS_EXCHANGE === "okx" ? "Crypto" : "Equity";
  const items = filter
    ? MARKETS.filter(m => m.symbol.toUpperCase().includes(filter) || (m.name || "").toUpperCase().includes(filter))
    : MARKETS;

  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="5" style="color:var(--t2)">No matching assets.</td></tr>`;
    return;
  }
  tbody.innerHTML = items.map(m => `
    <tr class="market-row" onclick="openMarketDashboard('${MARKETS_EXCHANGE}','${esc(m.symbol)}')">
      <td>${esc(m.display || m.symbol)}</td>
      <td style="color:var(--t1)">${esc(m.name || "")}</td>
      <td>${cls}</td>
      <td><span class="badge badge-green">Active Tracking</span></td>
      <td class="market-open-hint">Open dashboard →</td>
    </tr>
  `).join("");
}

function filterMarkets() { renderMarkets(); }

/* --- MARKET DASHBOARD --- */
let DASH_STATE = { exchange: null, symbol: null, timeframe: "1h" };
let DASH_CHART = null, DASH_CANDLE_SERIES = null, DASH_SMA20 = null, DASH_SMA50 = null;

async function openMarketDashboard(exchange, symbol) {
  DASH_STATE = { exchange, symbol, timeframe: "1h" };
  document.getElementById("market-dash-modal").classList.remove("hidden");
  document.getElementById("dash-symbol").textContent = symbol;
  document.getElementById("dash-name").textContent = "Loading asset…";
  document.getElementById("dash-price").textContent = "—";
  document.querySelectorAll("#dash-tf-toggle .mode-btn").forEach(b =>
    b.classList.toggle("active", b.getAttribute("data-tf") === "1h"));
  await reloadDashboard();
}

function closeMarketDashboard() {
  document.getElementById("market-dash-modal").classList.add("hidden");
  if (DASH_CHART) { try { DASH_CHART.remove(); } catch (_) {} DASH_CHART = null; }
  DASH_CANDLE_SERIES = DASH_SMA20 = DASH_SMA50 = null;
}

function changeDashTimeframe(tf) {
  DASH_STATE.timeframe = tf;
  document.querySelectorAll("#dash-tf-toggle .mode-btn").forEach(b =>
    b.classList.toggle("active", b.getAttribute("data-tf") === tf));
  reloadDashboard();
}

async function reloadDashboard() {
  const { exchange, symbol, timeframe } = DASH_STATE;
  if (!exchange || !symbol) return;
  const msg = document.getElementById("dash-chart-msg");
  msg.classList.remove("hidden");
  msg.textContent = "Loading market data…";
  try {
    const d = await api(`/api/markets/${exchange}/${symbol}/dashboard?timeframe=${encodeURIComponent(timeframe)}&limit=200`);
    if (DASH_STATE.symbol !== symbol || DASH_STATE.timeframe !== timeframe) return; // stale
    renderDashboard(d);
  } catch (e) {
    msg.classList.remove("hidden");
    msg.textContent = `⚠ ${e.message}`;
    document.getElementById("dash-bot-status").innerHTML =
      `<span style="color:var(--red)">${esc(e.message)}</span>`;
  }
}

function renderDashboard(d) {
  document.getElementById("dash-symbol").textContent = d.display_symbol || d.symbol;
  document.getElementById("dash-name").textContent = d.asset_name || "";
  document.getElementById("dash-class-badge").textContent = d.asset_class || d.exchange;
  document.getElementById("dash-price").textContent = formatPrice(d.last_price, d.quote);

  // Signal badge
  const sig = d.signal || {};
  const sBadge = document.getElementById("dash-signal-badge");
  sBadge.textContent = `${sig.action || "—"} · ${sig.confidence || ""}`;
  sBadge.className = "badge " + (sig.bias === "bullish" ? "badge-green" : sig.bias === "bearish" ? "badge-red" : "badge-amber");

  renderDashChart(d);
  renderDashPatterns(d.patterns || []);
  renderDashIndicators(d.indicators || {});
  renderDashBotStatus(d.bot_status || {}, sig);
}

function formatPrice(p, quote) {
  if (p == null) return "—";
  const digits = p >= 100 ? 2 : p >= 1 ? 4 : 6;
  return (quote === "USDT" ? "" : "$") + Number(p).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: digits }) + (quote === "USDT" ? " USDT" : "");
}

function renderDashChart(d) {
  const msg = document.getElementById("dash-chart-msg");
  const wrap = document.getElementById("dash-chart");
  if (typeof LightweightCharts === "undefined") {
    msg.classList.remove("hidden");
    msg.textContent = "Chart library unavailable (offline). Indicators below are still live.";
    return;
  }
  const candles = (d.candles || []).filter(c => c && c.time);
  if (!candles.length) {
    msg.classList.remove("hidden");
    msg.textContent = "No candle data available for this asset/timeframe.";
    return;
  }
  msg.classList.add("hidden");

  if (DASH_CHART) { try { DASH_CHART.remove(); } catch (_) {} DASH_CHART = null; }
  DASH_CHART = LightweightCharts.createChart(wrap, {
    layout: { background: { color: "#0d0f14" }, textColor: "#8b91a8" },
    grid: { vertLines: { color: "#1a1e28" }, horzLines: { color: "#1a1e28" } },
    rightPriceScale: { borderColor: "#2a2f3d" },
    timeScale: { borderColor: "#2a2f3d", timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0 },
    autoSize: true,
  });
  DASH_CANDLE_SERIES = DASH_CHART.addCandlestickSeries({
    upColor: "#00d68f", downColor: "#ff4d6a", borderVisible: false,
    wickUpColor: "#00d68f", wickDownColor: "#ff4d6a",
  });
  DASH_CANDLE_SERIES.setData(candles);

  // Overlay moving averages aligned to candle timestamps.
  const series = d.series || {};
  const overlay = (arr, color) => {
    if (!Array.isArray(arr)) return;
    const pts = [];
    for (let i = 0; i < candles.length; i++) {
      if (arr[i] != null) pts.push({ time: candles[i].time, value: arr[i] });
    }
    if (pts.length) {
      const line = DASH_CHART.addLineSeries({ color, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false });
      line.setData(pts);
    }
  };
  overlay(series.sma20, "#4d9fff");
  overlay(series.sma50, "#9b59ff");

  // Mark support / resistance levels as price lines.
  const lv = d.levels || {};
  if (lv.nearest_support != null)
    DASH_CANDLE_SERIES.createPriceLine({ price: lv.nearest_support, color: "#00d68f", lineStyle: 2, lineWidth: 1, title: "Support" });
  if (lv.nearest_resistance != null)
    DASH_CANDLE_SERIES.createPriceLine({ price: lv.nearest_resistance, color: "#ff4d6a", lineStyle: 2, lineWidth: 1, title: "Resistance" });

  DASH_CHART.timeScale().fitContent();
}

function renderDashPatterns(patterns) {
  const el = document.getElementById("dash-patterns");
  if (!patterns.length) { el.innerHTML = `<span style="color:var(--t3);font-size:12px">No structural pattern triggered on the current chart.</span>`; return; }
  el.innerHTML = patterns.map(p => `
    <div class="pattern-chip ${esc(p.bias)}">
      <div>
        <div class="pname">${esc(p.name)}</div>
        <div class="pdetail">${esc(p.detail || "")}</div>
      </div>
    </div>`).join("");
}

function renderDashIndicators(ind) {
  const el = document.getElementById("dash-indicators");
  const fmt = v => (v == null ? "—" : (typeof v === "number" ? Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 }) : v));
  const rsi = ind.rsi;
  const rsiColor = rsi == null ? "var(--t1)" : rsi <= 30 ? "var(--green)" : rsi >= 70 ? "var(--red)" : "var(--t1)";
  const cells = [
    ["RSI (14)", fmt(rsi), rsiColor],
    ["Trend", ind.trend || "—", ind.trend === "up" ? "var(--green)" : ind.trend === "down" ? "var(--red)" : "var(--t1)"],
    ["SMA 20", fmt(ind.sma20)], ["SMA 50", fmt(ind.sma50)],
    ["MACD", fmt(ind.macd), (ind.macd_hist >= 0 ? "var(--green)" : "var(--red)")],
    ["MACD Signal", fmt(ind.macd_signal)],
    ["Boll Upper", fmt(ind.bb_upper)], ["Boll Lower", fmt(ind.bb_lower)],
    ["ATR (14)", fmt(ind.atr)],
  ];
  el.innerHTML = cells.map(([k, v, c]) =>
    `<div class="dash-ind"><div class="k">${k}</div><div class="v" style="color:${c || "var(--t1)"}">${v}</div></div>`).join("");
}

function renderDashBotStatus(status, sig) {
  const el = document.getElementById("dash-bot-status");
  let html = "";
  if (status.has_bot && status.bots && status.bots.length) {
    html = status.bots.map(b => `
      <div style="margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid var(--border)">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px">
          <span style="font-weight:700">${esc(b.name)}</span>
          <span class="badge ${b.running ? "badge-green" : "badge-amber"}">${b.running ? "Running" : "Paused"}</span>
        </div>
        <div class="bot-status-line"><span class="lbl">Analysis</span><span>${esc(b.summary || "—")}</span></div>
        <div class="bot-status-line"><span class="lbl">Position</span><span>${b.in_position ? `In @ ${formatPrice(b.avg_entry_price)}` : "Flat — scanning for dip"}</span></div>
        ${b.in_position ? `<div class="bot-status-line"><span class="lbl">Stop / Target</span><span>${formatPrice(b.stop_price)} / ${formatPrice(b.take_profit_price)}</span></div>` : ""}
        <div class="bot-status-line"><span class="lbl">Trades</span><span>${b.trade_count || 0}</span></div>
      </div>`).join("");
  } else {
    // No bot yet — present the live engine read on this asset based on the signal.
    const action = sig.action === "BUY" ? "Scanning for confirmed dip → ready to BUY"
      : sig.action === "SELL" ? "Detecting peak/breakdown → ready to protect capital"
      : "Scanning for dip — no confirmed edge yet";
    html = `
      <div class="bot-status-line"><span class="lbl">Pattern Analysis</span><span>${esc((sig.bias || "neutral"))} bias</span></div>
      <div class="bot-status-line"><span class="lbl">Action</span><span>${esc(action)}</span></div>
      <div class="bot-status-line"><span class="lbl">Conviction</span><span>${esc(sig.confidence || "low")} (${(sig.strength != null ? sig.strength : 0)})</span></div>
      <div style="margin-top:10px"><button class="btn btn-primary btn-sm" onclick="prefillBotFromDashboard()">🤖 Deploy a bot on ${esc(DASH_STATE.symbol)}</button></div>`;
  }
  el.innerHTML = html;
}

function prefillBotFromDashboard() {
  closeMarketDashboard();
  document.querySelector('.nav-tab[data-tab="bots"]')?.click();
  showCreateBotModal();
  const t = document.getElementById("b-ticker");
  if (t) t.value = DASH_STATE.symbol;
  const n = document.getElementById("b-name");
  if (n && !n.value) n.value = `${DASH_STATE.symbol} Dip Hunter`;
}

async function loadTradeHistory() {
  const tbody = document.getElementById("history-table-body");
  try {
    const trades = await api("/broker/trades-ledger");
    if (!trades.length) { tbody.innerHTML = `<tr><td colspan="6" style="color:var(--t2)">No trades logged yet.</td></tr>`; return; }
    tbody.innerHTML = trades.map(t => `
      <tr>
        <td style="color:var(--t2)">${new Date(t.created_at).toLocaleString()}</td>
        <td style="font-weight:600">${t.ticker}</td>
        <td><span class="badge ${t.side === 'buy' ? 'badge-green' : 'badge-red'}">${t.side.toUpperCase()}</span></td>
        <td>${t.qty || 0}</td>
        <td style="font-family:monospace">$${parseFloat(t.price || 0).toFixed(2)}</td>
        <td><span class="badge badge-blue">${t.mode.toUpperCase()}</span></td>
      </tr>
    `).join("");
  } catch (e) { tbody.innerHTML = `<tr><td colspan="6" style="color:var(--red)">Failed to fetch execution records.</td></tr>`; }
}

async function loadNews() {
  const status = document.getElementById("news-status");
  const list   = document.getElementById("news-list");
  const btn    = document.getElementById("news-refresh-btn");
  status.innerHTML = '<div style="font-size:24px;margin-bottom:8px">⏳</div><div>Fetching latest headlines…</div>';
  status.style.display = "block";
  list.innerHTML = "";
  if (btn) { btn.disabled = true; btn.textContent = "Loading…"; }

  try {
    ALL_NEWS = await api("/api/news");
    if (!ALL_NEWS || !ALL_NEWS.length) {
      status.innerHTML = '<div style="font-size:24px;margin-bottom:8px">📭</div><div>No articles available right now — try again in a moment.</div>';
      return;
    }
    status.style.display = "none";
    renderNews();
  } catch (e) {
    status.innerHTML = '<div style="font-size:24px;margin-bottom:8px">⚠️</div><div>Failed to load news. Check your connection and try again.</div>';
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "🔄 Refresh"; }
  }
}

function filterNews(f) { NEWS_FILTER = f; renderNews(); }

function renderNews() {
  const list = document.getElementById("news-list");
  const filtered = NEWS_FILTER === "all" ? ALL_NEWS : ALL_NEWS.filter(n => n.sentiment === NEWS_FILTER);
  list.innerHTML = filtered.map(n => `
    <div class="card" style="cursor:pointer" onclick="window.open('${esc(n.link)}')">
      <div style="font-weight:600;margin-bottom:6px">${esc(n.title)}</div>
      <div style="font-size:11px;color:var(--t3)">${n.pubDate}</div>
    </div>
  `).join("");
}

(async function boot() {
  try { USER = await api("/auth/me"); await enterApp(); } catch (_) { toggleAuthMode(true); }
})();

function togglePassword(inputId, robotId, evt) {
    // Stop the click reaching any parent <label> that might refocus/interfere
    if (evt) { evt.stopPropagation(); evt.preventDefault(); }

    const passwordInput = document.getElementById(inputId);
    const robotIcon = document.getElementById(robotId);

    if (!passwordInput || !robotIcon) {
        console.error("togglePassword: could not find elements:", inputId, robotId);
        return;
    }

    if (passwordInput.type === 'password') {
        passwordInput.type = 'text';
        robotIcon.src = '/static/show-password.png';
    } else {
        passwordInput.type = 'password';
        robotIcon.src = '/static/hide-password.png';
    }
}
