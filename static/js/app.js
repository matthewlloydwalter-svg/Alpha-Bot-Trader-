let USER = null;   
let BOTS = [];     
let BOT_MODE = "auto";
let PRICE_HISTORY = {};
let ALL_NEWS = [];
let NEWS_FILTER = "all";
let LIVE_QUOTES = {};        // `${broker}:${symbol}` -> {price, signal_action,...}
let EVT_SOURCE = null;       // EventSource for the live data stream
let _perfReloadTimer = null; // debounce portfolio refreshes from the stream
let MARKET_STATUS = null;    // { open, next_open_epoch, ... } from /api/market-status

// User's local timezone (auto-detected from the device). All times shown on the
// site are rendered in this zone via toLocale* — East Coast sees ET, West Coast
// sees PT, China sees CST, etc.
const USER_TZ = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (_) { return "local"; } })();

// Descriptions used by the Low-balance strategy selector and hover tooltip.
const LOW_BALANCE_DESCRIPTIONS = {
  standard: "Standard execution uses your normal allocation logic.",
  one_shot_daily: "Uses 100% of your allocated funds for a single high-confidence trade today. Halts trading after selling until funds settle tomorrow.",
  micro_trader: "Executes multiple small day trades ($1.00 each) on a single stock to capture small movements without spending unsettled cash.",
  swing_trader: "Buys a stock and holds it for several days or weeks to ride larger trends. Safely avoids daily cash settlement rules.",
  scattershot: "Diversifies your risk by buying $1.00 of 5 different stocks simultaneously at the market open, selling them before the close."
};

// Render a UTC epoch (seconds) as a local day+time with the local tz label,
// e.g. "Fri 9:30 AM EDT" / "Fri 6:30 AM PDT" / "Fri 9:30 PM CST".
function fmtLocalFromEpoch(epochSec) {
  if (!epochSec) return "the next session";
  try {
    return new Date(epochSec * 1000).toLocaleString(undefined,
      { weekday: "short", hour: "numeric", minute: "2-digit", timeZoneName: "short" });
  } catch (_) { return new Date(epochSec * 1000).toLocaleString(); }
}
function fmtLocalOpenTime() {
  return MARKET_STATUS ? fmtLocalFromEpoch(MARKET_STATUS.next_open_epoch) : "the next session";
}
function isMarketClosed() { return !!(MARKET_STATUS && MARKET_STATUS.open === false); }

async function loadMarketStatus() {
  try {
    MARKET_STATUS = await api("/api/market-status");
  } catch (_) { return; }
  // Reflect the new status in any mounted market-aware UI.
  renderMarketOverlay();
  const bv = document.getElementById("view-bots");
  if (bv && !bv.classList.contains("hidden")) renderBots();
}

async function api(path, options = {}) {
  const resp = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let data = null;
  try { data = await resp.json(); } catch (_) {}
  if (!resp.ok) {
    const detail = formatApiDetail(data && data.detail);
    // Expired/missing session while the user still appears logged in → re-auth.
    if (resp.status === 401 && USER && path !== "/auth/login" && path !== "/auth/signup") {
      forceLogout(detail || "Session expired — please sign in again.");
      throw new Error(detail || "Session expired — please sign in again.");
    }
    throw new Error(detail || `Error triggered (${resp.status})`);
  }
  return data;
}

function formatApiDetail(detail) {
  if (detail == null || detail === "") return null;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => {
      if (typeof d === "string") return d;
      if (d && typeof d === "object") return d.msg || d.message || JSON.stringify(d);
      return String(d);
    }).filter(Boolean).join("; ") || null;
  }
  if (typeof detail === "object") {
    if (detail.msg || detail.message) return detail.msg || detail.message;
    try { return JSON.stringify(detail); } catch (_) { return String(detail); }
  }
  return String(detail);
}

let _forcingLogout = false;
function forceLogout(reason) {
  if (_forcingLogout) return;
  _forcingLogout = true;
  // Preserve deep link so re-login returns the user to where they were.
  try {
    const path = normalizePath(location.pathname);
    if (isDashboardPath(path) || path === "/admin") {
      sessionStorage.setItem("post_login_path", path);
    }
  } catch (_) {}
  USER = null;
  try { toast(reason || "Session expired — please sign in again.", ""); } catch (_) {}
  // Leave a beat so the toast can paint, then hard-navigate to login.
  setTimeout(() => { location.href = "/login"; }, 80);
}

