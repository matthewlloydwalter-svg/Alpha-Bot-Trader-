async function loadAdminData() {
    try {
        // 1. Fetch and render Stats
        const statsRes = await fetch('/admin/stats');
        if (!statsRes.ok) throw new Error('Failed to fetch stats');
        const stats = await statsRes.json();

        const statsContainer = document.getElementById('stats-content');
        if (statsContainer) {
            statsContainer.innerHTML = `
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
                    <div><strong>Total Users:</strong> ${stats.total_users}</div>
                    <div><strong>Verified Users:</strong> ${stats.verified_users}</div>
                    <div><strong>Total Deposited:</strong> $${stats.total_deposited?.toLocaleString() || 0}</div>
                    <div><strong>Total Withdrawn:</strong> $${stats.total_withdrawn?.toLocaleString() || 0}</div>
                    <div><strong>Total Bots:</strong> ${stats.total_bots}</div>
                    <div><strong>Total Trades:</strong> ${stats.total_trades}</div>
                </div>
            `;
        }

        // 2. Fetch and render Users
        const usersRes = await fetch('/admin/users');
        if (!usersRes.ok) throw new Error('Failed to fetch users');
        const users = await usersRes.json();

        const listContainer = document.getElementById('users-list');
        if (listContainer) {
            listContainer.innerHTML = `
                <table style="width: 100%; border-collapse: collapse; color: white;">
                    <thead>
                        <tr style="border-bottom: 1px solid #444;">
                            <th>Email</th>
                            <th>Deposited</th>
                            <th>Profit</th>
                            <th>Bots</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${users.map(u => `
                            <tr style="border-bottom: 1px solid #333;">
                                <td>${u.email}</td>
                                <td>$${u.total_deposited?.toLocaleString() || 0}</td>
                                <td>$${u.estimated_profit} (${u.estimated_profit_pct}%)</td>
                                <td>${u.bot_count}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            `;
        }
    } catch (error) {
        console.error('Error loading admin data:', error);
        const statsContainer = document.getElementById('stats-content');
        if (statsContainer) statsContainer.innerText = "Error loading data.";
    }
}

document.addEventListener('DOMContentLoaded', loadAdminData);
