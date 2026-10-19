const express = require('express');
const mongoose = require('mongoose');
const os = require('os');

const PORT = Number(process.env.PORT) || 5000;
const MONGO_URI = process.env.MONGO_URI || 'mongodb://localhost:27017/crud';
const HOSTNAME = os.hostname();
// Hard cap on stored items keeps the database tiny (~1-2 MB at most).
const MAX_ITEMS = Number(process.env.MAX_ITEMS) || 500;
// Small pool: every MongoDB connection costs ~1 MB of memory on the server,
// and with autoscaling the pools multiply by the replica count.
const MONGO_POOL_SIZE = Number(process.env.MONGO_POOL_SIZE) || 5;

// ---------- Model ----------
const itemSchema = new mongoose.Schema(
  {
    name: { type: String, required: true, trim: true, maxlength: 200 },
    description: { type: String, default: '', maxlength: 500 },
    quantity: { type: Number, default: 0, min: 0 },
  },
  { timestamps: true }
);
// The list endpoint sorts by newest first; without this index MongoDB sorts
// the whole collection in memory on every request.
itemSchema.index({ createdAt: -1 });
const Item = mongoose.model('Item', itemSchema);

// ---------- App ----------
const app = express();
app.use(express.json({ limit: '100kb' }));

// Tag every response with the pod that served it, so the UI and the load test
// can see requests being spread across replicas.
app.use((req, res, next) => {
  res.set('X-Served-By', HOSTNAME);
  next();
});

// Liveness: the process is up.
app.get('/health', (req, res) => res.json({ status: 'ok', host: HOSTNAME }));

// Readiness: only take traffic once MongoDB is connected.
app.get('/ready', (req, res) => {
  const ready = mongoose.connection.readyState === 1;
  res.status(ready ? 200 : 503).json({ ready, host: HOSTNAME });
});

const router = express.Router();

router.get('/', async (req, res, next) => {
  try {
    const limit = Math.min(Number(req.query.limit) || 50, 200);
    const items = await Item.find().sort({ createdAt: -1 }).limit(limit).lean();
    res.json(items);
  } catch (err) {
    next(err);
  }
});

router.get('/:id', async (req, res, next) => {
  try {
    const item = await Item.findById(req.params.id).lean();
    if (!item) return res.status(404).json({ error: 'Item not found' });
    res.json(item);
  } catch (err) {
    next(err);
  }
});

router.post('/', async (req, res, next) => {
  try {
    // estimatedDocumentCount reads collection metadata, so it stays cheap.
    if ((await Item.estimatedDocumentCount()) >= MAX_ITEMS) {
      return res.status(507).json({ error: `Item limit (${MAX_ITEMS}) reached; delete some first` });
    }
    const { name, description, quantity } = req.body;
    const item = await Item.create({ name, description, quantity });
    res.status(201).json(item);
  } catch (err) {
    next(err);
  }
});

router.put('/:id', async (req, res, next) => {
  try {
    const update = {};
    for (const key of ['name', 'description', 'quantity']) {
      if (req.body[key] !== undefined) update[key] = req.body[key];
    }
    const item = await Item.findByIdAndUpdate(req.params.id, update, {
      new: true,
      runValidators: true,
    }).lean();
    if (!item) return res.status(404).json({ error: 'Item not found' });
    res.json(item);
  } catch (err) {
    next(err);
  }
});

router.delete('/:id', async (req, res, next) => {
  try {
    const item = await Item.findByIdAndDelete(req.params.id).lean();
    if (!item) return res.status(404).json({ error: 'Item not found' });
    res.status(204).end();
  } catch (err) {
    next(err);
  }
});

app.use('/api/items', router);

app.use((req, res) => res.status(404).json({ error: 'Not found' }));

// eslint-disable-next-line no-unused-vars
app.use((err, req, res, next) => {
  if (err.name === 'ValidationError' || err.name === 'CastError') {
    return res.status(400).json({ error: err.message });
  }
  if (err.type === 'entity.parse.failed') {
    return res.status(400).json({ error: 'Invalid JSON body' });
  }
  console.error(err);
  res.status(500).json({ error: 'Internal server error' });
});

// ---------- Startup / shutdown ----------
async function connectWithRetry() {
  for (let attempt = 1; ; attempt++) {
    try {
      await mongoose.connect(MONGO_URI, { maxPoolSize: MONGO_POOL_SIZE, serverSelectionTimeoutMS: 5000 });
      console.log(`[${HOSTNAME}] connected to MongoDB`);
      return;
    } catch (err) {
      const delay = Math.min(1000 * attempt, 10000);
      console.error(`[${HOSTNAME}] MongoDB connection failed (attempt ${attempt}): ${err.message}; retrying in ${delay}ms`);
      await new Promise((r) => setTimeout(r, delay));
    }
  }
}

const server = app.listen(PORT, () => console.log(`[${HOSTNAME}] API listening on :${PORT}`));
connectWithRetry();

function shutdown(signal) {
  console.log(`[${HOSTNAME}] ${signal} received, shutting down`);
  server.close(async () => {
    await mongoose.disconnect();
    process.exit(0);
  });
  setTimeout(() => process.exit(1), 10000).unref();
}
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
