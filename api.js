// Mi Cineteca — capa de red. Token de sesión y wrapper de fetch.

function _getToken() {
  return _currentSession?.access_token ?? null;
}

async function api(path, options) {
  const token = _getToken();
  const headers = { ...(options?.headers) };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (options?.body && typeof options.body === "string") headers["Content-Type"] = headers["Content-Type"] || "application/json";
  const res = await fetch(path, { ...options, headers });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, data };
}
