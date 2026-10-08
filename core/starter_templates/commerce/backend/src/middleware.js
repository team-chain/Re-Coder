import { verifyToken } from './auth.js';
import pool from './db.js';

/**
 * Middleware to verify JWT token and attach user to request
 */
export function authMiddleware(req, res, next) {
  const authHeader = req.headers.authorization;

  if (!authHeader || !authHeader.startsWith('Bearer ')) {
    return res.status(401).json({ error: 'Missing or invalid authorization header' });
  }

  const token = authHeader.slice(7);
  const decoded = verifyToken(token);

  if (!decoded) {
    return res.status(401).json({ error: 'Invalid or expired token' });
  }

  req.user = decoded;
  next();
}

/**
 * Middleware to check if user is admin
 */
export async function adminMiddleware(req, res, next) {
  if (!req.user || req.user.role !== 'admin') {
    return res.status(403).json({ error: 'Admin access required' });
  }
  try {
    const result = await pool.query('SELECT role FROM users WHERE id = $1', [req.user.userId]);
    if (result.rows[0]?.role !== 'admin') return res.status(403).json({ error: 'Admin access revoked' });
    next();
  } catch { res.status(503).json({ error: 'Authorization service unavailable' }); }
}
