/**
 * v9-app.js — production frontend logic for AlphaBot.
 *
 * Every action calls the FastAPI backend at /auth/*, /bots/*, etc.
 * No demo data. No fake timers. No direct calls to api.anthropic.com.
 * The backend holds all secrets and does all AI + broker communication.
 */

// ─────────────────────────────────────────────────────────────────
// STATE — single source of truth, populated from the server
// ─────────────────────────────────────────────────────────────────
let USER = null;   // result of GET /auth/me
let BOTS = [];     // result of GET /bots
let BOT_MODE = "auto";
let PRICE_HISTORY = {};  // { [bot_id]: [price, price, ...] }  — grows with each cycle

// ─────────────────────────────────────────────────────────────────
// FETCH HELPER
// credentials:"include" sends the session cookie the backend set.
// Throws a readable Error on any non-2xx so callers just catch.
// ─────────────────────────────────────────────────────────────────
async function api(path, options = {}) {
  const resp = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let data = null;
  try { data = await resp.json(); } catch (_) {}
  if (!resp.ok) {
    throw new Error((data && data.detail) ? data.detail : `Request failed (${resp.status})`);
  }
  return data;
}

// ─────────────────────────────────────────────────────────────────
// TOAST NOTIFICATIONS
// ─────────────────────────────────────────────────────────────────
function toast(msg, type = "") {
  const wrap = document.getElementById("toast-container");
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.style.pointerEvents = "all";
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 3500);
  setTimeout(() => el.remove(), 3900);
}

// ─────────────────────────────────────────────────────────────────
// TOS MODAL
// ─────────────────────────────────────────────────────────────────
function openTosModal(ev) {
  if (ev) ev.preventDefault();
  document.getElementById("modal-tos").classList.remove("hidden");
}
function closeTosModal() {
  document.getElementById("modal-tos").classList.add("hidden");
}

// ─────────────────────────────────────────────────────────────────
// AUTH TAB SWITCH
// ─────────────────────────────────────────────────────────────────
function showAuthTab(t) {
  document.getElementById("login-form").classList.toggle("hidden", t !== "login");
  document.getElementById("signup-form").classList.toggle("hidden", t !== "signup");
  const onBg = "var(--bg3)", offBg = "transparent", onCol = "var(--t1)", offCol = "var(--t2)";
  const li = document.getElementById("tab-login");
  const si = document.getElementById("tab-signup");
  li.style.background = t === "login" ? onBg : offBg;
  li.style.color = t === "login" ? onCol : offCol;
  si.style.background = t === "signup" ? onBg : offBg;
  si.style.color = t === "signup" ? onCol : offCol;
}