function toast(msg, type = "") {
  // Suppress follow-on error toasts while a forced logout redirect is in flight.
  if (_forcingLogout && type === "error") return;
  if (typeof msg === "object" && msg !== null) {
    if (msg instanceof Error) msg = msg.message;
    else msg = formatApiDetail(msg.detail) || msg.message || JSON.stringify(msg);
  }
  const wrap = document.getElementById("toast-container");
  if (!wrap) return;
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.textContent = String(msg == null ? "" : msg);
  wrap.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function esc(str) {
  return String(str).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

/** Safe single-quoted JS string literal for inline onclick handlers. */
function escJs(str) {
  return JSON.stringify(str == null ? "" : String(str));
}

function toggleAuthMode(showLogin) {
  document.getElementById("login-card").classList.toggle("hidden", !showLogin);
  document.getElementById("register-card").classList.toggle("hidden", showLogin);
  closeForgotPassword();
}

/* --- URL router (History API) — dashboard tabs are real paths --- */
// Internal tab keys (data-tab / view-*) stay stable; URL segments are public-facing.
const TAB_PATHS = {
  portfolio: "/dashboard/portfolio",
  stocks: "/dashboard/markets",
  bots: "/dashboard/bots",
  news: "/dashboard/news",
  history: "/dashboard/history",
  assets: "/dashboard/assets",
  account: "/dashboard/account",
};
const PATH_TO_TAB = {
  portfolio: "portfolio",
  markets: "stocks",
  stocks: "stocks",
  crypto: "stocks",
  bots: "bots",
  news: "news",
  history: "history",
  assets: "assets",
  account: "account",
};

function normalizePath(pathname) {
  return String(pathname || "/").replace(/\/+$/, "") || "/";
}

function isAuthPath(pathname) {
  const p = normalizePath(pathname);
  return p === "/login" || p === "/signup";
}

function isDashboardPath(pathname) {
  const p = normalizePath(pathname);
  return p === "/dashboard" || p === "/app" || p.startsWith("/dashboard/");
}

function pathForTab(tab) {
  return TAB_PATHS[tab] || TAB_PATHS.portfolio;
}

function tabFromPath(pathname) {
  const p = normalizePath(pathname);
  if (p === "/dashboard" || p === "/app") return "portfolio";
  if (!p.startsWith("/dashboard/")) return null;
  const segment = p.slice("/dashboard/".length).split("/")[0].toLowerCase();
  return PATH_TO_TAB[segment] || "portfolio";
}

function navigateAuth(e, mode) {
  if (e) {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
    e.preventDefault();
  }
  const showLogin = mode !== "signup";
  toggleAuthMode(showLogin);
  const path = showLogin ? "/login" : "/signup";
  if (normalizePath(location.pathname) !== path) {
    history.pushState({ auth: mode }, "", path);
  }
}

async function activateTab(tab, { push = false, replace = false } = {}) {
  const target = TAB_PATHS[tab] ? tab : "portfolio";
  const path = pathForTab(target);

  document.querySelectorAll(".nav-tab").forEach(t => {
    if (t.id === "admin-nav-btn") return;
    t.classList.toggle("active", t.getAttribute("data-tab") === target);
  });
  const viewId = `view-${target}`;
  document.querySelectorAll(".tab-view").forEach(v => {
    v.classList.toggle("hidden", v.id !== viewId);
  });

  if (push) history.pushState({ tab: target }, "", path);
  else if (replace) history.replaceState({ tab: target }, "", path);

  // Non-portfolio tabs keep primary content inside a collapsed accordion by
  // default — expand once so deep links and first visits aren't an empty shell.
  if (target !== "portfolio" && !Object.prototype.hasOwnProperty.call(TAB_DETAILS_OPEN, target)) {
    TAB_DETAILS_OPEN[target] = true;
  }

  if (target === "portfolio") {
    loadPortfolioPerformance();
    if (typeof syncPortfolioDetailsUI === "function") syncPortfolioDetailsUI();
  } else if (typeof syncTabDetailsUI === "function") {
    syncTabDetailsUI(target);
  }
  if (target === "assets") loadBrokerAccount();
  if (target === "bots") loadBots();
  if (target === "stocks") loadStocks();
  if (target === "history") loadTradeHistory();
  if (target === "account") { loadBrokerKeys(); renderOnboarding(); }
  if (target === "news") loadNews();
}

function navigateToTab(tab, { push = true } = {}) {
  return activateTab(tab, { push, replace: !push });
}

/* --- Forgot password (Resend verification → new password) --- */
let FORGOT_RESET_TOKEN = null;

function openForgotPassword(e) {
  if (e) e.preventDefault();
  FORGOT_RESET_TOKEN = null;
  const emailPrefill = (document.getElementById("login-email")?.value || "").trim();
  const emailInput = document.getElementById("forgot-email");
  if (emailInput) emailInput.value = emailPrefill;
  const codeInput = document.getElementById("forgot-code");
  if (codeInput) codeInput.value = "";
  const pw = document.getElementById("forgot-password");
  const conf = document.getElementById("forgot-confirm");
  if (pw) { pw.value = ""; pw.type = "password"; }
  if (conf) { conf.value = ""; conf.type = "password"; }
  const r1 = document.getElementById("robot-forgot");
  const r2 = document.getElementById("robot-forgot-confirm");
  if (r1) r1.src = "/static/images/hide-password.png";
  if (r2) r2.src = "/static/images/hide-password.png";
  showForgotStep("request");
  document.getElementById("forgot-modal").classList.remove("hidden");
}

function closeForgotPassword() {
  const modal = document.getElementById("forgot-modal");
  if (modal) modal.classList.add("hidden");
  FORGOT_RESET_TOKEN = null;
}

function showForgotStep(step) {
  const map = {
    request: "forgot-step-request",
    verify: "forgot-step-verify",
    reset: "forgot-step-reset",
  };
  Object.entries(map).forEach(([key, id]) => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle("hidden", key !== step);
  });
}

function forgotPasswordBackToRequest() {
  FORGOT_RESET_TOKEN = null;
  showForgotStep("request");
}

async function sendForgotPasswordCode() {
  const email = (document.getElementById("forgot-email")?.value || "").trim();
  if (!email || !email.includes("@")) return toast("Enter a valid email address", "error");
  const btn = document.getElementById("forgot-send-btn");
  if (btn) btn.disabled = true;
  try {
    const res = await api("/auth/password-reset/request", {
      method: "POST",
      body: JSON.stringify({ email }),
    });
    if (res && res.email_not_configured) {
      toast(res.detail || "Email is not configured on this server.", "error");
      return;
    }
    const display = document.getElementById("forgot-email-display");
    if (display) display.textContent = email;
    const codeInput = document.getElementById("forgot-code");
    if (codeInput) codeInput.value = "";
    showForgotStep("verify");
    toast("If an account exists for that email, a verification code has been sent.", "success");
  } catch (e) {
    toast(e, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function verifyForgotPasswordCode() {
  const email = (document.getElementById("forgot-email")?.value || "").trim();
  const code = (document.getElementById("forgot-code")?.value || "").trim();
  if (!code) return toast("Enter the verification code from your email", "error");
  const btn = document.getElementById("forgot-verify-btn");
  if (btn) btn.disabled = true;
  try {
    const res = await api("/auth/password-reset/verify", {
      method: "POST",
      body: JSON.stringify({ email, code }),
    });
    FORGOT_RESET_TOKEN = res.reset_token;
    if (!FORGOT_RESET_TOKEN) throw new Error("Reset session could not be created.");
    showForgotStep("reset");
    toast("Email verified — choose a new password.", "success");
  } catch (e) {
    toast(e, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function confirmForgotPassword(e) {
  e.preventDefault();
  const password = document.getElementById("forgot-password")?.value || "";
  const confirm = document.getElementById("forgot-confirm")?.value || "";
  if (password.length < 8) return toast("Password must be at least 8 characters", "error");
  if (password !== confirm) return toast("Passwords do not match", "error");
  if (!FORGOT_RESET_TOKEN) return toast("Reset session expired. Request a new verification code.", "error");

  const btn = document.getElementById("forgot-reset-btn");
  if (btn) btn.disabled = true;
  try {
    await api("/auth/password-reset/confirm", {
      method: "POST",
      body: JSON.stringify({
        reset_token: FORGOT_RESET_TOKEN,
        password,
        confirm_password: confirm,
      }),
    });
    const email = (document.getElementById("forgot-email")?.value || "").trim();
    closeForgotPassword();
    toggleAuthMode(true);
    const loginEmail = document.getElementById("login-email");
    if (loginEmail && email) loginEmail.value = email;
    toast("Password updated. Sign in with your new password.", "success");
  } catch (err) {
    toast(err, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function handleLogin(e) {
  e.preventDefault();
  const email = document.getElementById("login-email").value.trim();
  const password = document.getElementById("login-password").value;
  try {
    USER = await api("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
    toast("Welcome back!", "success");
    if (await redirectAfterAuthIfNeeded()) return;
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
  if (password.length < 8) return toast("Password must be at least 8 characters", "error");
  if (!agreed) return toast("You must agree to the Terms of Service and Privacy Policy", "error");

  try {
    USER = await api("/auth/signup", {
      method: "POST",
      body: JSON.stringify({ email, password, confirm_password: confirm, agreed_to_tos: true })
    });
    toast("Account setup successful!", "success");
    if (await redirectAfterAuthIfNeeded()) return;
    await enterApp();
  } catch (err) { toast(err, "error"); }
}

/** Honor ?next=… after login/signup. Returns true if navigated away. */
async function redirectAfterAuthIfNeeded() {
  let next = null;
  try {
    next = new URLSearchParams(location.search).get("next");
  } catch (_) {}
  if (!next) {
    try { next = sessionStorage.getItem("post_login_path"); } catch (_) {}
  }
  if (!next) return false;

  let pathOnly = next;
  let search = "";
  try {
    const u = new URL(next, location.origin);
    pathOnly = normalizePath(u.pathname);
    search = u.search || "";
  } catch (_) {
    const qi = String(next).indexOf("?");
    pathOnly = normalizePath(qi >= 0 ? next.slice(0, qi) : next);
    search = qi >= 0 ? next.slice(qi) : "";
  }

  const clear = () => { try { sessionStorage.removeItem("post_login_path"); } catch (_) {} };

  if (pathOnly === "/admin") {
    if (USER && USER.is_admin) {
      clear();
      location.href = "/admin";
      return true;
    }
    return false;
  }
  if (pathOnly === "/checkout/success" || pathOnly === "/upgrade-plans") {
    clear();
    location.href = pathOnly + search;
    return true;
  }
  return false;
}

function openTosModal(e) {
  if (e) e.preventDefault();
  window.open('/terms', '_blank');
}
function openPrivacyModal(e) {
  if (e) e.preventDefault();
  window.open('/privacy', '_blank');
}

async function handleLogoutClick() {
  try { await api("/auth/logout", { method: "POST" }); }
  catch (_) {}
  USER = null;
  try { sessionStorage.removeItem("post_login_path"); } catch (_) {}
  location.href = "/login";
}

async function enterApp() {
  document.getElementById("auth-screen").classList.add("hidden");
  document.getElementById("main-app").classList.remove("hidden");
  document.getElementById("user-email-display").textContent = USER.email;
  updatePlanUI();
  
  if (USER.is_admin) {
    document.getElementById("admin-nav-btn").classList.remove("hidden");
    document.getElementById("admin-topbar-badge")?.classList.remove("hidden");
  }
  
  setupRouter();
  renderModeUI();
  renderBrokerUI();
  await refreshUserData();
  loadBrokerKeys();
  await loadMarketStatus();     // market open/closed drives halt UI + overlay
  if (!window._marketStatusTimer) window._marketStatusTimer = setInterval(loadMarketStatus, 60000);
  if (!window._botsRefreshTimer) window._botsRefreshTimer = setInterval(() => {
    const bv = document.getElementById("view-bots");
    if (bv && !bv.classList.contains("hidden")) loadBots();
  }, 15000);
  if (!window._portfolioRefreshTimer) window._portfolioRefreshTimer = setInterval(() => {
    const pv = document.getElementById("view-portfolio");
    if (pv && !pv.classList.contains("hidden")) loadPortfolioPerformance();
  }, 15000);

  // Resolve deep link / auth alias → canonical dashboard section URL.
  let tab = tabFromPath(location.pathname);
  if (isAuthPath(location.pathname) || !tab) {
    try {
      const saved = sessionStorage.getItem("post_login_path");
      if (saved) {
        let savedPath = saved;
        let savedSearch = "";
        try {
          const u = new URL(saved, location.origin);
          savedPath = normalizePath(u.pathname);
          savedSearch = u.search || "";
        } catch (_) {
          const qi = String(saved).indexOf("?");
          savedPath = normalizePath(qi >= 0 ? saved.slice(0, qi) : saved);
          savedSearch = qi >= 0 ? saved.slice(qi) : "";
        }
        if (savedPath === "/admin" && USER && USER.is_admin) {
          sessionStorage.removeItem("post_login_path");
          location.href = "/admin";
          return;
        }
        if (savedPath === "/checkout/success" || savedPath === "/upgrade-plans") {
          sessionStorage.removeItem("post_login_path");
          location.href = savedPath + savedSearch;
          return;
        }
        sessionStorage.removeItem("post_login_path");
        tab = tabFromPath(savedPath) || "portfolio";
      } else {
        tab = "portfolio";
      }
    } catch (_) {
      tab = "portfolio";
    }
    history.replaceState({ tab }, "", pathForTab(tab));
  } else if (normalizePath(location.pathname) !== normalizePath(pathForTab(tab))) {
    history.replaceState({ tab }, "", pathForTab(tab));
  }

  await activateTab(tab, { push: false });
  connectLiveStream();         // subscribe to the always-on backend feed

  if (!window._focusSyncBound) {
    window._focusSyncBound = true;
    document.addEventListener("visibilitychange", async () => {
      if (document.visibilityState !== "visible" || !USER || _forcingLogout) return;
      try {
        const prevMode = USER.trading_mode;
        const prevBroker = USER.active_broker;
        await refreshUserData();
        if (!USER) return;
        if (USER.trading_mode !== prevMode || USER.active_broker !== prevBroker) {
          renderModeUI();
          renderBrokerUI();
          renderPortfolioModeIndicator();
          const pv = document.getElementById("view-portfolio");
          if (pv && !pv.classList.contains("hidden")) loadPortfolioPerformance();
          const bv = document.getElementById("view-bots");
          if (bv && !bv.classList.contains("hidden")) loadBots();
        }
        updateBotLimitUI();
      } catch (_) {}
    });
  }
}

/* --- LIVE DATA STREAM (Server-Sent Events) --- */
function setLiveIndicator(state) {
  const el = document.getElementById("live-indicator");
  if (!el) return;
  if (state === "live") { el.className = "badge badge-green"; el.textContent = "● Live"; }
  else if (state === "down") { el.className = "badge badge-red"; el.textContent = "● Offline"; }
  else { el.className = "badge badge-amber"; el.textContent = "● Connecting…"; }
}

function schedulePerfReload() {
  // Coalesce bursts of stream events into a single refresh.
  if (_perfReloadTimer) clearTimeout(_perfReloadTimer);
  _perfReloadTimer = setTimeout(() => {
    const pv = document.getElementById("view-portfolio");
    if (pv && !pv.classList.contains("hidden")) loadPortfolioPerformance();
  }, 800);
}

function scheduleBotsReload() {
  // Coalesce quote bursts so /bots is not hammered on every market tick.
  if (window._botsReloadTimer) clearTimeout(window._botsReloadTimer);
  window._botsReloadTimer = setTimeout(() => {
    const bv = document.getElementById("view-bots");
    if (bv && !bv.classList.contains("hidden")) loadBots();
  }, 800);
}

function connectLiveStream() {
  if (EVT_SOURCE) { try { EVT_SOURCE.close(); } catch (_) {} }
  try {
    EVT_SOURCE = new EventSource("/stream/updates", { withCredentials: true });
  } catch (e) { setLiveIndicator("down"); return; }

  EVT_SOURCE.addEventListener("hello", () => setLiveIndicator("live"));
  EVT_SOURCE.addEventListener("ping", () => setLiveIndicator("live"));

  EVT_SOURCE.addEventListener("market_quote", (ev) => {
    setLiveIndicator("live");
    try {
      const q = JSON.parse(ev.data);
      LIVE_QUOTES[`${q.broker}:${q.symbol}`] = q;
      applyLiveQuote(q);
      scheduleBotsReload();
      schedulePerfReload();
    } catch (_) {}
  });

  EVT_SOURCE.addEventListener("trade", (ev) => {
    try {
      const t = JSON.parse(ev.data);
      const verb = t.side === "buy" ? "Bought" : t.side === "sell" ? "Sold" : "Traded";
      toast(`🤖 ${t.bot_name || "Bot"} ${verb} ${Number(t.qty || 0).toFixed(4)} ${t.ticker || ""}`.trim(), "success");
    } catch (_) {}
    const hv = document.getElementById("view-history");
    if (hv && !hv.classList.contains("hidden")) loadTradeHistory();
    const bv = document.getElementById("view-bots");
    if (bv && !bv.classList.contains("hidden")) loadBots();
    schedulePerfReload();
  });

  EVT_SOURCE.addEventListener("portfolio_update", () => schedulePerfReload());

  EVT_SOURCE.onerror = () => {
    setLiveIndicator("down");
    // EventSource auto-reconnects; just reflect the transient state.
  };
}

function applyLiveQuote(q) {
  // If the market dashboard is open on this asset, update its price live.
  if (typeof DASH_STATE !== "undefined" && DASH_STATE &&
      DASH_STATE.symbol && DASH_STATE.exchange &&
      DASH_STATE.symbol.toUpperCase() === q.symbol &&
      DASH_STATE.exchange.toLowerCase() === q.broker) {
    const priceEl = document.getElementById("dash-price");
    if (priceEl) priceEl.textContent = formatPrice(q.price);
  }
}

function setupRouter() {
  document.querySelectorAll(".nav-tab").forEach(tab => {
    if (tab.id === "admin-nav-btn") return;
    if (tab.dataset.routerBound === "1") return;
    tab.dataset.routerBound = "1";
    tab.addEventListener("click", (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
      e.preventDefault();
      const target = tab.getAttribute("data-tab");
      if (!target) return;
      const next = pathForTab(target);
      if (normalizePath(location.pathname) === normalizePath(next)) {
        activateTab(target, { push: false });
        return;
      }
      navigateToTab(target, { push: true });
    });
  });

  if (!window._dashPopstateBound) {
    window._dashPopstateBound = true;
    window.addEventListener("popstate", () => {
      if (!USER) {
        const path = normalizePath(location.pathname);
        if (isDashboardPath(path) || path === "/admin") {
          try { sessionStorage.setItem("post_login_path", path); } catch (_) {}
          history.replaceState({ auth: "login" }, "", "/login");
          toggleAuthMode(true);
          return;
        }
        toggleAuthMode(path !== "/signup");
        return;
      }
      // Logged-in Back onto /login|/signup → snap URL back to the active tab.
      if (isAuthPath(location.pathname)) {
        const active = document.querySelector(".nav-tab.active")?.getAttribute("data-tab");
        const tab = (active && TAB_PATHS[active]) ? active : "portfolio";
        history.replaceState({ tab }, "", pathForTab(tab));
        activateTab(tab, { push: false });
        return;
      }
      const tab = tabFromPath(location.pathname) || "portfolio";
      activateTab(tab, { push: false });
    });
  }
}

function setupTabs() {
  // Back-compat alias — tabs are URL-routed via setupRouter().
  setupRouter();
}

async function refreshUserData() {
  try {
    USER = await api("/auth/me");

    const acctEmail = document.getElementById("account-header-email");
    if (acctEmail) acctEmail.textContent = USER.email || "—";

    updatePlanUI();

    const vText = document.getElementById("verification-text");
    const vBtn = document.getElementById("verify-email-btn");
    if (USER.email_verified) {
      vText.textContent = "Email verified.";
      vText.style.color = "var(--green)";
      vBtn.classList.add("hidden");
    } else {
      // NOTE: Live trading requires verified email + live broker keys (enforced by API).
      vText.textContent = "Email not verified — required before enabling Live trading.";
      vText.style.color = "var(--amber, #f59e0b)";
      vBtn.classList.remove("hidden");
    }
  } catch (e) { if (!_forcingLogout) toast("Session sync failed.", "error"); }
}

function updatePlanUI() {
  if (!USER) return;
  const planName = USER.subscription_plan_name || USER.plan_level || "Starter";
  const canUpgrade = !!USER.can_upgrade;
  const supportPriority = USER.support_priority || "Basic";
  const supportMailto = USER.support_mailto || "support@alphabotixtrading.com";

  const planEl = document.getElementById("account-plan-name");
  if (planEl) planEl.textContent = planName;

  const supportEl = document.getElementById("account-support-priority");
  if (supportEl) supportEl.textContent = supportPriority;

  const supportBtn = document.getElementById("account-support-btn");
  if (supportBtn) {
    const addr = String(supportMailto).replace(/^mailto:/i, "");
    supportBtn.href = "mailto:" + addr;
    supportBtn.title = "Email " + addr;
  }

  const topBtn = document.getElementById("upgrade-topbar-btn");
  if (topBtn) topBtn.classList.toggle("hidden", !canUpgrade);

  const acctBtn = document.getElementById("account-upgrade-btn");
  if (acctBtn) acctBtn.classList.toggle("hidden", !canUpgrade);

  updateBotLimitUI();
}

function botsAtLimit() {
  if (!USER) return false;
  if (USER.bot_limit == null) return false;
  return Number(USER.bot_count || 0) >= Number(USER.bot_limit);
}

function updateBotLimitUI() {
  const btn = document.getElementById("new-bot-btn");
  const hint = document.getElementById("bots-limit-hint");
  const atLimit = botsAtLimit();
  const limit = USER && USER.bot_limit;
  const count = USER ? Number(USER.bot_count || 0) : 0;
  if (btn) {
    btn.disabled = atLimit;
    btn.title = atLimit
      ? `Plan limit reached (${count}/${limit}). Upgrade to create more.`
      : "Create a new bot";
  }
  if (hint) {
    if (USER && limit != null) {
      hint.textContent = atLimit
        ? `${count} / ${limit} bots · limit reached — upgrade for more`
        : `${count} / ${limit} bots used`;
      hint.classList.remove("hidden");
      hint.style.display = "block";
    } else if (USER) {
      hint.textContent = "Unlimited bots";
      hint.classList.remove("hidden");
      hint.style.display = "block";
    } else {
      hint.classList.add("hidden");
      hint.style.display = "";
    }
  }
}

async function openBillingPortal() {
  try {
    const data = await api("/billing/portal", { method: "POST", body: "{}" });
    if (!data || !data.url) throw new Error("No portal URL returned");
    location.href = data.url;
  } catch (err) {
    toast(err, "error");
  }
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
    // The Portfolio is mode-aware: refresh its indicator + data for the new mode.
    renderPortfolioModeIndicator();
    const pv = document.getElementById("view-portfolio");
    if (pv && !pv.classList.contains("hidden")) loadPortfolioPerformance();
    // The Bots tab is also mode-filtered — re-render it if it's showing.
    const bv = document.getElementById("view-bots");
    if (bv && !bv.classList.contains("hidden")) loadBots();
    const paused = Number(data.paused_count || 0);
    if (paused > 0) {
      toast(`Switched to ${mode}. Paused ${paused} bot${paused === 1 ? "" : "s"} from the previous mode.`, "success");
    } else {
      toast(`Switched to ${mode} trading`, "success");
    }
  } catch (e) { toast(e, "error"); }
}

// Portfolio-page mode toggle — switches the whole account mode (paper/live) and
// re-renders the Portfolio so all P&L / charts / bots reflect that account only.
function setPortfolioMode(mode) {
  if (((USER && USER.trading_mode) || "paper") === mode) return;
  setTradingMode(mode);
}

// Reflects the active trading mode in the Portfolio header (label + badge + toggle).
function renderPortfolioModeIndicator() {
  const mode = (USER && USER.trading_mode) || "paper";
  const label = document.getElementById("pf-mode-label");
  if (label) label.textContent = mode === "live" ? "Live Trading Portfolio" : "Paper Trading Portfolio";
  const badge = document.getElementById("pf-mode-badge");
  if (badge) {
    badge.textContent = mode === "live" ? "⚡ Live" : "● Paper";
    badge.className = "badge " + (mode === "live" ? "badge-red" : "badge-green");
  }
  const pB = document.getElementById("pf-mode-paper"), lB = document.getElementById("pf-mode-live");
  if (pB) pB.classList.toggle("active", mode === "paper");
  if (lB) lB.classList.toggle("active", mode === "live");
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
    syncScattershotOption();
    renderMarketOverlay();
    toast(`Switched to ${broker}`, "success");
  } catch (e) { toast(e, "error"); }
}

async function handleSaveAlpacaKeys(mode) {
  const api_key = document.getElementById(`alpaca-${mode}-key`).value.trim();
  const secret_key = document.getElementById(`alpaca-${mode}-secret`).value.trim();
  if (!api_key || !secret_key) return toast(`Enter both ${mode} Alpaca keys`, "error");
  if (api_key.includes("•") || secret_key.includes("•")) {
    return toast("Masked keys can't be re-saved. Paste your full API key and secret to update.", "error");
  }
  try {
    await api("/broker/alpaca/keys", { method: "POST", body: JSON.stringify({ api_key, secret_key, mode }) });
    toast(`Alpaca ${mode} keys saved.`, "success");
    await loadBrokerKeys();
  } catch (e) { toast(e, "error"); }
}

async function handleSaveOkxKeys(mode) {
  const api_key = document.getElementById(`okx-${mode}-key`).value.trim();
  const secret_key = document.getElementById(`okx-${mode}-secret`).value.trim();
  const passphrase = document.getElementById(`okx-${mode}-pass`).value.trim();
  if (!api_key || !secret_key || !passphrase) return toast(`Enter all ${mode} OKX fields`, "error");
  if (api_key.includes("•") || secret_key.includes("•") || passphrase.includes("•")) {
    return toast("Masked keys can't be re-saved. Paste full OKX credentials to update.", "error");
  }
  try {
    await api("/broker/okx/keys", { method: "POST", body: JSON.stringify({ api_key, secret_key, passphrase, mode }) });
    toast(`OKX ${mode} keys saved.`, "success");
    await loadBrokerKeys();
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
    const res = await api("/auth/trigger-verification", { method: "POST" });
    if (res && (res.email_not_configured || res.smtp_not_configured)) {
      const vText = document.getElementById("verification-text");
      if (vText) {
        vText.textContent = (res.detail) || "Email sending is not configured on this server yet (set RESEND_API_KEY). Live trading still requires a verified email once email delivery is available.";
        vText.style.color = "var(--amber, #f59e0b)";
      }
      toast("Email delivery is not configured — Live trading still requires verification when email is available.", "");
    } else {
      toast("Verification code sent to your inbox!", "success");
      document.getElementById("verify-modal").classList.remove("hidden");
    }
  } catch (e) {
    toast((e && e.message) || "Failed to send verification email.", "error");
  }
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

/* ════════════════════════════════════════════════════════════════════
   PORTFOLIO TAB — holographic glassmorphism component (LIVE DATA)
   Charts are driven by the real backend:
     • GET /api/portfolio/performance  -> total valuation, cumulative P/L
       series (sliced per timeframe), buy/sell markers, 24h winner/loser.
     • GET /bots                       -> active-bot list + allocations.
   It refreshes on tab open, on the Refresh hooks, and live via the SSE
   stream (schedulePerfReload on trade/portfolio events).
   ════════════════════════════════════════════════════════════════════ */
let PF_STATE = { timeframe: "1D" };   // default timeframe = 1 Day
let PF_DATA = null;                   // { perf: <performance json>, bots: [...] }
let PF_MAIN_CHART = null;             // lightweight-charts instance for the main chart
let BOT_MINI_CHARTS = {};             // botId -> chart instance for expanded panels
let ACTIVE_BOTS_OPEN = true;          // "Active Bots" accordion open/closed
let PF_DETAILS_OPEN = false;          // Portfolio details collapsed by default (show background)

const PF_GREEN = "#00d68f", PF_RED = "#ff4d6a";
const TF_SECONDS = { "1D": 86400, "1W": 604800, "1M": 2592000, "1Y": 31536000, "5Y": 157680000 };

function money(v) { return "$" + Number(v || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
function fmtSignedMoney(v) { const n = Number(v || 0); return (n >= 0 ? "+" : "-") + "$" + Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
function fmtSignedPct(v) { const n = Number(v || 0); return (n >= 0 ? "+" : "") + n.toFixed(2) + "%"; }

// Shared transparent (holographic) chart options so the glass + globe show through.
function _holoChartOpts() {
  return {
    layout: { background: { color: "rgba(0,0,0,0)" }, textColor: "#a9c7e0", fontSize: 11 },
    grid: { vertLines: { color: "rgba(57,217,255,0.07)" }, horzLines: { color: "rgba(57,217,255,0.07)" } },
    rightPriceScale: { borderColor: "rgba(57,217,255,0.18)" },
    timeScale: { borderColor: "rgba(57,217,255,0.18)", timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0, vertLine: { color: "rgba(57,217,255,0.4)" }, horzLine: { color: "rgba(57,217,255,0.4)" } },
    // Render the crosshair time in the user's local timezone (auto-detected).
    localization: { timeFormatter: (t) => new Date(t * 1000).toLocaleString() },
    autoSize: true,
  };
}

// Slice the real cumulative-P/L series to the selected timeframe and rebase to
// 0 at the window start, so the line shows the actual gain/loss WITHIN it.
function _tfWindow(series, tf) {
  if (!Array.isArray(series) || !series.length) return [];
  const now = Math.floor(Date.now() / 1000), cutoff = now - (TF_SECONDS[tf] || 86400);
  let baseline = series[0].value;
  const inWin = [];
  for (const p of series) {
    if (p.time < cutoff) baseline = p.value;   // last known value before the window
    else inWin.push(p);
  }
  if (!inWin.length) return [];
  const pts = [];
  if (inWin[0].time > cutoff) pts.push({ time: cutoff, value: 0 });   // anchor window start at 0
  for (const p of inWin) pts.push({ time: p.time, value: +(p.value - baseline).toFixed(2) });
  return pts;
}

// Real 24h winner/loser from the trade ledger (sum of realized P/L per bot on
// SELL fills in the last 24h).
function _winnerLoser24h(perf) {
  const cutoff = Math.floor(Date.now() / 1000) - 86400, byBot = {};
  for (const m of (perf.markers || [])) {
    if (m.type && m.type.indexOf("sell") === 0 && m.time >= cutoff && m.pnl != null) {
      const k = m.bot_name || ("Bot " + m.bot_id);
      byBot[k] = (byBot[k] || 0) + Number(m.pnl);
    }
  }
  const e = Object.entries(byBot);
  if (!e.length) return null;
  e.sort((a, b) => b[1] - a[1]);
  return { winner: { name: e[0][0], pnl: e[0][1] }, loser: { name: e[e.length - 1][0], pnl: e[e.length - 1][1] } };
}

// Per-bot execution chart data straight from the real trade ledger markers.
function _botExecData(botId) {
  const ms = (PF_DATA.perf.markers || []).filter(m => m.bot_id === botId).sort((a, b) => a.time - b.time);
  const seen = {}, series = [], markers = [];
  for (const m of ms) {
    if (seen[m.time]) continue;   // lightweight-charts requires unique, ascending times
    seen[m.time] = 1;
    series.push({ time: m.time, value: Number(m.price || 0) });
    markers.push({ time: m.time, type: m.type === "buy" ? "buy" : "sell" });
  }
  return { series, markers };
}

// Public entry — called on boot, tab switch and SSE refresh. Pulls LIVE data.
// Public entry — loads the Portfolio for the account's CURRENT trading mode.
async function loadPortfolioPerformance() {
  return loadPortfolioForMode((USER && USER.trading_mode) || "paper");
}

// Fetches + renders the Portfolio for ONE trading mode. Paper and Live are
// fully separated: the backend only returns that mode's trades, and open
// positions only count toward the account you're currently live in.
async function loadPortfolioForMode(mode) {
  renderPortfolioModeIndicator();
  const msg = document.getElementById("pf-main-chart-msg");
  let perf, bots;
  try {
    [perf, bots] = await Promise.all([
      api(`/api/portfolio/performance?mode=${encodeURIComponent(mode)}`),
      api("/bots"),
    ]);
  } catch (e) {
    if (_forcingLogout) return;
    if (msg) { msg.classList.remove("hidden"); msg.textContent = `⚠ ${e.message}`; }
    PF_DATA = null;
    const tEl = document.getElementById("pf-total-value");
    if (tEl) tEl.textContent = "—";
    const pnlEl = document.getElementById("pf-total-pnl");
    if (pnlEl) {
      pnlEl.textContent = "—";
      pnlEl.className = "pf-total-pnl";
    }
    const dEl = document.getElementById("pf-total-delta");
    if (dEl) { dEl.textContent = ""; dEl.className = "pf-delta"; }
    if (PF_MAIN_CHART) { try { PF_MAIN_CHART.remove(); } catch (_) {} PF_MAIN_CHART = null; }
    const win = document.getElementById("pf-winner");
    if (win) win.innerHTML = "";
    const lose = document.getElementById("pf-loser");
    if (lose) lose.innerHTML = "";
    const abl = document.getElementById("active-bots-list");
    if (abl) abl.innerHTML = `<div style="color:var(--t2);padding:8px">Portfolio data unavailable.</div>`;
    return;
  }
  PF_DATA = { perf: perf || {}, bots: Array.isArray(bots) ? bots : (bots && bots.bots) || [], mode };

  // Total current asset valuation held by active bots = open-position cost
  // basis + live unrealized mark-to-market.
  const total = Number(perf.funds_allocated || 0) + Number(perf.unrealized || 0);
  const tEl = document.getElementById("pf-total-value");
  if (tEl) tEl.textContent = money(total);

  // Overall P/L (realized + unrealized) — sticky header figure.
  const totalPnl = (perf.live_value != null)
    ? Number(perf.live_value)
    : (Number(perf.net_position || 0) + Number(perf.unrealized || 0));
  const pnlEl = document.getElementById("pf-total-pnl");
  if (pnlEl) {
    pnlEl.textContent = fmtSignedMoney(totalPnl);
    pnlEl.className = "pf-total-pnl " + (totalPnl >= 0 ? "pup" : "pdn");
  }

  const sel = document.getElementById("pf-timeframe");
  if (sel) sel.value = PF_STATE.timeframe;

  syncPortfolioDetailsUI();
  renderPortfolioMainChart();
  renderWinnerLoser();
  renderActiveBots();
  renderMarketOverlay();
}

function syncPortfolioDetailsUI() {
  const details = document.getElementById("pf-details");
  const toggle = document.getElementById("pf-details-toggle");
  const label = document.getElementById("pf-details-toggle-label");
  if (details) {
    details.classList.toggle("is-open", PF_DETAILS_OPEN);
    details.classList.toggle("collapsed", !PF_DETAILS_OPEN);
  }
  if (toggle) toggle.setAttribute("aria-expanded", String(PF_DETAILS_OPEN));
  if (label) label.textContent = PF_DETAILS_OPEN ? "Hide details" : "Show details";
}

function togglePortfolioDetails() {
  PF_DETAILS_OPEN = !PF_DETAILS_OPEN;
  syncPortfolioDetailsUI();
  // Chart needs a layout pass after the accordion opens.
  if (PF_DETAILS_OPEN) {
    setTimeout(() => {
      try { renderPortfolioMainChart(); } catch (_) {}
      try {
        (PF_DATA && PF_DATA.bots || []).forEach(b => {
          const el = document.getElementById(`mini-bot-chart-${b.id}`);
          if (el && el.childNodes.length) { /* already drawn */ }
        });
      } catch (_) {}
    }, 360);
  }
}
window.togglePortfolioDetails = togglePortfolioDetails;

/* Generic collapsible details for Markets / Bots / News / History / Assets / Account */
const TAB_DETAILS_OPEN = Object.create(null);

function syncTabDetailsUI(key) {
  const open = !!TAB_DETAILS_OPEN[key];
  const details = document.getElementById(`${key}-details`);
  const toggle = document.getElementById(`${key}-details-toggle`);
  const label = document.getElementById(`${key}-details-toggle-label`);
  if (details) {
    details.classList.toggle("is-open", open);
    details.classList.toggle("collapsed", !open);
  }
  if (toggle) toggle.setAttribute("aria-expanded", String(open));
  if (label) label.textContent = open ? "Hide details" : "Show details";
}

function toggleTabDetails(key) {
  TAB_DETAILS_OPEN[key] = !TAB_DETAILS_OPEN[key];
  syncTabDetailsUI(key);
}
window.toggleTabDetails = toggleTabDetails;

function setPortfolioTimeframe(tf) {
  PF_STATE.timeframe = tf;
  renderPortfolioMainChart();   // re-slice cached live series, no refetch needed
}

// Semi-transparent "MARKET OFFLINE" stamp over the main chart when closed.
function renderMarketOverlay() {
  const ov = document.getElementById("pf-market-overlay");
  if (!ov) return;
  // US equities session status does not apply to 24/7 OKX crypto.
  const closed = isMarketClosed() && !(USER && USER.active_broker === "okx");
  ov.classList.toggle("hidden", !closed);
  if (closed) {
    const sub = document.getElementById("pf-market-overlay-sub");
    if (sub) sub.textContent = `No active trades. Trading algorithms in standby mode. Markets will initialize at ${fmtLocalOpenTime()}.`;
  }
}

function renderPortfolioMainChart() {
  if (!PF_DATA) return;
  // Don't build the chart while details are collapsed (zero-size container).
  if (!PF_DETAILS_OPEN) return;
  const wrap = document.getElementById("pf-main-chart");
  const msg = document.getElementById("pf-main-chart-msg");
  if (!wrap) return;
  const series = _tfWindow(PF_DATA.perf.series || [], PF_STATE.timeframe);
  const net = series.length ? series[series.length - 1].value : 0;

  const dEl = document.getElementById("pf-total-delta");
  if (dEl) {
    // Only show a percentage when there is a meaningful capital base to divide
    // by (open allocation); otherwise the % degenerates (flat account).
    const base = Number(PF_DATA.perf.funds_allocated || 0);
    const pctStr = base > 0 ? ` (${fmtSignedPct(net / base * 100)})` : "";
    dEl.textContent = `${fmtSignedMoney(net)}${pctStr} · ${PF_STATE.timeframe}`;
    dEl.className = "pf-delta " + (net >= 0 ? "pup" : "pdn");
  }

  if (PF_MAIN_CHART) { try { PF_MAIN_CHART.remove(); } catch (_) {} PF_MAIN_CHART = null; }
  if (typeof LightweightCharts === "undefined") {
    if (msg) { msg.classList.remove("hidden"); msg.textContent = "Chart library unavailable (offline)."; }
    return;
  }
  if (!series.length) {
    if (msg) { msg.classList.remove("hidden"); msg.textContent = "No portfolio history in this timeframe yet — it fills in as your bots trade."; }
    return;
  }
  if (msg) msg.classList.add("hidden");

  PF_MAIN_CHART = LightweightCharts.createChart(wrap, _holoChartOpts());
  // Line color logic: GREEN for an overall net gain, RED for a net loss.
  const line = PF_MAIN_CHART.addLineSeries({
    color: net >= 0 ? PF_GREEN : PF_RED,
    lineWidth: 2, priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: true,
  });
  line.setData(series);
  // The main historical chart intentionally has NO buy/sell markers.
  PF_MAIN_CHART.timeScale().fitContent();
}

function renderWinnerLoser() {
  if (!PF_DATA) return;
  const wl = _winnerLoser24h(PF_DATA.perf);
  const setCard = (id, name, pnl, cls) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.querySelector(".perf-wl-name").textContent = name;
    const v = el.querySelector(".perf-wl-val");
    v.textContent = (pnl == null) ? "—" : fmtSignedMoney(pnl);
    v.className = "perf-wl-val " + (cls || "");
  };
  if (!wl) {
    setCard("pf-winner", "No closed trades in last 24h", null, "");
    setCard("pf-loser", "No closed trades in last 24h", null, "");
    return;
  }
  setCard("pf-winner", wl.winner.name, wl.winner.pnl, "pup");
  setCard("pf-loser", wl.loser.name, wl.loser.pnl, "pdn");
}

function toggleActiveBots() {
  ACTIVE_BOTS_OPEN = !ACTIVE_BOTS_OPEN;
  const c = document.querySelector(".active-bots");
  if (c) c.classList.toggle("collapsed", !ACTIVE_BOTS_OPEN);
  const t = document.getElementById("active-bots-toggle");
  if (t) t.setAttribute("aria-expanded", String(ACTIVE_BOTS_OPEN));
}

function renderActiveBots() {
  if (!PF_DATA) return;
  const list = document.getElementById("active-bots-list");
  const cnt = document.getElementById("active-bots-count");
  if (!list) return;

  // Scope the list to the selected mode: a bot belongs to this view if it has
  // executions in this mode (its markers are already mode-filtered by the API),
  // or it is currently holding a position in the account you're live in.
  const mode = PF_DATA.mode || (USER && USER.trading_mode) || "paper";
  const isActiveMode = mode === ((USER && USER.trading_mode) || "paper");
  const markers = (PF_DATA.perf && PF_DATA.perf.markers) || [];
  const pnlByBot = {}, activeIds = new Set();
  for (const m of markers) {
    if (m.bot_id == null) continue;
    activeIds.add(m.bot_id);
    if (m.type && m.type.indexOf("sell") === 0 && m.pnl != null) {
      pnlByBot[m.bot_id] = (pnlByBot[m.bot_id] || 0) + Number(m.pnl);
    }
  }
  const bots = (PF_DATA.bots || []).filter(b => activeIds.has(b.id) || (isActiveMode && b.in_position));
  if (cnt) cnt.textContent = bots.length;
  if (!bots.length) {
    const label = mode === "live" ? "live" : "paper";
    list.innerHTML = `<div style="color:rgba(234,246,255,0.6);font-size:13px;padding:6px 2px">No ${label}-mode bot activity yet${isActiveMode ? " — create one in the Bots tab." : "."}</div>`;
    return;
  }
  list.innerHTML = bots.map(b => {
    const realized = Number(pnlByBot[b.id] || 0);   // realized P&L for THIS mode
    const alloc = Number(b.funds_allocated || 0);
    const plPct = alloc > 0 ? (realized / alloc * 100) : 0;
    const plCls = plPct >= 0 ? "pup" : "pdn";
    const tks = b.ticker ? [b.ticker] : (b.auto_select ? ["AUTO"] : ["—"]);
    const tickers = tks.map(t => `<span class="tk">${esc(t)}</span>`).join("");
    const posBadge = (isActiveMode && b.in_position) ? `<span class="badge badge-blue" style="font-size:9px;margin-left:6px">In position</span>` : "";
    return `
    <div class="mini-bot" id="mini-bot-${b.id}">
      <div class="mini-bot-row" onclick="toggleBotPanel(${b.id})">
        <span class="mini-bot-caret">▸</span>
        <div class="mini-bot-main">
          <div class="mini-bot-name">${esc(b.name)}${posBadge}</div>
          <div class="mini-bot-tickers">${tickers}</div>
        </div>
        <div class="mini-bot-alloc">
          <div class="amt">${money(alloc)}</div>
          <div class="pl ${plCls}">${fmtSignedPct(plPct)}</div>
        </div>
      </div>
      <div class="mini-bot-panel">
        <div class="mini-bot-legend">
          <span class="legend-dot" style="background:${PF_GREEN}"></span> Buy
          <span class="legend-dot" style="background:${PF_RED};margin-left:10px"></span> Sell
          · live execution history
        </div>
        <div class="mini-bot-chart" id="mini-bot-chart-${b.id}"></div>
        <div class="mini-bot-empty hidden" id="mini-bot-empty-${b.id}" style="color:rgba(234,246,255,0.55);font-size:12px;padding:8px 2px">No executions recorded yet for this bot.</div>
      </div>
    </div>`;
  }).join("");
}

function toggleBotPanel(id) {
  const card = document.getElementById(`mini-bot-${id}`);
  if (!card) return;
  const opening = !card.classList.contains("open");
  card.classList.toggle("open", opening);
  if (opening) {
    renderBotMiniChart(id);
  } else if (BOT_MINI_CHARTS[id]) {
    try { BOT_MINI_CHARTS[id].remove(); } catch (_) {}
    delete BOT_MINI_CHARTS[id];
  }
}

function renderBotMiniChart(id) {
  if (!PF_DATA) return;
  const wrap = document.getElementById(`mini-bot-chart-${id}`);
  const empty = document.getElementById(`mini-bot-empty-${id}`);
  if (!wrap || typeof LightweightCharts === "undefined") return;
  if (BOT_MINI_CHARTS[id]) { try { BOT_MINI_CHARTS[id].remove(); } catch (_) {} delete BOT_MINI_CHARTS[id]; }
  const { series, markers } = _botExecData(id);
  if (!series.length) { wrap.style.display = "none"; if (empty) empty.classList.remove("hidden"); return; }
  wrap.style.display = ""; if (empty) empty.classList.add("hidden");
  const chart = LightweightCharts.createChart(wrap, _holoChartOpts());
  const net = series.length ? (series[series.length - 1].value - series[0].value) : 0;
  const line = chart.addLineSeries({ color: net >= 0 ? PF_GREEN : PF_RED, lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
  line.setData(series);
  // Buy/Sell execution dots — present ONLY on these per-bot mini charts.
  line.setMarkers(markers.map(m => ({
    time: m.time,
    position: m.type === "buy" ? "belowBar" : "aboveBar",
    color: m.type === "buy" ? PF_GREEN : PF_RED,
    shape: "circle", size: 1.6, text: m.type === "buy" ? "B" : "S",
  })));
  chart.timeScale().fitContent();
  BOT_MINI_CHARTS[id] = chart;
}

async function loadBrokerAccount() {
  const el = document.getElementById("broker-account-info");
  if (!el) return;
  // Header reflects the *active* broker session so it's clear whose balance this is.
  const broker = USER ? (USER.active_broker || "alpaca") : "alpaca";
  const mode = USER ? (USER.trading_mode || "paper") : "paper";
  const brokerLabel = broker === "okx" ? "OKX" : "Alpaca";
  const modeLabel = mode === "live" ? "Live" : "Paper";
  const header =
    `<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
       <span class="badge badge-blue">${brokerLabel}</span>
       <span class="badge ${mode === "live" ? "badge-red" : "badge-green"}">${modeLabel}</span>
     </div>`;
  el.innerHTML = header + '<span style="color:var(--t3)">Fetching live balance from broker…</span>';
  try {
    const data = await api("/broker/account");
    let rowsHtml = "";
    let headerCash = null;
    if (data && data.balances && typeof data.balances === "object" && !Array.isArray(data.balances)) {
      // OKX (and similar) return { balances: { USDT: n, ... } }
      const entries = Object.entries(data.balances).filter(([, v]) => v != null && Number(v) !== 0);
      rowsHtml = entries.length
        ? entries.map(([k, v]) => {
            const num = Number(v);
            const display = Number.isFinite(num)
              ? num.toLocaleString(undefined, { maximumFractionDigits: 8 })
              : esc(String(v));
            return `<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border);font-size:13px">
              <span style="color:var(--t2)">${esc(k)}</span>
              <span style="font-weight:500">${display}</span>
            </div>`;
          }).join("")
        : `<div style="color:var(--t2);font-size:13px">No balances reported.</div>`;
      const usdt = data.balances.USDT ?? data.balances.USD ?? data.balances.USDC;
      if (usdt != null && Number.isFinite(Number(usdt))) headerCash = Number(usdt);
    } else {
      rowsHtml = Object.entries(data || {})
        .filter(([k, v]) => k !== "error" && (typeof v === "number" || typeof v === "string"))
        .map(([k, v]) =>
          `<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border);font-size:13px">
            <span style="color:var(--t2)">${esc(k.replace(/_/g, " "))}</span>
            <span style="font-weight:500">${typeof v === "number" ? "$" + Number(v).toLocaleString() : esc(String(v))}</span>
          </div>`
        ).join("");
      headerCash = data.cash ?? data.buying_power ?? data.equity ?? data.balance;
      if (headerCash != null) headerCash = Number(headerCash);
    }
    el.innerHTML = header + (rowsHtml || `<div style="color:var(--t2);font-size:13px">No account fields returned.</div>`);
    const cashEl = document.getElementById("assets-header-cash");
    if (cashEl) {
      cashEl.textContent = (typeof headerCash === "number" && Number.isFinite(headerCash))
        ? "$" + headerCash.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
        : "—";
    }
  } catch (e) { el.innerHTML = header + `<span style="color:var(--red)">⚠ ${e.message}</span>`; }
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
function updateLowBalanceStrategyHint() {
  const el = document.getElementById("b-low-balance-strategy");
  const hint = document.getElementById("low-balance-strategy-hint");
  const tooltip = document.getElementById("low-balance-strategy-tooltip");
  if (!el || !hint) return;
  const desc = LOW_BALANCE_DESCRIPTIONS[el.value] || LOW_BALANCE_DESCRIPTIONS.standard;
  hint.textContent = desc;
  if (tooltip) {
    tooltip.textContent = desc;
    tooltip.classList.toggle("hidden", !desc);
  }
}

// Show the low-balance description on hover/focus for better discoverability.
function _attachLowBalanceHover() {
  const el = document.getElementById("b-low-balance-strategy");
  const hint = document.getElementById("low-balance-strategy-hint");
  if (!el || !hint) return;
  el.addEventListener("mouseenter", () => {
    hint.textContent = LOW_BALANCE_DESCRIPTIONS[el.value] || LOW_BALANCE_DESCRIPTIONS.standard;
  });
  el.addEventListener("focus", () => {
    hint.textContent = LOW_BALANCE_DESCRIPTIONS[el.value] || LOW_BALANCE_DESCRIPTIONS.standard;
  });
  el.addEventListener("mouseleave", () => updateLowBalanceStrategyHint());
  el.addEventListener("blur", () => updateLowBalanceStrategyHint());
}

// Attach hover listeners immediately (script is loaded at end of body).
try { _attachLowBalanceHover(); } catch (e) { /* non-fatal */ }

function showCreateBotModal() {
  if (botsAtLimit()) {
    const limit = USER.bot_limit;
    const count = Number(USER.bot_count || 0);
    toast(`Plan limit reached (${count}/${limit}). Upgrade your plan to create more bots.`, "error");
    return;
  }
  resetCreateBotForm();
  syncScattershotOption();
  updateLowBalanceStrategyHint();
  document.getElementById("modal-bot").classList.remove("hidden");
}
function hideCreateBotModal() { document.getElementById("modal-bot").classList.add("hidden"); }

function resetCreateBotForm() {
  setBotMode("auto");
  const name = document.getElementById("b-name");
  const funds = document.getElementById("b-funds");
  const ticker = document.getElementById("b-ticker");
  const buy = document.getElementById("b-buy");
  const sell = document.getElementById("b-sell");
  const strategy = document.getElementById("b-low-balance-strategy");
  if (name) name.value = "";
  if (funds) funds.value = "";
  if (ticker) ticker.value = "";
  if (buy) buy.value = "";
  if (sell) sell.value = "";
  if (strategy) strategy.value = "standard";
  updateLowBalanceStrategyHint();
}

function syncScattershotOption() {
  const el = document.getElementById("b-low-balance-strategy");
  if (!el) return;
  const opt = el.querySelector('option[value="scattershot"]');
  if (!opt) return;
  const okx = !!(USER && USER.active_broker === "okx");
  opt.disabled = okx;
  opt.hidden = okx;
  if (okx && el.value === "scattershot") {
    el.value = "standard";
    updateLowBalanceStrategyHint();
  }
}

async function createBot() {
  const name = document.getElementById("b-name").value.trim();
  const funds_allocated = parseFloat(document.getElementById("b-funds").value);
  // Fully autonomous mode sends NO ticker — the engine picks the asset.
  const ticker = BOT_MODE === "manual"
    ? (document.getElementById("b-ticker").value.trim().toUpperCase() || null)
    : null;
  const buy_limit = document.getElementById("b-buy").value ? +document.getElementById("b-buy").value : null;
  const sell_limit = document.getElementById("b-sell").value ? +document.getElementById("b-sell").value : null;
  const low_balance_strategy = document.getElementById("b-low-balance-strategy").value;

  if (!funds_allocated || funds_allocated <= 0) return toast("Enter a funds amount to allocate", "error");
  if (BOT_MODE === "manual" && !ticker) return toast("Manual bots need a ticker symbol", "error");

  try {
    await api("/bots", {
      method: "POST",
      body: JSON.stringify({
        name, ticker, funds_allocated,
        low_balance_strategy,
        broker: USER ? (USER.active_broker || "alpaca") : "alpaca",
        timeframe: "1h",
        is_auto: BOT_MODE === "auto", buy_limit, sell_limit
      })
    });
    const launchedAuto = BOT_MODE === "auto";
    hideCreateBotModal();
    resetCreateBotForm();
    toast(launchedAuto ? "Autonomous bot launched — it will scan all markets" : "Bot launched", "success");
    if (USER) USER.bot_count = Number(USER.bot_count || 0) + 1;
    updateBotLimitUI();
    await refreshUserData();
    await loadBots();
  } catch (e) { if (!_forcingLogout) toast(e, "error"); }
}

async function loadBots() {
  try {
    const res = await api("/bots");
    BOTS = Array.isArray(res) ? res : (res.bots || []);
    if (USER) {
      USER.bot_count = BOTS.length;
      updateBotLimitUI();
    }
    renderBots();
  } catch (e) { if (!_forcingLogout) toast(e, "error"); }
}

function renderBots() {
  const el = document.getElementById("bots-list-container");
  if (!el) return;

  // ── Mode-aware view filter (UI ONLY) ──────────────────────────────────
  // Group the user's bots by the account they're assigned to (bot.mode from
  // GET /bots). Both arrays are kept so it's clear how the view filters; we
  // render only the one for the current trading mode. This is purely a display
  // filter — hidden bots keep running on the backend scheduler untouched.
  const mode = (USER && USER.trading_mode) || "paper";
  const activePaperBots = BOTS.filter(b => ((b.mode || "paper") === "paper"));
  const activeLiveBots  = BOTS.filter(b => ((b.mode || "paper") === "live"));
  const visible = mode === "live" ? activeLiveBots : activePaperBots;

  const hdrCount = document.getElementById("bots-header-count");
  if (hdrCount) hdrCount.textContent = String(visible.filter(b => b.running).length);

  // Reflect the active account in the Bots-tab header.
  const badge = document.getElementById("bots-mode-badge");
  if (badge) {
    badge.textContent = mode === "live" ? "⚡ Live" : "● Paper";
    badge.className = "badge " + (mode === "live" ? "badge-red" : "badge-green");
    badge.style.fontSize = "10px"; badge.style.verticalAlign = "middle";
  }
  const sub = document.getElementById("bots-mode-sub");
  if (sub) sub.textContent = `Showing your ${mode === "live" ? "Live" : "Paper"} account bots (${visible.length}) · switch account in the top bar or Account tab`;

  if (!visible.length) {
    const other = mode === "live" ? activePaperBots.length : activeLiveBots.length;
    el.innerHTML = `<div style="text-align:center;color:var(--t2);padding:20px">No ${mode === "live" ? "Live" : "Paper"} account bots yet.` +
      (other ? ` You have ${other} bot(s) in your ${mode === "live" ? "Paper" : "Live"} account — switch accounts to see them.` : ` Create one with “+ New Bot”.`) +
      `</div>`;
    return;
  }
  el.innerHTML = visible.map((b) => {
    const botMode = (b.mode || "paper");
    const modeBadge = botMode === "live"
      ? `<span class="badge badge-mode-live" title="Live trading account">LIVE</span>`
      : `<span class="badge badge-mode-paper" title="Paper trading account">PAPER</span>`;
    // Equities halt when the US market is closed; crypto (OKX) trades 24/7.
    const isStock = (b.broker || "alpaca").toLowerCase() !== "okx";
    const haltRow = (isStock && isMarketClosed())
      ? `<div class="bot-halt">🛑 SYSTEM HALT: Market offline. Core trading loops suspended. Awaiting market open at ${esc(fmtLocalOpenTime())}.</div>`
      : "";
    const sigColor = b.last_signal === "BUY" ? "badge-green" : b.last_signal === "SELL" ? "badge-red" : "badge-amber";
    const entryPrice = (b.entry_price ?? b.avg_entry_price);
    const currentPrice = (b.current_price ?? entryPrice);
    const displayStop = (b.display_stop_price ?? b.stop_price);
    const displayTarget = (b.display_take_profit_price ?? b.take_profit_price);
    const realized = Number(b.realized_pnl || 0);
    const unrealized = (typeof b.unrealized_pl === "number" && b.in_position) ? Number(b.unrealized_pl) : 0;
    const livePnl = realized + unrealized;
    const pos = b.in_position
      ? `<span class="badge badge-blue">In position</span>`
      : `<span class="badge badge-amber">Flat — scanning</span>`;
    const pnlColor = livePnl >= 0 ? PF_GREEN : PF_RED;
    const assetLabel = b.ticker ? esc(b.ticker) : (b.auto_select ? "🧠 Auto-select (all markets)" : "Auto");
    const strategyLabel = b.low_balance_strategy_label || b.low_balance_strategy || "Standard";
    const cooldownNote = (b.strategy_cooldown_until && new Date(b.strategy_cooldown_until) > new Date())
      ? `<span class="badge badge-amber" title="Settlement cooldown active">Cooldown</span>`
      : "";
    const scattershotNote = (b.scattershot_legs && b.scattershot_legs.length)
      ? `<span class="badge badge-purple" title="${esc(b.scattershot_legs.map(l => l.ticker).join(', '))}">${b.scattershot_legs.length}-leg basket</span>`
      : "";
    const swingNote = (b.low_balance_strategy === "swing_trader" && b.in_position && typeof b.swing_hold_days === "number")
      ? `<span class="badge badge-blue" title="Minimum 3-day swing hold">Day ${b.swing_hold_days}</span>`
      : "";
    const chartTicker = b.ticker ? String(b.ticker).split(",")[0].trim() : "";
    const chartBtn = chartTicker
      ? `<button class="btn btn-sm" onclick="openMarketDashboard(${escJs(b.broker||'alpaca')},${escJs(chartTicker)})">📈 Chart</button>`
      : "";
    // Manual Sell — only meaningful while the bot is actually holding a position.
    const sellAllBtn = b.in_position
      ? `<button class="btn btn-sm btn-warning" title="Sell all of this bot's holdings now" onclick="sellBot(${b.id})">💵 Sell All</button>`
      : "";
    return `
    <div class="bot-card ${b.running ? "running" : "paused"}">
      <div style="display:flex;justify-content:space-between;margin-bottom:10px">
        <div style="font-weight:700">${esc(b.name)}
          ${modeBadge}
          <span class="badge ${b.running?"badge-green":"badge-amber"}">${b.running?"Running":"Paused"}</span>
          ${b.auto_select ? `<span class="badge badge-purple">Autonomous</span>` : ""}
          ${b.last_signal ? `<span class="badge ${sigColor}">${esc(b.last_signal)}</span>` : ""}
        </div>
        <div style="display:flex;gap:6px">
          ${chartBtn}
          ${sellAllBtn}
          <button class="btn btn-sm" onclick="toggleBot(${b.id})">${b.running?"⏸":"▶"}</button>
          <button class="btn btn-sm btn-danger" onclick="deleteBot(${b.id})">🗑</button>
        </div>
      </div>
      ${haltRow}
      <div style="color:var(--t2);font-size:12px;margin-bottom:8px">
        ${assetLabel} · ${esc((b.broker||"alpaca").toUpperCase())} · ${esc(b.timeframe||"1h")} |
        Strategy: ${esc(strategyLabel)} ${cooldownNote} ${scattershotNote} ${swingNote} |
        Funds: $${b.funds_allocated} | Trades: ${b.trade_count} |
        P&L: <span style="color:${pnlColor}">${fmtSignedMoney(livePnl)}</span>
      </div>
      <div style="margin-bottom:8px">${pos}
        ${b.in_position && entryPrice ? `<span class="ind-pill">Entry ${formatPrice(entryPrice)}</span>` : ""}
        ${b.in_position && currentPrice ? `<span class="ind-pill">Current ${formatPrice(currentPrice)}</span>` : ""}
        ${b.in_position && displayStop ? `<span class="ind-pill">Stop ${formatPrice(displayStop)}</span>` : ""}
        ${b.in_position && displayTarget ? `<span class="ind-pill">Target ${formatPrice(displayTarget)}</span>` : ""}
      </div>
      <div class="funds-ctl">
        <span class="funds-ctl-label">Allocated funds</span>
        <button class="btn btn-sm" title="Reduce funds" onclick="adjustBotFunds(${b.id}, -50)">－</button>
        <input type="number" id="funds-${b.id}" class="funds-input" value="${b.funds_allocated}" min="1" step="1">
        <button class="btn btn-sm" title="Give more funds" onclick="adjustBotFunds(${b.id}, 50)">＋</button>
        <button class="btn btn-sm btn-primary" onclick="updateBotFunds(${b.id})">Update</button>
      </div>
      <div style="background:var(--bg2);padding:10px;border-radius:6px;border:1px solid var(--border)">
        <div style="font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.6px;margin-bottom:4px">Last scan ${b.last_analysis_at ? `· ${new Date(b.last_analysis_at+"Z").toLocaleTimeString()}` : "· not yet scanned"}</div>
        <div style="font-size:11px;color:var(--t2)">${esc(b.last_pattern_summary || (b.running ? "Engine is warming up — first scan runs within 60 seconds." : "Bot is paused. Toggle it on to start autonomous scanning."))}</div>
      </div>
    </div>`;
  }).join("");
}

function adjustBotFunds(id, delta) {
  // Quick "give more / reduce" stepper that nudges the input, then persists.
  const el = document.getElementById(`funds-${id}`);
  if (!el) return;
  const next = Math.max(1, Math.round((parseFloat(el.value) || 0) + delta));
  el.value = next;
  updateBotFunds(id);
}

async function updateBotFunds(id) {
  const el = document.getElementById(`funds-${id}`);
  if (!el) return;
  const funds_allocated = parseFloat(el.value);
  if (!funds_allocated || funds_allocated <= 0) return toast("Enter a funds amount greater than zero", "error");
  try {
    const res = await api(`/bots/${id}/funds`, { method: "POST", body: JSON.stringify({ funds_allocated }) });
    toast(`Allocation updated to $${Number(res.funds_allocated).toLocaleString()}`, "success");
    await loadBots();
    // Keep the portfolio highlights/metrics in sync if that tab is mounted.
    const pv = document.getElementById("view-portfolio");
    if (pv && !pv.classList.contains("hidden")) loadPortfolioPerformance();
  } catch (e) { toast(e, "error"); }
}

async function toggleBot(id) {
  try { await api(`/bots/${id}/toggle`, { method: "POST" }); await loadBots(); } catch (e) { toast(e, "error"); }
}
async function deleteBot(id) {
  if (!confirm("Delete this bot? Any open position will be sold first (open orders are cancelled, then the position is liquidated), then the bot is removed.")) return;
  try {
    const res = await api(`/bots/${id}`, { method: "DELETE" });
    const sold = res && res.liquidation && res.liquidation.action === "SELL";
    toast(sold ? "Position sold and bot deleted" : "Bot deleted", "success");
    await refreshUserData();
    await loadBots();
    const pv = document.getElementById("view-portfolio");
    if (pv && !pv.classList.contains("hidden")) loadPortfolioPerformance();
  } catch (e) { toast(e, "error"); }
}

async function sellBot(id) {
  if (!confirm("Sell ALL of this bot's holdings now? This cancels its open orders and liquidates the position at market. The bot itself is kept.")) return;
  try {
    const res = await api(`/bots/${id}/liquidate`, { method: "POST" });
    if (res && res.status === "flat") toast(res.detail || "Nothing to sell — bot is flat", "");
    else toast("Holdings sold — bot is now flat", "success");
    await loadBots();
    const pv = document.getElementById("view-portfolio");
    if (pv && !pv.classList.contains("hidden")) loadPortfolioPerformance();
  } catch (e) { toast(e, "error"); }
}

// runBotCycle removed — bots scan autonomously via the background scheduler.
// The /bots/{id}/run-cycle endpoint still exists for admin/debug use.

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

  const hdr = document.getElementById("markets-header-count");
  if (hdr) hdr.textContent = String(MARKETS.length || 0);

  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="5" style="color:var(--t2)">No matching assets.</td></tr>`;
    return;
  }
  tbody.innerHTML = items.map(m => `
    <tr class="market-row" onclick="openMarketDashboard(${escJs(MARKETS_EXCHANGE)},${escJs(m.symbol)})">
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

// Chart timeframe presets → backend `preset` codes.
// Backend maps these to Alpaca-compatible bar size + dynamic UTC start_date:
//   1D → 1Min / now−1 day
//   1M → 30Min / now−30 days
//   3M → 1Hour / now−90 days
const DASH_PRESETS = ["1D", "1M", "3M"];
const DASH_REFRESH_MS = 15000;
const DASH_CHART_TYPES = ["candles", "line"];

let DASH_STATE = { exchange: null, symbol: null, preset: "1D", chartType: "candles" };
let DASH_CHART = null, DASH_PRIMARY_SERIES = null, DASH_SMA20 = null, DASH_SMA50 = null;
let DASH_FETCH_SEQ = 0;
let DASH_LAST = null; // last dashboard payload — used to switch candles/line without re-fetch

function _setDashFetchStatus(busy, text) {
  const status = document.getElementById("dash-fetch-status");
  if (status) {
    status.textContent = text || "Fetching data…";
    status.classList.toggle("hidden", !busy);
  }
}

function syncDashPresetButtons(preset) {
  document.querySelectorAll("#dash-preset-group .mode-btn").forEach(btn => {
    btn.classList.toggle("active", btn.getAttribute("data-preset") === preset);
  });
}

function syncDashChartTypeSelect(chartType) {
  const sel = document.getElementById("dash-chart-type");
  if (sel && sel.value !== chartType) sel.value = chartType;
}

function stopDashRefreshTimer() {
  if (window._dashRefreshTimer) {
    clearInterval(window._dashRefreshTimer);
    window._dashRefreshTimer = null;
  }
}

function startDashRefreshTimer() {
  stopDashRefreshTimer();
  window._dashRefreshTimer = setInterval(() => {
    const modal = document.getElementById("market-dash-modal");
    if (!modal || modal.classList.contains("hidden")) return;
    if (!DASH_STATE.exchange || !DASH_STATE.symbol) return;
    // Soft refresh re-pulls Alpaca/OKX bars every 15s for BOTH candles and line.
    reloadDashboard({ soft: true });
  }, DASH_REFRESH_MS);
}

async function openMarketDashboard(exchange, symbol) {
  bindDashControls();
  DASH_STATE = { exchange, symbol, preset: "1D", chartType: "candles" };
  DASH_LAST = null;
  document.getElementById("market-dash-modal").classList.remove("hidden");
  document.getElementById("dash-symbol").textContent = symbol;
  document.getElementById("dash-name").textContent = "Loading asset…";
  document.getElementById("dash-price").textContent = "—";
  syncDashPresetButtons("1D");
  syncDashChartTypeSelect("candles");
  startDashRefreshTimer();
  await reloadDashboard();
}

function closeMarketDashboard() {
  document.getElementById("market-dash-modal").classList.add("hidden");
  stopDashRefreshTimer();
  _setDashFetchStatus(false);
  DASH_LAST = null;
  if (DASH_CHART) { try { DASH_CHART.remove(); } catch (_) {} DASH_CHART = null; }
  DASH_PRIMARY_SERIES = DASH_SMA20 = DASH_SMA50 = null;
}

function setDashPreset(preset) {
  try {
    const code = String(preset || "1D").toUpperCase();
    if (!DASH_PRESETS.includes(code)) {
      console.warn("[DASH] unknown preset", preset);
      return;
    }
    console.log("[DASH] setDashPreset", code);
    DASH_STATE.preset = code;
    syncDashPresetButtons(code);
    // Restart the 15s timer so the next tick is relative to this switch, and
    // immediately fetch with the new date range + timeframe.
    startDashRefreshTimer();
    reloadDashboard({ soft: false });
  } catch (e) {
    console.error("[DASH] setDashPreset failed", e);
    try { toast(String(e.message || e), "error"); } catch (_) {}
  }
}

function setDashChartType(type) {
  try {
    const t = String(type || "candles").toLowerCase();
    if (!DASH_CHART_TYPES.includes(t)) {
      console.warn("[DASH] unknown chart type", type);
      return;
    }
    console.log("[DASH] setDashChartType", t, "hasData=", !!DASH_LAST);
    DASH_STATE.chartType = t;
    syncDashChartTypeSelect(t);
    // Re-render immediately from the latest Alpaca/OKX payload; if we don't
    // have data yet, force a fetch so Line/Candles still switches visibly.
    if (DASH_LAST) {
      renderDashChart(DASH_LAST);
    } else {
      reloadDashboard({ soft: false });
    }
  } catch (e) {
    console.error("[DASH] setDashChartType failed", e);
    try { toast(String(e.message || e), "error"); } catch (_) {}
  }
}

// Back-compat alias if anything still calls the old select handler.
function changeDashTimeframe(view) {
  const map = { "1d": "1D", "1mo": "1M", "1h": "3M", "15m": "1D", "1w": "3M", "1y": "3M", "5y": "3M" };
  setDashPreset(map[view] || "1D");
}

// Event-delegation backup so controls work even if inline onclick is blocked
// or a stale cached HTML shell is mixed with a fresh app.js.
function bindDashControls() {
  const modal = document.getElementById("market-dash-modal");
  if (!modal || modal.dataset.dashBound === "1") return;
  modal.dataset.dashBound = "1";
  modal.addEventListener("click", (ev) => {
    const btn = ev.target && ev.target.closest ? ev.target.closest("[data-preset]") : null;
    if (btn && modal.contains(btn)) {
      ev.preventDefault();
      setDashPreset(btn.getAttribute("data-preset"));
    }
  });
  const typeSel = document.getElementById("dash-chart-type");
  if (typeSel) {
    typeSel.addEventListener("change", () => setDashChartType(typeSel.value));
  }
}

// Expose handlers globally for inline HTML onclick/onchange attributes.
window.setDashPreset = setDashPreset;
window.setDashChartType = setDashChartType;
window.changeDashTimeframe = changeDashTimeframe;
window.openMarketDashboard = openMarketDashboard;
window.closeMarketDashboard = closeMarketDashboard;
window.reloadDashboard = reloadDashboard;

try { bindDashControls(); } catch (e) { console.warn("[DASH] bindDashControls", e); }

async function reloadDashboard(opts) {
  const soft = !!(opts && opts.soft);
  const { exchange, symbol, preset } = DASH_STATE;
  if (!exchange || !symbol) return;
  const seq = ++DASH_FETCH_SEQ;
  const msg = document.getElementById("dash-chart-msg");
  _setDashFetchStatus(true, "Fetching data…");
  if (!soft && msg) {
    msg.classList.remove("hidden");
    msg.textContent = "Fetching data…";
  }
  try {
    const url = `/api/markets/${exchange}/${symbol}/dashboard?preset=${encodeURIComponent(preset || "1D")}`;
    console.log("[DASHBOARD] fetching", { exchange, symbol, preset, url, soft });
    const d = await api(url);
    // Ignore stale responses if the user switched presets mid-flight.
    if (seq !== DASH_FETCH_SEQ || DASH_STATE.symbol !== symbol || DASH_STATE.preset !== preset) return;
    renderDashboard(d);
  } catch (e) {
    if (seq !== DASH_FETCH_SEQ) return;
    if (msg) {
      msg.classList.remove("hidden");
      msg.textContent = `⚠ ${e.message}`;
    }
    document.getElementById("dash-bot-status").innerHTML =
      `<span style="color:var(--red)">${esc(e.message)}</span>`;
  } finally {
    if (seq === DASH_FETCH_SEQ) _setDashFetchStatus(false);
  }
}

function renderDashboard(d) {
  DASH_LAST = d;
  document.getElementById("dash-symbol").textContent = d.display_symbol || d.symbol;
  document.getElementById("dash-name").textContent = d.asset_name || "";
  document.getElementById("dash-class-badge").textContent = d.asset_class || d.exchange;
  document.getElementById("dash-price").textContent = formatPrice(d.last_price, d.quote);

  const sub = document.getElementById("dash-price-sub");
  if (sub) {
    if (d.data_as_of) {
      const asOf = new Date(d.data_as_of);
      const ageMin = Math.max(0, Math.round((Date.now() - asOf.getTime()) / 60000));
      const when = asOf.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
      sub.textContent = ageMin <= 1 ? `last bar ${when} (live)` : `last bar ${when} · ${ageMin}m ago`;
    } else {
      sub.textContent = "spot price";
    }
  }

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
  const candles = (d.candles || []).filter(c => c && c.time != null && c.close != null);
  if (!candles.length) {
    msg.classList.remove("hidden");
    msg.textContent = "No candle data available for this asset/timeframe.";
    return;
  }
  msg.classList.add("hidden");

  const chartType = (DASH_STATE.chartType === "line") ? "line" : "candles";

  if (DASH_CHART) { try { DASH_CHART.remove(); } catch (_) {} DASH_CHART = null; }
  DASH_PRIMARY_SERIES = null;
  DASH_CHART = LightweightCharts.createChart(wrap, {
    layout: { background: { color: "#0d0f14" }, textColor: "#8b91a8" },
    grid: { vertLines: { color: "#1a1e28" }, horzLines: { color: "#1a1e28" } },
    rightPriceScale: { borderColor: "#2a2f3d" },
    timeScale: { borderColor: "#2a2f3d", timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0 },
    localization: { timeFormatter: (t) => new Date(t * 1000).toLocaleString() },
    autoSize: true,
  });

  // Both styles use the same Alpaca/OKX OHLCV payload refreshed every 15s.
  // Line chart plots close prices from those bars.
  if (chartType === "line") {
    const linePts = candles.map(c => ({ time: c.time, value: Number(c.close) }));
    DASH_PRIMARY_SERIES = DASH_CHART.addLineSeries({
      color: "#00d68f",
      lineWidth: 2,
      priceLineVisible: true,
      lastValueVisible: true,
    });
    DASH_PRIMARY_SERIES.setData(linePts);
  } else {
    DASH_PRIMARY_SERIES = DASH_CHART.addCandlestickSeries({
      upColor: "#00d68f", downColor: "#ff4d6a", borderVisible: false,
      wickUpColor: "#00d68f", wickDownColor: "#ff4d6a",
    });
    DASH_PRIMARY_SERIES.setData(candles);
  }

  // Overlay moving averages aligned to bar timestamps.
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

  // Mark support / resistance levels on the primary series.
  const lv = d.levels || {};
  if (DASH_PRIMARY_SERIES) {
    if (lv.nearest_support != null)
      DASH_PRIMARY_SERIES.createPriceLine({ price: lv.nearest_support, color: "#00d68f", lineStyle: 2, lineWidth: 1, title: "Support" });
    if (lv.nearest_resistance != null)
      DASH_PRIMARY_SERIES.createPriceLine({ price: lv.nearest_resistance, color: "#ff4d6a", lineStyle: 2, lineWidth: 1, title: "Resistance" });
  }

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
  navigateToTab("bots", { push: true });
  showCreateBotModal();
  setBotMode("manual");
  const t = document.getElementById("b-ticker");
  if (t) t.value = DASH_STATE.symbol;
  const n = document.getElementById("b-name");
  if (n && !n.value) n.value = `${DASH_STATE.symbol} Dip Hunter`;
}

async function loadTradeHistory() {
  const tbody = document.getElementById("history-table-body");
  try {
    const trades = await api("/broker/trades-ledger");
    const hdr = document.getElementById("history-header-count");
    if (hdr) hdr.textContent = String((trades && trades.length) || 0);
    if (!trades.length) { tbody.innerHTML = `<tr><td colspan="7" style="color:var(--t2)">No trades logged yet.</td></tr>`; return; }
    tbody.innerHTML = trades.map(t => {
      const qty = Number(t.qty || 0);
      const qtyStr = qty === 0 ? "0" : qty.toLocaleString(undefined, { maximumFractionDigits: 8 });
      return `
      <tr>
        <td style="color:var(--t2)">${new Date(/Z|[+-]\d{2}:?\d{2}$/.test(t.created_at) ? t.created_at : t.created_at + "Z").toLocaleString()}</td>
        <td style="font-weight:600">${esc(t.bot_name || "—")}</td>
        <td style="font-weight:600">${esc(t.ticker)}</td>
        <td><span class="badge ${t.side === 'buy' ? 'badge-green' : 'badge-red'}">${esc(String(t.side || "—").toUpperCase())}</span></td>
        <td>${qtyStr}</td>
        <td style="font-family:monospace">$${parseFloat(t.price || 0).toFixed(2)}</td>
        <td><span class="badge badge-blue">${esc((t.mode || "paper").toUpperCase())}</span></td>
      </tr>`;
    }).join("");
  } catch (e) { tbody.innerHTML = `<tr><td colspan="7" style="color:var(--red)">Failed to fetch execution records.</td></tr>`; }
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
  const hdr = document.getElementById("news-header-count");
  if (hdr) hdr.textContent = String((ALL_NEWS && ALL_NEWS.length) || 0);
  // Highlight active filter button
  ["all", "bullish", "bearish", "neutral"].forEach(f => {
    const btn = document.getElementById(`news-filter-${f}`);
    if (!btn) return;
    const on = NEWS_FILTER === f;
    btn.style.borderColor = on ? "var(--blue)" : "";
    btn.style.color = on ? "var(--blue)" : "";
  });
  list.innerHTML = filtered.map(n => {
    const link = String(n.link || "");
    const safe = /^https?:\/\//i.test(link) ? link : "";
    const openAttr = safe
      ? `onclick="window.open(${escJs(safe)},'_blank','noopener,noreferrer')"`
      : "";
    return `
    <div class="card" style="cursor:${safe ? "pointer" : "default"}" ${openAttr}>
      <div style="font-weight:600;margin-bottom:6px">${esc(n.title)}</div>
      <div style="font-size:11px;color:var(--t3)">${esc(n.pubDate || "")}</div>
    </div>`;
  }).join("");
}

(async function boot() {
  try {
    // Preserve ?next=… across the auth screen when arriving logged out.
    // Do not clobber an existing deep-link (e.g. /dashboard/bots) with a later next.
    try {
      const next = new URLSearchParams(location.search).get("next");
      if (next) {
        const existing = sessionStorage.getItem("post_login_path");
        if (!existing) sessionStorage.setItem("post_login_path", next);
      }
    } catch (_) {}
    USER = await api("/auth/me");
    if (await redirectAfterAuthIfNeeded()) return;
    await enterApp();
  } catch (_) {
    // Unauthenticated: show login/signup. Preserve deep-linked dashboard paths
    // so post-login restore works; AdSense stays off the public landing `/`.
    const path = normalizePath(location.pathname);
    if (isDashboardPath(path) || path === "/admin" || path === "/checkout/success" || path === "/upgrade-plans") {
      try {
        const full = path + (location.search || "");
        if (!sessionStorage.getItem("post_login_path")) {
          sessionStorage.setItem("post_login_path", full);
        }
      } catch (_) {}
      history.replaceState({ auth: "login" }, "", "/login");
      toggleAuthMode(true);
    } else {
      toggleAuthMode(path !== "/signup");
    }
  }
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
        robotIcon.src = '/static/images/show-password.png';
    } else {
        passwordInput.type = 'password';
        robotIcon.src = '/static/images/hide-password.png';
    }
}
