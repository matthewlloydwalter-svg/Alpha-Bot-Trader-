// app.js — example of how your real frontend should call the backend.
// Cookies are sent automatically by the browser once logged in, as
// long as fetch calls include `credentials: "include"`.

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
