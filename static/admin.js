JavaScript

async function loadAdminData() {
    try {
        // 1. Fetch System Stats
        const statsResponse = await fetch('/admin/stats');
        if (!statsResponse.ok) throw new Error('Failed to fetch stats');
        const stats = await statsResponse.json();

        // Update the stats section
        const statsContent = document.getElementById('stats-content');
        // If stats is an object, we display it as a pretty string
        statsContent.innerHTML = `<pre>${JSON.stringify(stats, null, 2)}</pre>`;

        // 2. Fetch User List
        const usersResponse = await fetch('/admin/users');
        if (!usersResponse.ok) throw new Error('Failed to fetch users');
        const users = await usersResponse.json();

        // Update the users list
        const usersList = document.getElementById('users-list');
        if (users.length > 0) {
            usersList.innerHTML = users.map(user => `<li>${user.email}</li>`).join('');
        } else {
            usersList.innerHTML = '<li>No users found.</li>';
        }

    } catch (error) {
        console.error("Error loading admin data:", error);
        document.getElementById('stats-content').innerText = "Access Denied or Server Error.";
        document.getElementById('users-list').innerHTML = '';
    }
}

// Run this function immediately when the page finishes loading
document.addEventListener('DOMContentLoaded', loadAdminData);
