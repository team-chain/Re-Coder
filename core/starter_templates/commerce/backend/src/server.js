import express from 'express';
import cors from 'cors';
import dotenv from 'dotenv';
import pool from './db.js';
import authRoutes from './routes/auth.js';
import productsRoutes from './routes/products.js';
import cartRoutes from './routes/cart.js';
import ordersRoutes from './routes/orders.js';
import adminRoutes from './routes/admin.js';
import webhooksRoutes from './routes/webhooks.js';
import { startOrderExpiry } from './order_expiry.js';

dotenv.config();

const app = express();
const PORT = process.env.PORT || 3001;

// Validate required environment variables
if (!process.env.JWT_SECRET || process.env.JWT_SECRET.length < 32 || (process.env.NODE_ENV === 'production' && /your_|change_this|example|local-disposable/i.test(process.env.JWT_SECRET))) {
  console.error('ERROR: JWT_SECRET must be set and at least 32 characters long');
  process.exit(1);
}

if (!process.env.STRIPE_SECRET_KEY) {
  console.error('ERROR: STRIPE_SECRET_KEY must be set');
  process.exit(1);
}

if (!process.env.STRIPE_WEBHOOK_SECRET) {
  console.error('ERROR: STRIPE_WEBHOOK_SECRET must be set');
  process.exit(1);
}

// CORS configuration: allowlist origin only
const corsOptions = (req, done) => done(null, {
  origin: (origin, callback) => {
    const allowedOrigins = (process.env.FRONTEND_URL || '').split(',').map(o => o.trim());
    const sameOrigin = `${req.protocol}://${req.get('host')}`;
    if (!origin || origin === sameOrigin || allowedOrigins.includes(origin)) {
      callback(null, true);
    } else {
      callback(Object.assign(new Error('Not allowed by CORS'), { status: 403 }));
    }
  },
  credentials: true,
  methods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
  allowedHeaders: ['Content-Type', 'Authorization']
});

app.use(cors(corsOptions));

// Security headers
app.use((req, res, next) => {
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('Referrer-Policy', 'strict-origin-when-cross-origin');
  res.setHeader('X-Frame-Options', 'DENY');
  res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self' https://js.stripe.com; frame-src 'self' https://js.stripe.com; connect-src 'self' https://api.stripe.com; img-src 'self' https://placehold.co data:; style-src 'self' 'unsafe-inline'");
  res.removeHeader('X-Powered-By');
  next();
});

// Body size limit


// Webhook route must be before JSON parser (raw body)
app.use('/api/webhooks', webhooksRoutes);
app.use(express.json({ limit: '100kb' }));

// Health check endpoint with DB verification
app.get('/health', async (req, res) => {
  try {
    const client = await pool.connect();
    try {
      await client.query('SELECT 1');
      res.status(200).json({ status: 'ok' });
    } finally {
      client.release();
    }
  } catch (err) {
    console.error('Health check failed:', err);
    res.status(503).json({ status: 'error', message: 'Database unavailable' });
  }
});

// Only public payment configuration is exposed; secret keys stay server-side.
app.get('/api/config', (req, res) => {
  const key = process.env.STRIPE_PUBLIC_KEY || '';
  res.setHeader('Cache-Control', 'no-store');
  res.json({ stripe_public_key: /^pk_(test|live)_[A-Za-z0-9]+$/.test(key) ? key : null,
    test_payment_mode: process.env.NODE_ENV === 'test' && process.env.PAYMENT_MODE === 'mock' });
});

// API routes
app.use('/api/auth', authRoutes);
app.use('/api/products', productsRoutes);
app.use('/api/cart', cartRoutes);
app.use('/api/orders', ordersRoutes);
app.use('/api/admin', adminRoutes);

// Serve static frontend files (SPA)
app.use(express.static('frontend/dist'));

// 404 handler for API routes (JSON only, no HTML fallback)
app.use('/api', (req, res) => {
  res.status(404).json({ error: 'Not found' });
});

// SPA fallback for non-API routes
app.get('*', (req, res) => {
  res.sendFile('frontend/dist/index.html', { root: '.' });
});

// Error handler
app.use((err, req, res, next) => {
  console.error('Unhandled error:', err);
  res.status(err.status === 403 ? 403 : err.status === 413 ? 413 : 500).json({ error: err.status === 403 ? 'Origin denied' : err.status === 413 ? 'Request too large' : 'Internal server error' });
});

// Start server
const stopOrderExpiry = startOrderExpiry();
app.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
  console.log(`Environment: ${process.env.NODE_ENV || 'development'}`);
});

// Graceful shutdown
process.on('SIGTERM', () => {
  stopOrderExpiry();
  console.log('SIGTERM received, shutting down gracefully');
  pool.end(() => {
    console.log('Database pool closed');
    process.exit(0);
  });
});

process.on('SIGINT', () => {
  stopOrderExpiry();
  console.log('SIGINT received, shutting down gracefully');
  pool.end(() => {
    console.log('Database pool closed');
    process.exit(0);
  });
});