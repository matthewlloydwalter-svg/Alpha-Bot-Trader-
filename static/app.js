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
      
      if (target === "portfolio") renderPortfolio();
      if (target === "bots") loadBots();
      if (target === "stocks") loadStocks();
      if (target === "history") loadTradeHistory();
      if (target === "news" && ALL_NEWS.length === 0) loadNews();
    };
  });
}

async function refreshUserData() {
  try {
    USER = await api("/auth/me");
    renderPortfolio();
    
    const vText = document.getElementById("verification-text");
    const vBtn = document.getElementById("verify-email-btn");
    if (USER.email_verified) {
      vText.textContent = "Verified. Live Production Clearance Permitted.";
      vText.style.color = "var(--green)";
      vBtn.classList.add("hidden");
    } else {
      vText.textContent = "Unverified. Live Trading Locked.";
      vText.style.color = "var(--amber)";
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
}

async function setBroker(broker) {
  try {
    const data = await api("/broker/switch", { method: "POST", body: JSON.stringify({ broker }) });
    USER.active_broker = data.active_broker;
    renderBrokerUI();
    toast(`Switched to ${broker}`, "success");
  } catch (e) { toast(e, "error"); }
}

async function handleSaveAlpacaKeys(e) {
  e.preventDefault();
  const api_key = document.getElementById("alpaca-key").value.trim();
  const secret_key = document.getElementById("alpaca-secret").value.trim();
  try {
    await api("/broker/alpaca/keys", { method: "POST", body: JSON.stringify({ api_key, secret_key }) });
    toast("Alpaca keys saved.", "success");
  } catch (e) { toast(e, "error"); }
}

async function handleSaveOkxKeys(e) {
  e.preventDefault();
  const api_key = document.getElementById("okx-key").value.trim();
  const secret_key = document.getElementById("okx-secret").value.trim();
  const passphrase = document.getElementById("okx-pass").value.trim();
  try {
    await api("/broker/okx/keys", { method: "POST", body: JSON.stringify({ api_key, secret_key, passphrase }) });
    toast("OKX keys saved.", "success");
  } catch (e) { toast(e, "error"); }
}

/* --- EMAIL VERIFICATION --- */
async function triggerEmailVerification() {
  const btn = document.getElementById("verify-email-btn");
  btn.disabled = true; btn.textContent = "Sending...";
  try {
    await api("/auth/send-verification", { method: "POST" });
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
    await api("/auth/verify-email", { method: "POST", body: JSON.stringify({ code }) });
    toast("Email verification complete!", "success");
    closeVerificationModal();
    await refreshUserData();
  } catch (e) { toast(e, "error"); }
}

/* --- PORTFOLIO --- */
function renderPortfolio() {
  const dep = USER ? (USER.total_deposited || 0) : 0;
  const wit = USER ? (USER.total_withdrawn || 0) : 0;
  const net = dep - wit;
  document.getElementById("pf-deposited").textContent = "$" + dep.toFixed(2);
  document.getElementById("pf-withdrawn").textContent = "$" + wit.toFixed(2);
  const netEl = document.getElementById("pf-net");
  netEl.textContent = (net >= 0 ? "+" : "") + "$" + Math.abs(net).toFixed(2);
  netEl.className = "stat-value " + (net >= 0 ? "pup" : "pdn");
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
}
function showCreateBotModal() { document.getElementById("modal-bot").classList.remove("hidden"); }
function hideCreateBotModal() { document.getElementById("modal-bot").classList.add("hidden"); }

async function createBot() {
  const name = document.getElementById("b-name").value.trim();
  const funds_allocated = parseFloat(document.getElementById("b-funds").value);
  const ticker = document.getElementById("b-ticker").value.trim().toUpperCase() || null;
  const buy_limit = document.getElementById("b-buy").value ? +document.getElementById("b-buy").value : null;
  const sell_limit = document.getElementById("b-sell").value ? +document.getElementById("b-sell").value : null;

  try {
    await api("/bots", {
      method: "POST",
      body: JSON.stringify({ name, ticker, funds_allocated, is_auto: BOT_MODE === "auto", buy_limit, sell_limit })
    });
    hideCreateBotModal();
    toast("Bot launched", "success");
    await loadBots();
  } catch (e) { toast(e, "error"); }
}

async function loadBots() {
  try { BOTS = await api("/bots"); renderBots(); } catch (e) { toast(e, "error"); }
}

function renderBots() {
  const el = document.getElementById("bots-list-container");
  if (!BOTS.length) { el.innerHTML = `<div style="text-align:center;color:var(--t2);padding:20px">No bots yet.</div>`; return; }
  el.innerHTML = BOTS.map((b, i) => `
    <div class="bot-card ${b.running ? "running" : "paused"}">
      <div style="display:flex;justify-content:space-between;margin-bottom:10px">
        <div style="font-weight:700">${esc(b.name)} <span class="badge ${b.running?"badge-green":"badge-amber"}">${b.running?"Running":"Paused"}</span></div>
        <div style="display:flex;gap:6px">
          <button class="btn btn-sm" onclick="toggleBot(${b.id})">${b.running?"⏸":"▶"}</button>
          <button class="btn btn-sm btn-danger" onclick="deleteBot(${b.id})">🗑</button>
        </div>
      </div>
      <div style="color:var(--t2);font-size:12px;margin-bottom:10px">Ticker: ${b.ticker||"Auto"} | Funds: $${b.funds_allocated} | Trades: ${b.trade_count}</div>
      <div style="background:var(--bg2);padding:10px;border-radius:6px;border:1px solid var(--border)">
        <div style="display:flex;gap:8px">
          <input type="number" id="price-${b.id}" placeholder="Current Price" style="flex:1">
          <button class="btn btn-sm btn-primary" onclick="runBotCycle(${b.id})">Run Cycle</button>
        </div>
        <div class="log-box" id="blog-${b.id}" style="display:none;margin-top:8px"></div>
      </div>
    </div>
  `).join("");
}

async function toggleBot(id) {
  try { await api(`/bots/${id}/toggle`, { method: "POST" }); await loadBots(); } catch (e) { toast(e, "error"); }
}
async function deleteBot(id) {
  if (!confirm("Delete bot?")) return;
  try { await api(`/bots/${id}`, { method: "DELETE" }); await loadBots(); } catch (e) { toast(e, "error"); }
}

async function runBotCycle(id) {
  const price = parseFloat(document.getElementById(`price-${id}`).value);
  if (!price) return toast("Enter price", "error");
  
  if (!PRICE_HISTORY[id]) PRICE_HISTORY[id] = [];
  PRICE_HISTORY[id].push(price);
  
  const logEl = document.getElementById(`blog-${id}`);
  logEl.style.display = "block"; logEl.innerHTML = `Running cycle with ${PRICE_HISTORY[id].length} data points...`;
  
  try {
    const result = await api(`/bots/${id}/run-cycle`, {
      method: "POST", body: JSON.stringify({ current_price: price, recent_prices: PRICE_HISTORY[id], news_summary: "" })
    });
    logEl.innerHTML = `<span style="color:var(--blue)">Action: ${result.action}</span><br>${result.reasoning || ""}`;
    await loadBots();
  } catch (e) { logEl.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`; }
}

/* --- STOCKS, HISTORY, NEWS --- */
async function loadStocks() {
  const tbody = document.getElementById("stocks-table-body");
  const items = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA"];
  tbody.innerHTML = items.map(sym => `
    <tr><td style="font-weight:600;color:var(--blue)">${sym}</td><td>Equity</td><td><span class="badge badge-green">Active Tracking</span></td><td>Alpaca Supported</td></tr>
  `).join("");
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
  const list = document.getElementById("news-list");
  status.innerHTML = 'Fetching latest headlines...';
  status.style.display = "block"; list.innerHTML = "";
  try {
    const res = await fetch("https://api.rss2json.com/v1/api.json?rss_url=https://feeds.reuters.com/reuters/businessNews");
    const data = await res.json();
    ALL_NEWS = data.items.map(item => ({
      title: item.title, link: item.link, pubDate: item.pubDate, sentiment: "neutral" 
    }));
    status.style.display = "none";
    renderNews();
  } catch (e) { status.innerHTML = "Failed to load news feeds."; }
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
