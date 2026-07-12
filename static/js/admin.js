async function loadAdminData() {
    try {
        const statsRes = await fetch('/admin/stats', { credentials: "include" });
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

        const usersRes = await fetch('/admin/users', { credentials: "include" });
        if (!usersRes.ok) throw new Error('Failed to retrieve active database rows.');
        const users = await usersRes.json();

        const esc = (str) => String(str == null ? "" : str)
            .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");

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
                                <td style="padding: 8px; font-weight:600;">${esc(u.email)}</td>
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
        const msg = "Execution error accessing remote endpoint data streams.";
        const sc = document.getElementById('stats-content');
        if (sc) sc.innerText = msg;
        const ul = document.getElementById('users-list');
        if (ul) ul.innerText = msg;
    }
}

/* ──────────────────────────────────────────────────────────────────
 * AI Code Assistant (admin only, human-in-the-loop)
 * ────────────────────────────────────────────────────────────────── */
let AI_PROPOSAL_ID = null;

function aiEsc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

async function aiApi(path, options = {}) {
  const resp = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let data = null;
  try { data = await resp.json(); } catch (_) {}
  if (!resp.ok) throw new Error((data && data.detail) ? data.detail : `Error (${resp.status})`);
  return data;
}

async function loadAiStatus() {
  const el = document.getElementById('ai-provider-status');
  if (!el) return;
  try {
    const s = await aiApi('/admin/ai/status');
    if (s.configured) { el.className = 'badge badge-green'; el.textContent = `Provider: ${s.provider}`; }
    else { el.className = 'badge badge-red'; el.textContent = 'No LLM key configured'; }
  } catch (e) {
    el.className = 'badge badge-red'; el.textContent = 'Status unavailable';
  }
}

function renderDiff(diffText) {
  // Colorize a unified diff for readability.
  return aiEsc(diffText).split('\n').map(line => {
    let color = 'var(--t2)';
    if (line.startsWith('+') && !line.startsWith('+++')) color = 'var(--green)';
    else if (line.startsWith('-') && !line.startsWith('---')) color = 'var(--red)';
    else if (line.startsWith('@@')) color = 'var(--blue)';
    return `<span style="color:${color}">${line || ' '}</span>`;
  }).join('\n');
}

async function runAiAudit() {
  const btn = document.getElementById('ai-audit-btn');
  const out = document.getElementById('ai-output');
  const proposalBox = document.getElementById('ai-proposal');
  const prompt = (document.getElementById('ai-prompt').value || '').trim();
  if (!prompt) { out.innerHTML = '<span style="color:var(--amber)">Enter a prompt first.</span>'; return; }

  AI_PROPOSAL_ID = null;
  proposalBox.classList.add('hidden');
  btn.disabled = true; btn.textContent = 'Analyzing…';
  out.innerHTML = '<span style="color:var(--t2)">⏳ The assistant is scanning the codebase…</span>';

  try {
    const res = await aiApi('/admin/ai/audit', { method: 'POST', body: JSON.stringify({ prompt }) });

    let html = '';
    if (res.summary) html += `<div style="margin-bottom:10px"><strong>Summary:</strong> ${aiEsc(res.summary)}</div>`;
    if (res.findings && res.findings.length) {
      html += '<div style="margin-bottom:6px"><strong>Findings:</strong></div><ul style="margin:0 0 10px 18px">';
      html += res.findings.map(f => `<li style="margin-bottom:4px">${aiEsc(f)}</li>`).join('');
      html += '</ul>';
    }
    if (res.files_in_context && res.files_in_context.length) {
      html += `<div style="font-size:11px;color:var(--t3)">Reviewed: ${res.files_in_context.map(aiEsc).join(', ')}</div>`;
    }
    if (res.raw) html += `<pre style="white-space:pre-wrap;background:var(--bg2);padding:10px;border-radius:6px;margin-top:8px">${aiEsc(res.raw)}</pre>`;
    out.innerHTML = html || '<span style="color:var(--t2)">No response.</span>';

    if (res.proposal_id && res.diffs && res.diffs.length) {
      AI_PROPOSAL_ID = res.proposal_id;
      const diffsEl = document.getElementById('ai-diffs');
      diffsEl.innerHTML = res.diffs.map(d => {
        if (d.error) {
          return `<div style="margin-bottom:12px"><div style="font-weight:600">${aiEsc(d.path)}</div>
            <div style="color:var(--red);font-size:12px">Rejected: ${aiEsc(d.error)}</div></div>`;
        }
        const tag = d.is_new ? ' <span class="badge badge-green">new file</span>' : '';
        return `<div style="margin-bottom:12px">
          <div style="font-weight:600;margin-bottom:4px">${aiEsc(d.path)}${tag}</div>
          <pre style="white-space:pre;overflow:auto;max-height:320px;background:var(--bg2);padding:10px;border-radius:6px;font-size:12px;border:1px solid var(--border)">${renderDiff(d.diff || '(no diff)')}</pre>
        </div>`;
      }).join('');
      document.getElementById('ai-proposal').classList.remove('hidden');
    }
  } catch (e) {
    out.innerHTML = `<span style="color:var(--red)">⚠ ${aiEsc(e.message)}</span>`;
  } finally {
    btn.disabled = false; btn.textContent = '🔍 Audit / Propose Changes';
  }
}

async function approveAiProposal() {
  if (!AI_PROPOSAL_ID) return;
  if (!confirm('Apply these changes to the server files? This will write to disk.')) return;
  const btn = document.getElementById('ai-approve-btn');
  btn.disabled = true; btn.textContent = 'Applying…';
  try {
    const res = await aiApi('/admin/ai/approve', { method: 'POST', body: JSON.stringify({ proposal_id: AI_PROPOSAL_ID }) });
    document.getElementById('ai-output').innerHTML =
      `<span style="color:var(--green)">✅ Applied ${res.count} file(s): ${(res.written || []).map(aiEsc).join(', ')}. Restart/redeploy to load changes.</span>`;
    document.getElementById('ai-proposal').classList.add('hidden');
    AI_PROPOSAL_ID = null;
  } catch (e) {
    document.getElementById('ai-output').innerHTML = `<span style="color:var(--red)">⚠ ${aiEsc(e.message)}</span>`;
  } finally {
    btn.disabled = false; btn.textContent = '✅ Approve & Apply';
  }
}

async function denyAiProposal() {
  if (!AI_PROPOSAL_ID) return;
  try { await aiApi('/admin/ai/deny', { method: 'POST', body: JSON.stringify({ proposal_id: AI_PROPOSAL_ID }) }); } catch (_) {}
  document.getElementById('ai-proposal').classList.add('hidden');
  document.getElementById('ai-output').innerHTML = '<span style="color:var(--t2)">Proposal denied — no files were changed.</span>';
  AI_PROPOSAL_ID = null;
}

document.addEventListener('DOMContentLoaded', () => {
  loadAdminData();
  loadAiStatus();
});
