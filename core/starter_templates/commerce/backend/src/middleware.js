import { verifyToken } from './auth.js';
import pool from './db.js';

/**
 * Middleware to verify JWT token and attach user to request
 */
export function authMiddleware(req, res, next) {
  const authHeader = req.headers.authorization;

  if (!authHeader || !authHeader.startsWith('Bearer ')) {
    return res.status(401).json({ error: '로그인이 필요합니다' });
  }

  const token = authHeader.slice(7);
  const decoded = verifyToken(token);

  if (!decoded) {
    return res.status(401).json({ error: '로그인이 만료되었습니다. 다시 로그인하세요' });
  }

  req.user = decoded;
  next();
}

/**
 * Middleware to check if user is admin
 */
export async function adminMiddleware(req, res, next) {
  if (!req.user || req.user.role !== 'admin') {
    return res.status(403).json({ error: '관리자만 사용할 수 있습니다' });
  }
  try {
    const result = await pool.query('SELECT role FROM users WHERE id = $1', [req.user.userId]);
    if (result.rows[0]?.role !== 'admin') return res.status(403).json({ error: '관리자 권한이 해제되었습니다' });
    next();
  } catch { res.status(503).json({ error: '권한을 확인하지 못했습니다. 잠시 후 다시 시도하세요' }); }
}
