import express from 'express';
import pool from '../db.js';
import { hashPassword, comparePassword, generateToken } from '../auth.js';

const router = express.Router();

/**
 * POST /api/auth/register
 * Register a new user
 */
router.post('/register', async (req, res) => {
  try {
    const { email, password, name } = req.body;

    // Validation
    if (typeof email !== 'string' || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) || typeof password !== 'string' || typeof name !== 'string' || !name.trim() || name.length > 200 || email.length > 254) {
      return res.status(400).json({ error: 'Email, password, and name are required' });
    }

    if (password.length < 8 || Buffer.byteLength(password) > 72) {
      return res.status(400).json({ error: 'Password must be at least 8 characters long' });
    }

    // Check if email already exists
    const existingUser = await pool.query(
      'SELECT id FROM users WHERE email = $1',
      [email.toLowerCase()]
    );

    if (existingUser.rows.length > 0) {
      return res.status(409).json({ error: 'Email already registered' });
    }

    // Hash password
    const passwordHash = await hashPassword(password);

    // Create user
    const result = await pool.query(
      'INSERT INTO users (email, password_hash, name, role) VALUES ($1, $2, $3, $4) RETURNING id, email, name, role',
      [email.toLowerCase(), passwordHash, name, 'customer']
    );

    const user = result.rows[0];
    const token = generateToken(user.id, user.email, user.role);

    res.status(201).json({
      id: user.id,
      email: user.email,
      name: user.name,
      token
    });
  } catch (err) {
    if (err.code === '23505') return res.status(409).json({ error: 'Email already registered' });
    console.error('Register error:', err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

/**
 * POST /api/auth/login
 * Login user
 */
router.post('/login', async (req, res) => {
  let client;
  try {
    const { email, password } = req.body;

    // Validation
    if (typeof email !== 'string' || typeof password !== 'string' || !email || !password || email.length > 254 || password.length > 72) {
      return res.status(400).json({ error: 'Email and password are required' });
    }

    const emailLower = email.toLowerCase();

    // Serialize attempts per account across all server instances.
    client = await pool.connect();
    await client.query('BEGIN');
    const userResult = await client.query(
      `SELECT id, password_hash, name, role, failed_login_attempts, last_failed_login, (failed_login_attempts >= 5 AND last_failed_login > NOW() - INTERVAL '15 minutes') AS login_locked FROM users WHERE email = $1 FOR UPDATE`,
      [emailLower]
    );

    if (userResult.rows.length === 0) {
      await client.query('ROLLBACK');
      // User not found - return generic error
      return res.status(401).json({ error: 'Email or password is incorrect' });
    }

    const user = userResult.rows[0];

    // Compare in the DB timezone; timestamp without timezone must not use the host clock.
    if (user.login_locked) {
      await client.query('ROLLBACK');
      return res.status(429).json({ error: 'Too many failed login attempts' });
    }

    // Compare password
    const passwordMatch = await comparePassword(password, user.password_hash);

    if (!passwordMatch) {
      // Increment failed login attempts
      await client.query(
        `UPDATE users SET failed_login_attempts = CASE
           WHEN last_failed_login > NOW() - INTERVAL '15 minutes' THEN failed_login_attempts + 1
           ELSE 1 END, last_failed_login = NOW() WHERE id = $1`,
        [user.id]
      );
      await client.query('COMMIT');
      return res.status(401).json({ error: 'Email or password is incorrect' });
    }

    // Reset failed login attempts on successful login
    await client.query(
      'UPDATE users SET failed_login_attempts = 0, last_failed_login = NULL WHERE id = $1',
      [user.id]
    );

    await client.query('COMMIT');
    const token = generateToken(user.id, emailLower, user.role);

    res.json({
      id: user.id,
      email: emailLower,
      name: user.name,
      role: user.role,
      token
    });
  } catch (err) {
    if (client) await client.query('ROLLBACK').catch(() => {});
    console.error('Login error:', err);
    res.status(500).json({ error: 'Internal server error' });
  } finally { client?.release(); }
});

export default router;
