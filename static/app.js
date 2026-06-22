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
        // Check if user is logged in by fetching account data
        await api("/account");
        setAppState(true);
        updateDashboard();
    } catch (err) {
        setAppState(false); // User is not logged in
    }
});

// --- 5. SIGNUP LOGIC ---
async function doSignup() {
    const pass = document.getElementById('su-pass').value;
    const confirmPass = document.getElementById('su-pass-confirm').value;
    const errorDiv = document.getElementById('password-error');

    // THIS IS THE FEEDBACK PART
    if (pass !== confirmPass) {
        errorDiv.textContent = "Passwords do not match!"; // This shows the text
        errorDiv.style.display = "block";                // This ensures it is visible
        return; 
    }
    
    // Clear the message if they match
    errorDiv.textContent = ""; 
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
    }
}
