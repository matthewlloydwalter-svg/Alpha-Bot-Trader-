// --- AUTHENTICATION ---
async function login(email, password) {
  const resp = await fetch("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ email, password }),
  });
  if (!resp.ok) throw new Error((await resp.json()).detail);
  return resp.json();
}

async function getMe() {
  const resp = await fetch("/auth/me", { credentials: "include" });
  if (!resp.ok) return null;
  return resp.json();
}

// --- DATA FETCHING (The "Real" Data) ---
// Note: You must replace 'YOUR_BACKEND_API_TOKEN' with the value from your .env
const API_TOKEN = "YOUR_BACKEND_API_TOKEN"; 

async function fetchData(endpoint) {
  const resp = await fetch(endpoint, {
    method: "GET",
    headers: { "x-api-token": API_TOKEN }
  });
  if (!resp.ok) throw new Error("Failed to fetch data");
  return resp.json();
}

async function updateDashboard() {
  try {
    // Fetch account and positions
    const [account, positions] = await Promise.all([
      fetchData("/account"),
      fetchData("/positions")
    ]);

    // Update HTML elements (IDs must match index.html)
    document.getElementById('pf-bal').innerText = '$' + parseFloat(account.portfolio_value).toLocaleString(undefined, {minimumFractionDigits: 2});
    document.getElementById('pf-free').innerText = '$' + parseFloat(account.cash).toLocaleString(undefined, {minimumFractionDigits: 2});
    
    // Update Positions List
    const posList = document.getElementById('pf-holdings');
    posList.innerHTML = positions.map(p => `
      <div class="stk-row">
        <span>${p.symbol}</span>
        <span style="margin-left:auto">${p.qty} shares</span>
      </div>
    `).join('');

  } catch (err) {
    console.error("Dashboard update failed:", err);
  }
}

// Initialize on load
window.addEventListener('DOMContentLoaded', () => {
    updateDashboard();
});
