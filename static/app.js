// --- 1. STATE MANAGER ---
function setAppState(isLoggedIn) {
    const authScreen = document.getElementById('auth-screen');
    const mainApp = document.getElementById('main-app');

    if (isLoggedIn) {
        authScreen.classList.add('hidden');
        mainApp.classList.remove('hidden');
    } else {
        authScreen.classList.remove('hidden');
        mainApp.classList.add('hidden');
    }
}

// --- 2. API HELPER ---
async function api(path, options = {}) {
    const resp = await fetch(path, {
        ...options,
        headers: { "Content-Type": "application/json", ...options.headers },
        credentials: "include"
    });
    
    const data = await resp.json().catch(() => ({}));
    
    if (!resp.ok) {
        throw new Error(data.detail || `Error ${resp.status}`);
    }
    return data;
}

// --- 3. DASHBOARD LOGIC ---
async function updateDashboard() {
    try {
        const [account, positions] = await Promise.all([
            api("/account"),
            api("/positions")
        ]);

        document.getElementById('pf-bal').innerText = '$' + 
            parseFloat(account.portfolio_value || 0).toLocaleString(undefined, {minimumFractionDigits: 2});
            
        document.getElementById('pf-free').innerText = '$' + 
            parseFloat(account.cash || 0).toLocaleString(undefined, {minimumFractionDigits: 2});
        
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

// --- 4. INITIALIZATION ---
window.addEventListener('DOMContentLoaded', async () => {
    try {
        await api("/account");
        setAppState(true);
        updateDashboard();
    } catch (err) {
        setAppState(false);
    }
});

// --- 5. SIGNUP LOGIC ---
async function doSignup() {
    const name = document.getElementById('su-name').value;
    const email = document.getElementById('su-email').value;
    const pass = document.getElementById('su-pass').value;
    const confirmPass = document.getElementById('su-pass-confirm').value;
    const errorDiv = document.getElementById('password-error');

    if (pass !== confirmPass) {
        errorDiv.textContent = "Passwords do not match!";
        errorDiv.style.setProperty('display', 'block', 'important'); 
        return; 
    } else {
        errorDiv.style.setProperty('display', 'none', 'important');
    }
    
    errorDiv.textContent = ""; 
    
    try {
        const data = await api('/signup', {
            method: 'POST',
            body: JSON.stringify({ name, email, password: pass })
        });

        if (data.status === 'success') {
            alert("Signup successful! You can now sign in.");
            showAuthTab('login');
        }
    } catch (err) {
        errorDiv.textContent = err.message;
        errorDiv.style.setProperty('display', 'block', 'important');
    }
}

// Add this to your app.js file
async function doLogin() {
    const email = document.getElementById('login-email').value;
    const password = document.getElementById('login-pass').value;

    try {
        const data = await api('/login', {
            method: 'POST',
            body: JSON.stringify({ email, password })
        });

        if (data.status === 'success') {
            alert("Login successful!");
            setAppState(true); // Switches to the main app view
            updateDashboard(); // Refreshes your portfolio/account data
        }
    } catch (err) {
        alert("Login failed: " + err.message);
    }
}