// ─────────────────────────────────────────────────────────────────
// LOGIN
// ─────────────────────────────────────────────────────────────────
async function doLogin() {
  const email = document.getElementById("login-email").value.trim();
  const password = document.getElementById("login-pass").value;
  if (!email || !password) return toast("Enter your email and password", "error");
  try {
    await api("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
    await enterApp();
  } catch (e) {
    toast(e.message, "error");
  }
}

// ─────────────────────────────────────────────────────────────────
// SIGN UP
// ─────────────────────────────────────────────────────────────────
async function doSignup() {
  const email = document.getElementById("su-email").value.trim();
  const password = document.getElementById("su-pass").value;
  const agreed = document.getElementById("su-tos").checked;
  if (!email || !password) return toast("Fill in all fields", "error");
  if (!agreed) return toast("You must agree to the Terms of Service to sign up", "error");
  try {
    await api("/auth/signup", {
      method: "POST",
      body: JSON.stringify({ email, password, agreed_to_tos: true }),
    });
    // Signup succeeded — backend sent a verification email.
    document.getElementById("auth-screen").classList.add("hidden");
    document.getElementById("verify-screen").classList.remove("hidden");
    document.getElementById("verify-email-target").textContent = email;
    toast("Account created — check your email for a verification code", "success");
  } catch (e) {
    toast(e.message, "error");
  }
}

// ─────────────────────────────────────────────────────────────────
// EMAIL VERIFICATION
// ─────────────────────────────────────────────────────────────────
async function submitVerifyCode() {
  const code = document.getElementById("verify-code-input").value.trim();
  if (!code || code.length < 6) return toast("Enter the 6-digit code from your email", "error");
  try {
    await api("/auth/verify-email", { method: "POST", body: JSON.stringify({ code }) });
    toast("Email verified! Welcome to AlphaBot.", "success");
    document.getElementById("verify-screen").classList.add("hidden");
    await enterApp();
  } catch (e) {
    toast(e.message, "error");
  }
}

async function resendCode() {
  try {
    const data = await api("/auth/send-verification", { method: "POST" });
    toast(data.message || "Code resent — check your email", "success");
  } catch (e) {
    toast(e.message, "error");
  }
}

function skipVerifyForNow() {
  document.getElementById("verify-screen").classList.add("hidden");
  enterApp();
}

// Verify from Account tab: send code, then show an inline prompt
async function startVerifyFromAccount() {
  try {
    const data = await api("/auth/send-verification", { method: "POST" });
    toast(data.message || "Verification code sent", "success");
    // Small inline modal-style prompt (no extra DOM needed)
    const code = prompt("Enter the 6-digit code from your email:");
    if (!code) return;
    await api("/auth/verify-email", { method: "POST", body: JSON.stringify({ code: code.trim() }) });
    USER.email_verified = true;
    renderAccountVerifyUI();
    renderTopbar();
    toast("Email verified! Live trading is now unlocked.", "success");
  } catch (e) {
    toast(e.message, "error");
  }
}

// ─────────────────────────────────────────────────────────────────
// LOGOUT
// ─────────────────────────────────────────────────────────────────
async function doLogout() {
  try { await api("/auth/logout", { method: "POST" }); } catch (_) {}
  USER = null; BOTS = []; PRICE_HISTORY = {};
  document.getElementById("main-app").classList.add("hidden");
  document.getElementById("auth-screen").classList.remove("hidden");
  showAuthTab("login");
}

// ─────────────────────────────────────────────────────────────────
// ENTER APP — called after any successful auth action
// ─────────────────────────────────────────────────────────────────
async function enterApp() {
  try {
    USER = await api("/auth/me");
  } catch (e) {
    toast("Could not load session: " + e.message, "error");
    return;
  }
  document.getElementById("auth-screen").classList.add("hidden");
  document.getElementById("verify-screen").classList.add("hidden");
  document.getElementById("main-app").classList.remove("hidden");

  renderTopbar();
  renderAccountVerifyUI();
  renderModeUI();
  renderBrokerUI();
  renderPortfolio();

  document.getElementById("acc-email").textContent = USER.email;
  document.getElementById("verified-email-display").textContent = USER.email;
  document.getElementById("unverified-email-display").textContent = USER.email;

  // Show admin tab only for admins
  document.getElementById("admin-nav-tab").classList.toggle("hidden", !USER.is_admin);
  document.getElementById("admin-topbar-badge").classList.toggle("hidden", !USER.is_admin);

  await loadBots();
  goTab("portfolio");
}

// ─────────────────────────────────────────────────────────────────
// NAV
// ─────────────────────────────────────────────────────────────────
function goTab(t) {
  ["portfolio", "bots", "account", "admin"].forEach(n => {
    document.getElementById("page-" + n).classList.toggle("hidden", n !== t);
  });
  document.querySelectorAll(".nav-tab").forEach((el, i) => {
    el.classList.toggle("active", ["portfolio", "bots", "account", "admin"][i] === t);
  });
  if (t === "bots") { loadBots(); }
  if (t === "admin") { loadAdminData(); }
}

// ─────────────────────────────────────────────────────────────────
// TOPBAR — reflects live USER state
// ─────────────────────────────────────────────────────────────────
function renderTopbar() {
  document.getElementById("user-disp").textContent = USER.email;
  document.getElementById("unverified-badge").classList.toggle("hidden", USER.email_verified);
  renderModeUI();
  renderBrokerUI();
}

// ─────────────────────────────────────────────────────────────────
// TRADING MODE
// ─────────────────────────────────────────────────────────────────
function renderModeUI() {
  const m = USER.trading_mode || "paper";
  const badge = document.getElementById("mode-badge");
  badge.textContent = m === "live" ? "⚡ Live" : "● Paper";
  badge.style.background = m === "live" ? "rgba(255,77,106,.15)" : "var(--gdim)";
  badge.style.color = m === "live" ? "var(--red)" : "var(--green)";

  const paperBtn = document.getElementById("mode-paper");
  const liveBtn = document.getElementById("mode-live");
  if (paperBtn && liveBtn) {
    paperBtn.classList.toggle("active", m === "paper");
    liveBtn.classList.toggle("active", m === "live");
  }

  const lockedNote = document.getElementById("live-locked-note");
  const activeNote = document.getElementById("live-active-note");
  if (lockedNote && activeNote) {
    lockedNote.classList.toggle("hidden", m === "live");
    activeNote.classList.toggle("hidden", m !== "live");
  }
}

async function setTradingMode(mode) {
  try {
    const data = await api("/broker/trading-mode", { method: "POST", body: JSON.stringify({ mode }) });
    USER.trading_mode = data.trading_mode;
    renderModeUI();
    toast(
      mode === "live"
        ? "⚡ Live trading enabled — real orders will be placed"
        : "📄 Switched to paper trading",
      mode === "live" ? "error" : "success"
    );
  } catch (e) {
    // Backend returns descriptive errors: "Verify your email", "Save API keys first", etc.
    toast(e.message, "error");
  }
}

// ─────────────────────────────────────────────────────────────────
// BROKER SWITCHING
// ─────────────────────────────────────────────────────────────────
function renderBrokerUI() {
  const b = USER.active_broker || "alpaca";
  const badge = document.getElementById("broker-badge");
  badge.textContent = b === "alpaca" ? "Alpaca" : "OKX Crypto";
  badge.style.color = b === "alpaca" ? "var(--blue)" : "var(--amber)";
  badge.style.background = b === "alpaca" ? "var(--bdim)" : "rgba(255,179,71,.15)";

  const aBtn = document.getElementById("broker-alpaca");
  const oBtn = document.getElementById("broker-okx");
  if (aBtn && oBtn) {
    aBtn.classList.toggle("active", b === "alpaca");
    oBtn.classList.toggle("active", b === "okx");
  }
  const aCard = document.getElementById("alpaca-keys-card");
  const oCard = document.getElementById("okx-keys-card");
  if (aCard && oCard) {
    aCard.classList.toggle("hidden", b !== "alpaca");
    oCard.classList.toggle("hidden", b !== "okx");
  }
  const desc = document.getElementById("broker-desc");
  if (desc) {
    desc.textContent = b === "alpaca"
      ? "Alpaca: US stock & ETF market. Paper trading uses Alpaca's built-in paper environment."
      : "OKX: Global crypto exchange. For paper mode, use OKX demo-trading API keys.";
  }
}

async function setBroker(broker) {
  try {
    const data = await api("/broker/switch", { method: "POST", body: JSON.stringify({ broker }) });
    USER.active_broker = data.active_broker;
    renderBrokerUI();
    // Re-check live mode eligibility when broker changes
    if (USER.trading_mode === "live") {
      USER.trading_mode = "paper";
      renderModeUI();
    }
    toast(`Switched to ${broker === "alpaca" ? "Alpaca (Stocks)" : "OKX (Crypto)"}`, "success");
  } catch (e) {
    toast(e.message, "error");
  }
}

async function saveAlpacaKeys() {
  const api_key = document.getElementById("alpaca-key").value.trim();
  const secret_key = document.getElementById("alpaca-secret").value.trim();
  if (!api_key || !secret_key) return toast("Enter both the API key and secret key", "error");
  if (api_key.length < 8 || secret_key.length < 8) return toast("Those keys look too short — check and re-paste", "error");
  try {
    await api("/broker/alpaca/keys", { method: "POST", body: JSON.stringify({ api_key, secret_key }) });
    document.getElementById("alpaca-key").value = "";
    document.getElementById("alpaca-secret").value = "";
    document.getElementById("alpaca-key-status").textContent = "✓ Saved";
    document.getElementById("alpaca-key-status").style.color = "var(--green)";
    toast("Alpaca keys saved to the server", "success");
  } catch (e) {
    toast(e.message, "error");
  }
}

async function saveOkxKeys() {
  const api_key = document.getElementById("okx-key").value.trim();
  const secret_key = document.getElementById("okx-secret").value.trim();
  const passphrase = document.getElementById("okx-pass").value.trim();
  if (!api_key || !secret_key || !passphrase) return toast("Enter API key, secret key, and passphrase", "error");
  try {
    await api("/broker/okx/keys", { method: "POST", body: JSON.stringify({ api_key, secret_key, passphrase }) });
    document.getElementById("okx-key").value = "";
    document.getElementById("okx-secret").value = "";
    document.getElementById("okx-pass").value = "";
    document.getElementById("okx-key-status").textContent = "✓ Saved";
    document.getElementById("okx-key-status").style.color = "var(--green)";
    toast("OKX keys saved to the server", "success");
  } catch (e) {
    toast(e.message, "error");
  }
}

// ─────────────────────────────────────────────────────────────────
// ACCOUNT TAB — verify UI
// ─────────────────────────────────────────────────────────────────
function renderAccountVerifyUI() {
  const verified = USER && USER.email_verified;
  document.getElementById("email-verified-view").classList.toggle("hidden", !verified);
  document.getElementById("email-unverified-view").classList.toggle("hidden", !!verified);
}

// ─────────────────────────────────────────────────────────────────
// PORTFOLIO TAB
// ─────────────────────────────────────────────────────────────────
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
  if (!amount || amount <= 0) return toast("Enter a valid deposit amount", "error");
  try {
    const data = await api("/cash/deposit", { method: "POST", body: JSON.stringify({ amount }) });
    USER.total_deposited = data.total_deposited;
    renderPortfolio();
    document.getElementById("dep-amt").value = "";
    toast(`Deposit of $${amount.toFixed(2)} recorded`, "success");
  } catch (e) {
    toast(e.message, "error");
  }
}

