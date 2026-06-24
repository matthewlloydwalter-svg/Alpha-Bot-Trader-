async function loadAdminData() {
    try {
        const statsRes = await fetch('/admin/stats');
        if (!statsRes.ok) throw new Error('Failed to retrieve system status.');
        const stats = await statsRes.json();

        const statsContainer = document.getElementById('stats-content');
        if (statsContainer) {
            statsContainer.innerHTML = `
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
                    <div><strong>Registered Users:</strong> ${stats.total_users}</div>
                    <div><strong>Verified Enforcements:</strong> ${stats.verified_users}</div>
                    <div><strong>Total Virtual Allocation:</strong> $${(stats.total_deposited || 0).toLocaleString()}</div>
                    <div><strong>Active Orchestrated Brains:</strong> ${stats.total_bots}</div>
                    <div><strong>Total Ledger Cycles:</strong> ${stats.total_trades}</div>
                </div>
            `;
        }

        const usersRes = await fetch('/admin/users');
        if (!usersRes.ok) throw new Error('Failed to retrieve active database rows.');
        const users = await usersRes.json();

        const listContainer = document.getElementById('users-list');
        if (listContainer) {
            listContainer.innerHTML = `
                <table style="width: 100%; border-collapse: collapse;">
                    <thead>
                        <tr style="border-bottom: 1px solid var(--border); text-align: left; color: var(--t2);">
                            <th style="padding: 8px;">User Identifier</th>
                            <th style="padding: 8px;">Clearance Level</th>
                            <th style="padding: 8px;">Verified State</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${users.map(u => `
                            <tr style="border-bottom: 1px solid var(--border);">
                                <td style="padding: 8px; font-weight:600;">${u.email}</td>
                                <td style="padding: 8px;">${u.is_admin ? 'Platform Admin' : 'Standard Account'}</td>
                                <td style="padding: 8px; color: ${u.email_verified ? 'var(--green)' : 'var(--amber)'}">
                                    ${u.email_verified ? 'Verified' : 'Pending Verification'}
                                </td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            `;
        }
    } catch (error) {
        console.error('Telepathy read execution fault:', error);
        const sc = document.getElementById('stats-content');
        if (sc) sc.innerText = "Execution error accessing remote endpoint data streams.";
    }
}

document.addEventListener('DOMContentLoaded', loadAdminData);
