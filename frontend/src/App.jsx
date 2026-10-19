import { useEffect, useMemo, useState } from 'react';
import * as api from './api.js';
import { Icon } from './Icon.jsx';

const EMPTY = { name: '', description: '', quantity: 0 };

export default function App() {
  const [items, setItems] = useState([]);
  const [form, setForm] = useState(EMPTY);
  const [editingId, setEditingId] = useState(null);
  const [error, setError] = useState('');
  const [servedBy, setServedBy] = useState('');
  const [podsSeen, setPodsSeen] = useState(() => new Set());
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState('');

  async function run(action) {
    setError('');
    setLoading(true);
    try {
      return await action();
    } catch (err) {
      setError(err.message);
    } finally {
      const pod = api.lastServedBy;
      if (pod) {
        setServedBy(pod);
        setPodsSeen((prev) => (prev.has(pod) ? prev : new Set(prev).add(pod)));
      }
      setLoading(false);
    }
  }

  const refresh = () => run(async () => setItems(await api.listItems()));

  useEffect(() => {
    refresh();
  }, []);

  async function handleSubmit(e) {
    e.preventDefault();
    const payload = { ...form, quantity: Number(form.quantity) || 0 };
    await run(async () => {
      if (editingId) {
        const updated = await api.updateItem(editingId, payload);
        setItems((prev) => prev.map((it) => (it._id === editingId ? updated : it)));
      } else {
        const created = await api.createItem(payload);
        setItems((prev) => [created, ...prev]);
      }
      setForm(EMPTY);
      setEditingId(null);
    });
  }

  function startEdit(item) {
    setEditingId(item._id);
    setForm({ name: item.name, description: item.description, quantity: item.quantity });
  }

  function cancelEdit() {
    setEditingId(null);
    setForm(EMPTY);
  }

  async function handleDelete(id) {
    await run(async () => {
      await api.deleteItem(id);
      setItems((prev) => prev.filter((it) => it._id !== id));
      if (editingId === id) cancelEdit();
    });
  }

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter(
      (it) => it.name.toLowerCase().includes(q) || (it.description || '').toLowerCase().includes(q)
    );
  }, [items, query]);

  const totalQty = items.reduce((sum, it) => sum + (it.quantity || 0), 0);

  return (
    <div className="page">
      <header className="topbar">
        <div className="brand">
          <span className="logo" aria-hidden="true">
            <Icon name="box" />
          </span>
          <div>
            <h1>Inventory</h1>
            <p className="tagline">Node.js · MongoDB · React on Kubernetes</p>
          </div>
        </div>
        <div className="topbar-actions">
          {servedBy && (
            <span className="pod" title="Backend pod that answered the last request">
              <span className="pulse" aria-hidden="true" />
              <code>{servedBy}</code>
            </span>
          )}
          <button className="icon-btn" onClick={refresh} disabled={loading} aria-label="Refresh">
            <Icon name="refresh" className={loading ? 'spin' : ''} />
          </button>
        </div>
      </header>

      <section className="stats" aria-label="Summary">
        <Stat label="Items" value={items.length} icon="list" />
        <Stat label="Total quantity" value={totalQty.toLocaleString()} icon="stack" />
        <Stat
          label="Backend pods seen"
          value={podsSeen.size}
          icon="server"
          hint="Distinct pods that answered this browser — grows as the HPA scales out"
        />
      </section>

      {error && (
        <div className="alert" role="alert">
          <Icon name="alert" />
          <span>{error}</span>
          <button className="icon-btn ghost" onClick={() => setError('')} aria-label="Dismiss">
            <Icon name="x" />
          </button>
        </div>
      )}

      <main className="layout">
        <form onSubmit={handleSubmit} className="card form">
          <h2>{editingId ? 'Edit item' : 'Add an item'}</h2>
          <label>
            <span>Name</span>
            <input
              placeholder="e.g. Raspberry Pi 5"
              value={form.name}
              maxLength={200}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              required
            />
          </label>
          <label>
            <span>Description</span>
            <textarea
              placeholder="Optional notes"
              rows={3}
              maxLength={500}
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
            />
          </label>
          <label>
            <span>Quantity</span>
            <input
              type="number"
              min="0"
              value={form.quantity}
              onChange={(e) => setForm({ ...form, quantity: e.target.value })}
            />
          </label>
          <div className="actions">
            <button type="submit" className="btn primary" disabled={loading}>
              <Icon name={editingId ? 'check' : 'plus'} />
              {editingId ? 'Save changes' : 'Add item'}
            </button>
            {editingId && (
              <button type="button" className="btn" onClick={cancelEdit}>
                Cancel
              </button>
            )}
          </div>
        </form>

        <section className="card list">
          <div className="list-head">
            <h2>Items</h2>
            <label className="search">
              <Icon name="search" />
              <input
                type="search"
                placeholder="Search"
                aria-label="Search items"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
          </div>

          {visible.length === 0 ? (
            <div className="empty">
              <Icon name="box" />
              <p>{items.length === 0 ? 'No items yet — add your first one.' : 'No items match your search.'}</p>
            </div>
          ) : (
            <ul className="rows">
              {visible.map((item) => (
                <li key={item._id} className={item._id === editingId ? 'row editing' : 'row'}>
                  <div className="row-main">
                    <span className="row-name">{item.name}</span>
                    {item.description && <span className="row-desc">{item.description}</span>}
                  </div>
                  <span className="qty" title="Quantity">
                    {item.quantity}
                  </span>
                  <div className="row-actions">
                    <button className="icon-btn ghost" onClick={() => startEdit(item)} aria-label={`Edit ${item.name}`}>
                      <Icon name="edit" />
                    </button>
                    <button
                      className="icon-btn ghost danger"
                      onClick={() => handleDelete(item._id)}
                      aria-label={`Delete ${item.name}`}
                    >
                      <Icon name="trash" />
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </main>

      <footer className="footer">
        Every API response carries an <code>X-Served-By</code> header — watch it change as Kubernetes scales the
        backend.
      </footer>
    </div>
  );
}

function Stat({ label, value, icon, hint }) {
  return (
    <div className="stat" title={hint}>
      <span className="stat-icon" aria-hidden="true">
        <Icon name={icon} />
      </span>
      <div>
        <div className="stat-value">{value}</div>
        <div className="stat-label">{label}</div>
      </div>
    </div>
  );
}