async function doWithdraw() {
  const amount = parseFloat(document.getElementById("with-amt").value);
  if (!amount || amount <= 0) return toast("Enter a valid withdrawal amount", "error");
  try {
    const data = await api("/cash/withdraw", { method: "POST", body: JSON.stringify({ amount }) });
    USER.total_withdrawn = data.total_withdrawn;
    renderPortfolio();
    document.getElementById("with-amt").value = "";
    toast(`Withdrawal of $${amount.toFixed(2)} recorded`, "success");
  } catch (e) {
    toast(e.message, "error");
  }
}

async function loadBrokerAccount() {
  const el = document.getElementById("broker-account-info");
  el.innerHTML = '<span style="color:var(--t3)">Fetching from broker...</span>';
  try {
    const data = await api("/broker/account");
    el.innerHTML = Object.entries(data).map(([k, v]) =>
      `<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border);font-size:12px">
        <span style="color:var(--t2)">${k.replace(/_/g," ")}</span>
        <span style="font-weight:500">${typeof v === "number" ? "$" + Number(v).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2}) : v}</span>
      </div>`
    ).join("") || '<span style="color:var(--t2)">No data returned.</span>';
  } catch (e) {
    el.innerHTML = `<span style="color:var(--red)">⚠ ${e.message}</span>`;
    if (e.message.includes("not set") || e.message.includes("key")) {
      el.innerHTML += '<div style="font-size:11px;color:var(--t3);margin-top:6px">Save your API keys in the Account tab first.</div>';
    }
  }
}

