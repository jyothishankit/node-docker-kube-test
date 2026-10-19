const BASE = '/api/items';

// Remembers which backend pod answered the most recent request.
export let lastServedBy = null;

async function request(path, options = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  lastServedBy = res.headers.get('X-Served-By');
  if (res.status === 204) return null;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `Request failed (${res.status})`);
  return body;
}

export const listItems = () => request('');
export const createItem = (item) => request('', { method: 'POST', body: JSON.stringify(item) });
export const updateItem = (id, item) => request(`/${id}`, { method: 'PUT', body: JSON.stringify(item) });
export const deleteItem = (id) => request(`/${id}`, { method: 'DELETE' });
