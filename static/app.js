// This function acts as the gatekeeper for your app
function setAppState(isLoggedIn) {
    const authScreen = document.getElementById('auth-screen');
    const mainApp = document.getElementById('main-app');

    if (isLoggedIn) {
        authScreen.classList.add('hidden');  // Hide auth
        mainApp.classList.remove('hidden');  // Show app
    } else {
        authScreen.classList.remove('hidden'); // Show auth
        mainApp.classList.add('hidden');       // Hide app
    }
}

// Ensure the app starts in the correct state
window.onload = function() {
    // By default, assume user is not logged in until they prove it
    setAppState(false); 
};
/**
 * Unified API helper: Uses session cookies exclusively.
 * No API tokens needed in the frontend code.
 */
async function api(path, options = {}) {
    const resp = await fetch(path, {
        ...options,
        headers: { "Content-Type": "application/json", ...options.headers },
        credentials: "include" // Automatically handles your session
    });
    
    // Handle empty responses
    const data = await resp.json().catch(() => ({}));
    
    if (!resp.ok) {
        throw new Error(data.detail || `Error ${resp.status}`);
    }
    return data;
}

// --- DASHBOARD UPDATER ---
async function updateDashboard() {
    try {
        // Fetch account and positions using the unified helper
        const [account, positions] = await Promise.all([
            api("/account"),
            api("/positions")
        ]);

        // Update Balance
        document.getElementById('pf-bal').innerText = '$' + 
            parseFloat(account.portfolio_value || 0).toLocaleString(undefined, {minimumFractionDigits: 2});
            
        document.getElementById('pf-free').innerText = '$' + 
            parseFloat(account.cash || 0).toLocaleString(undefined, {minimumFractionDigits: 2});
        
        // Update Positions List
        const posList = document.getElementById('pf-holdings');
        posList.innerHTML = positions.length > 0 
            ? positions.map(p => `
                <div class="stk-row">
                    <span>${p.symbol}</span>
                    <span style="margin-left:auto">${p.qty} shares</span>
                </div>
            `).join('')
            : '<div style="color:var(--t2);padding:10px">No positions</div>';

    } catch (err) {
        console.error("Dashboard update failed:", err.message);
    }
}

// Initialize
window.addEventListener('DOMContentLoaded', updateDashboard);
function doSignup() {
    const pass = document.getElementById('su-pass').value;
    const confirmPass = document.getElementById('su-pass-confirm').value;
    const errorDiv = document.getElementById('password-error');

    // Check if they match
    if (pass !== confirmPass) {
        errorDiv.textContent = "Passwords do not match!";
        return; // Stop the function here
    }

    // Clear error if they match
    errorDiv.textContent = "";

    // Proceed with your existing signup logic...
    // e.g., fetch('/signup', { method: 'POST', body: JSON.stringify({...}) })
}