// ─────────────────────────────────────────────────────────────────
// BOTS TAB
// ─────────────────────────────────────────────────────────────────
function setBotMode(m) {
  BOT_MODE = m;
  document.getElementById("botmode-auto").classList.toggle("active", m === "auto");
  document.getElementById("botmode-manual").classList.toggle("active", m === "manual");
  document.getElementById("manual-limits").classList.toggle("hidden", m === "auto");
  document.getElementById("botmode-desc").textContent = m === "auto"
    ? "AI picks the ticker, buys dips detected by RSI, sells at profit targets."
    : "You set the ticker and optional price limits. AI still uses RSI/SMA signals within those limits.";
}

function showCreateBot() {
  document.getElementById("modal-bot").classList.remove("hidden");
}
function hideCreateBot() {
  document.getElementById("modal-bot").classList.add("hidden");
  // Clear form
  ["b-name","b-ticker","b-funds","b-firstbuy","b-buy","b-sell"].forEach(id => {
    document.getElementById(id).value = "";
  });
  document.getElementById("b-pct").value = "";
  setBotMode("auto");
}

async function createBot() {
  const name = document.getElementById("b-name").value.trim();
  const funds_allocated = parseFloat(document.getElementById("b-funds").value);
  if (!name) return toast("Enter a name for this bot", "error");
  if (!funds_allocated || funds_allocated < 10) return toast("Enter a valid funds amount ($10 minimum)", "error");

  const ticker = document.getElementById("b-ticker").value.trim().toUpperCase() || null;
  const first_buy_price = document.getElementById("b-firstbuy").value ? +document.getElementById("b-firstbuy").value : null;
  const buy_limit = document.getElementById("b-buy").value ? +document.getElementById("b-buy").value : null;
  const sell_limit = document.getElementById("b-sell").value ? +document.getElementById("b-sell").value : null;
  const min_profit_pct = document.getElementById("b-pct").value ? +document.getElementById("b-pct").value : null;

  try {
    await api("/bots", {
      method: "POST",
      body: JSON.stringify({
        name, ticker, funds_allocated,
        is_auto: BOT_MODE === "auto",
        buy_limit, sell_limit, min_profit_pct, first_buy_price,
      }),
    });
    hideCreateBot();
    toast(`Bot "${name}" launched`, "success");
    await loadBots();
  } catch (e) {
    // Common: "Email verification required for this action"
    toast(e.message, "error");
  }
}

async function loadBots() {
  try {
    BOTS = await api("/bots");
    renderBots();
  } catch (e) {
    toast("Could not load bots: " + e.message, "error");
  }
}

function renderBots() {
  const el = document.getElementById("bots-list");
  if (!BOTS.length) {
    el.innerHTML = `
      <div style="text-align:center;padding:32px;color:var(--t2)">
        <div style="font-size:32px;margin-bottom:8px">🤖</div>
        <div style="font-size:14px;font-weight:500;margin-bottom:4px">No bots yet</div>
        <div style="font-size:12px">Create your first bot to start automated trading</div>
      </div>`;
    return;
  }

  el.innerHTML = BOTS.map((b, i) => {
    const inPos = b.in_position;
    const pnl = b.realized_pnl || 0;
    const pnlSign = pnl >= 0 ? "+" : "";
    const statusColor = b.running ? "var(--green)" : "var(--amber)";
    const borderColor = b.running ? "rgba(0,214,143,.3)" : "rgba(255,179,71,.2)";

    return `
    <div class="bot-card ${b.running ? "running" : "paused"}" style="border-color:${borderColor}">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px">
        <div style="display:flex;align-items:center;gap:8px">
          <div style="width:22px;height:22px;border-radius:50%;background:var(--bg3);display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:var(--blue);flex-shrink:0">${i+1}</div>
          <div>
            <span style="font-weight:600;font-size:14px">${esc(b.name)}</span>
            <span style="font-size:10px;padding:2px 7px;border-radius:20px;margin-left:6px;background:${b.running?"var(--gdim)":"rgba(255,179,71,.13)"};color:${statusColor};font-weight:600">${b.running ? "● Running" : "⏸ Paused"}</span>
            <span style="font-size:10px;padding:2px 7px;border-radius:20px;margin-left:4px;background:var(--bdim);color:var(--blue);font-weight:600">${b.is_auto ? "🧠 Auto" : "⚙ Manual"}</span>
          </div>
        </div>
        <div style="display:flex;gap:6px;align-items:center;flex-shrink:0">
          <span class="${pnl>=0?"pup":"pdn"}" style="font-size:13px;font-weight:600">${pnlSign}$${Math.abs(pnl).toFixed(2)}</span>
          <button class="btn btn-sm" onclick="toggleBot(${b.id})" title="${b.running?"Pause":"Resume"}">${b.running ? "⏸" : "▶"}</button>
          <button class="btn btn-sm btn-danger" onclick="deleteBot(${b.id})" title="Delete bot">🗑</button>
        </div>
      </div>

      <div style="display:flex;gap:16px;font-size:11px;color:var(--t2);margin-bottom:10px;flex-wrap:wrap">
        <span>Ticker: <strong style="color:var(--t1)">${b.ticker || "AI picks"}</strong></span>
        <span>Broker: <strong style="color:var(--t1)">${b.broker}</strong></span>
        <span>Funds: <strong style="color:var(--t1)">$${b.funds_allocated}</strong></span>
        <span>Trades: <strong style="color:var(--t1)">${b.trade_count}</strong></span>
        ${inPos ? `<span>Holding: <strong style="color:var(--amber)">${b.shares_held.toFixed(4)} @ $${(b.avg_entry_price||0).toFixed(2)}</strong></span>` : ""}
      </div>

      <!-- RUN CYCLE CONTROLS -->
      <div style="background:var(--bg2);border-radius:8px;padding:10px 12px;border:1px solid var(--border)">
        <div style="font-size:10px;font-weight:600;color:var(--t2);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px">Run decision cycle</div>
        <div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap">
          <div style="flex:1;min-width:140px">
            <div style="font-size:10px;color:var(--t3);margin-bottom:3px">Current price ($)</div>
            <input type="number" id="price-${b.id}" placeholder="e.g. 183.40" step="0.01" min="0.0001">
          </div>
          <button class="btn btn-sm btn-primary" onclick="runBotCycle(${b.id})" style="white-space:nowrap">
            🧠 Analyze & trade
          </button>
        </div>
        <div style="font-size:10px;color:var(--t3);margin-top:6px">
          Sends price to server → RSI + SMA computed → Claude decides BUY/SELL/HOLD → ${b.is_auto ? "real order placed if signal is clear" : "order placed if signal clears your limits"}
        </div>
        <div class="log-box" id="blog-${b.id}" style="margin-top:8px;display:none"></div>
      </div>
    </div>`;
  }).join("");
}

// Safely escape HTML to prevent XSS in bot names, emails, etc.
function esc(str) {
  return String(str).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

async function toggleBot(id) {
  try {
    const data = await api(`/bots/${id}/toggle`, { method: "POST" });
    const bot = BOTS.find(b => b.id === id);
    if (bot) bot.running = data.running;
    renderBots();
    toast(data.running ? "Bot resumed" : "Bot paused", "success");
  } catch (e) {
    toast(e.message, "error");
  }
}

async function deleteBot(id) {
  const bot = BOTS.find(b => b.id === id);
  if (!confirm(`Delete bot "${bot ? bot.name : id}"? This cannot be undone.`)) return;
  try {
    await api(`/bots/${id}`, { method: "DELETE" });
    delete PRICE_HISTORY[id];
    toast("Bot deleted", "success");
    await loadBots();
  } catch (e) {
    toast(e.message, "error");
  }
}

/**
 * runBotCycle — the critical path.
 *
 * Sends the price the user typed to the backend, which:
 *   1. Computes RSI-14 and SMA-20 from the accumulated price history
 *   2. Asks Claude: BUY / SELL / HOLD given those indicators
 *   3. Places a real order through Alpaca or OKX if the decision clears limits
 *   4. Returns the result (action, reasoning, indicator values, order id if any)
 *
 * We maintain PRICE_HISTORY[id] locally across cycles so each cycle
 * feeds the server more data, improving RSI/SMA accuracy over time.
 */
async function runBotCycle(id) {
  const priceInput = document.getElementById(`price-${id}`);
  const current_price = parseFloat(priceInput.value);
  if (!current_price || current_price <= 0) {
    return toast("Enter the current market price before running a cycle", "error");
  }

  // Accumulate price history for this bot so RSI/SMA get better over time
  if (!PRICE_HISTORY[id]) PRICE_HISTORY[id] = [];
  PRICE_HISTORY[id].push(current_price);
  if (PRICE_HISTORY[id].length > 100) PRICE_HISTORY[id].shift(); // keep last 100

  const logEl = document.getElementById(`blog-${id}`);
  logEl.style.display = "block";
  logEl.innerHTML = `<span style="color:var(--blue)">⏳ Sending to server... (${PRICE_HISTORY[id].length} price points so far)</span>`;

  try {
    const result = await api(`/bots/${id}/run-cycle`, {
      method: "POST",
      body: JSON.stringify({
        current_price,
        recent_prices: PRICE_HISTORY[id],
        news_summary: "",  // extend later: fetch from /news and pass here
      }),
    });

    const ind = result.indicators || {};
    const action = result.action || "HOLD";
    const actionColor = action === "BUY" ? "var(--green)" : action === "SELL" ? "var(--red)" : "var(--blue)";

    let html = `<div style="margin-bottom:4px">
      <strong style="color:${actionColor};font-size:12px">${action}</strong>
      <span style="color:var(--t2)"> — ${esc(result.reasoning || result.reason || "")}</span>
    </div>`;

    // Show computed indicator values so you can see what triggered the decision
    if (ind.rsi !== null && ind.rsi !== undefined) {
      const rsiColor = ind.rsi < 35 ? "var(--green)" : ind.rsi > 70 ? "var(--red)" : "var(--t2)";
      html += `<div style="margin-top:4px;display:flex;gap:8px;flex-wrap:wrap">
        <span class="ind-pill">RSI(14): <strong style="color:${rsiColor}">${ind.rsi}</strong></span>
        ${ind.sma20 ? `<span class="ind-pill">SMA(20): <strong>$${ind.sma20}</strong></span>` : ""}
        ${ind.pct_from_sma !== null && ind.pct_from_sma !== undefined ? `<span class="ind-pill">vs SMA: <strong style="color:${ind.pct_from_sma<-3?"var(--green)":ind.pct_from_sma>5?"var(--red)":"var(--t2)"}">${ind.pct_from_sma}%</strong></span>` : ""}
        <span class="ind-pill">Signal: <strong>${ind.signal || "—"}</strong></span>
        <span class="ind-pill">${ind.price_count || 0} prices</span>
      </div>`;
    } else {
      html += `<div style="color:var(--t3);margin-top:4px;font-size:10px">Need more price history for RSI/SMA — keep running cycles to build up data.</div>`;
    }

    if (result.order) {
      html += `<div style="margin-top:6px;padding:6px 8px;background:rgba(0,214,143,.08);border-radius:5px;border:1px solid rgba(0,214,143,.2)">
        ✅ Order placed — ID: ${esc(result.order.order_id)} · Status: ${esc(result.order.status)}
      </div>`;
    }
    if (result.error) {
      html += `<div style="margin-top:6px;color:var(--red)">⚠ Broker error: ${esc(result.error)}</div>`;
    }

    logEl.innerHTML = html;
    await loadBots(); // refresh P&L and position state
  } catch (e) {
    logEl.innerHTML = `<span style="color:var(--red)">⚠ ${esc(e.message)}</span>`;
    // Common: "Email verification required for this action" — remind user
    if (e.message.includes("verif")) {
      logEl.innerHTML += `<div style="color:var(--amber);margin-top:4px">Go to Account → Email verification to unlock trading.</div>`;
    }
  }
}

// ─────────────────────────────────────────────────────────────────
// ADMIN TAB
// ─────────────────────────────────────────────────────────────────
async function loadAdminData() {
  try {
    const [stats, users] = await Promise.all([
      api("/admin/stats"),
      api("/admin/users"),
    ]);

    document.getElementById("admin-total-users").textContent = stats.total_users;
    document.getElementById("admin-verified-users").textContent = stats.verified_users;
    document.getElementById("admin-total-deposited").textContent =
      "$" + (stats.total_deposited || 0).toLocaleString("en-US", {minimumFractionDigits:2,maximumFractionDigits:2});
    document.getElementById("admin-total-bots").textContent = stats.total_bots;
    document.getElementById("admin-total-trades").textContent = stats.total_trades;

    document.getElementById("admin-user-list").innerHTML = users.length
      ? users.map(u => `
        <div class="user-row">
          <div style="flex:1;min-width:120px">
            <div style="font-size:12px;font-weight:600">${esc(u.email)}${u.is_admin?" <span style='font-size:9px;background:rgba(155,89,255,.15);color:var(--purple);padding:1px 5px;border-radius:10px;font-weight:600'>ADMIN</span>":""}</div>
            <div style="font-size:10px;color:var(--t2);margin-top:2px">
              Joined ${new Date(u.joined).toLocaleDateString()}
              · ${u.email_verified
                ? '<span style="color:var(--green)">✓ verified</span>'
                : '<span style="color:var(--amber)">⚠ unverified</span>'}
              · ${u.bot_count} bot${u.bot_count===1?"":"s"}
            </div>
          </div>
          <div style="flex:0 0 80px;text-align:right;font-size:11px">
            <div style="font-size:9px;color:var(--t3);font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:2px">Deposited</div>
            $${(u.total_deposited||0).toFixed(2)}
          </div>
          <div style="flex:0 0 80px;text-align:right;font-size:11px">
            <div style="font-size:9px;color:var(--t3);font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:2px">Withdrawn</div>
            $${(u.total_withdrawn||0).toFixed(2)}
          </div>
          <div style="flex:0 0 80px;text-align:right;font-size:11px">
            <div style="font-size:9px;color:var(--t3);font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:2px">Profit</div>
            <span class="${(u.estimated_profit||0)>=0?"pup":"pdn"}">
              ${(u.estimated_profit||0)>=0?"+":""}$${Math.abs(u.estimated_profit||0).toFixed(2)}
            </span>
          </div>
          <div style="flex:0 0 55px;text-align:right;font-size:11px">
            <div style="font-size:9px;color:var(--t3);font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:2px">%</div>
            <span class="${(u.estimated_profit_pct||0)>=0?"pup":"pdn"}">
              ${(u.estimated_profit_pct||0)>=0?"+":""}${(u.estimated_profit_pct||0).toFixed(2)}%
            </span>
          </div>
        </div>`).join("")
      : '<div style="color:var(--t2);font-size:12px;padding:16px 0">No users yet.</div>';

  } catch (e) {
    // 403 = user somehow reached admin tab without admin rights
    document.getElementById("admin-user-list").innerHTML =
      `<div style="color:var(--red);font-size:12px;padding:16px 0">⚠ ${esc(e.message)}</div>`;
    toast(e.message, "error");
  }
}

async function savePlatformEmail() {
  const email = document.getElementById("platform-email-input").value.trim();
  if (!email || !email.includes("@")) return toast("Enter a valid email address", "error");
  try {
    const data = await api("/admin/platform-email", { method: "POST", body: JSON.stringify({ email }) });
    document.getElementById("platform-email-status").textContent = data.note;
    document.getElementById("platform-email-status").style.color = "var(--blue)";
    toast("Platform email recorded", "success");
  } catch (e) {
    toast(e.message, "error");
  }
}

async function emailAllUsers() {
  const subject = prompt("Email subject:");
  if (!subject || !subject.trim()) return;
  const body = prompt("Email body:");
  if (!body || !body.trim()) return;
  try {
    const data = await api("/admin/email-users", {
      method: "POST",
      body: JSON.stringify({ subject: subject.trim(), body: body.trim(), target: "all" }),
    });
    toast(`Sent to ${data.sent} of ${data.attempted} users`, "success");
  } catch (e) {
    toast(e.message, "error");
  }
}

// ─────────────────────────────────────────────────────────────────
// BOOT — try to resume an existing session on every page load.
// If the session cookie is valid, /auth/me succeeds and we skip
// the auth screen entirely. If not, we show the login form.
// ─────────────────────────────────────────────────────────────────
(async function boot() {
  try {
    USER = await api("/auth/me");
    await enterApp();
  } catch (_) {
    // No active session — show auth screen (already visible by default).
    showAuthTab("login");
  }
})();
