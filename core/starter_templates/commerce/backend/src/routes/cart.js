import express from 'express';
import pool from '../db.js';
import { authMiddleware } from '../middleware.js';

const router = express.Router();

/**
 * GET /api/cart
 * Get user's cart items
 */
router.get('/', authMiddleware, async (req, res) => {
  try {
    const userId = req.user.userId;

    const result = await pool.query(
      `SELECT c.product_id, c.quantity, p.price
       FROM carts c
       JOIN products p ON c.product_id = p.id
       WHERE c.user_id = $1
       ORDER BY c.created_at DESC`,
      [userId]
    );

    const items = result.rows.map(row => ({
      product_id: row.product_id,
      quantity: row.quantity,
      price: parseFloat(row.price)
    }));

    const total = items.reduce((sum, item) => sum + (item.price * item.quantity), 0);

    res.json({ items, total });
  } catch (err) {
    console.error('Get cart error:', err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

/**
 * POST /api/cart
 * Add item to cart
 */
router.post('/', authMiddleware, async (req, res) => {
  try {
    const userId = req.user.userId;
    const { product_id, quantity } = req.body;

    // Validation
    if (!product_id || !quantity) {
      return res.status(400).json({ error: 'product_id and quantity are required' });
    }

    if (!Number.isInteger(quantity) || quantity <= 0) {
      return res.status(400).json({ error: 'quantity must be a positive integer' });
    }

    // Check product exists and has stock
    const productResult = await pool.query(
      'SELECT id, stock FROM products WHERE id = $1',
      [product_id]
    );

    if (productResult.rows.length === 0) {
      return res.status(404).json({ error: 'Product not found' });
    }

    const product = productResult.rows[0];

    if (product.stock < quantity) {
      return res.status(400).json({ error: 'Insufficient stock' });
    }

    // Add or update cart item
    const result = await pool.query(
      `INSERT INTO carts (user_id, product_id, quantity)
       VALUES ($1, $2, $3)
       ON CONFLICT (user_id, product_id)
       DO UPDATE SET quantity = carts.quantity + $3, updated_at = NOW()
       RETURNING product_id, quantity`,
      [userId, product_id, quantity]
    );

    const cartItem = result.rows[0];

    res.status(201).json({
      product_id: cartItem.product_id,
      quantity: cartItem.quantity
    });
  } catch (err) {
    console.error('Add to cart error:', err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

/**
 * POST /api/cart/:product_id
 * Update cart item quantity
 */
router.post('/:product_id', authMiddleware, async (req, res) => {
  try {
    const userId = req.user.userId;
    const { product_id } = req.params;
    const { quantity } = req.body;

    // Validation
    if (!quantity) {
      return res.status(400).json({ error: 'quantity is required' });
    }

    if (!Number.isInteger(quantity) || quantity <= 0) {
      return res.status(400).json({ error: 'quantity must be a positive integer' });
    }

    // Check product exists and has stock
    const productResult = await pool.query(
      'SELECT id, stock FROM products WHERE id = $1',
      [product_id]
    );

    if (productResult.rows.length === 0) {
      return res.status(404).json({ error: 'Product not found' });
    }

    const product = productResult.rows[0];

    if (product.stock < quantity) {
      return res.status(400).json({ error: 'Insufficient stock' });
    }

    // Update cart item
    const result = await pool.query(
      `UPDATE carts
       SET quantity = $1, updated_at = NOW()
       WHERE user_id = $2 AND product_id = $3
       RETURNING product_id, quantity`,
      [quantity, userId, product_id]
    );

    if (result.rows.length === 0) {
      return res.status(404).json({ error: 'Cart item not found' });
    }

    const cartItem = result.rows[0];

    res.json({
      product_id: cartItem.product_id,
      quantity: cartItem.quantity
    });
  } catch (err) {
    console.error('Update cart error:', err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

/**
 * DELETE /api/cart/:product_id
 * Remove item from cart
 */
router.delete('/:product_id', authMiddleware, async (req, res) => {
  try {
    const userId = req.user.userId;
    const { product_id } = req.params;

    const result = await pool.query(
      'DELETE FROM carts WHERE user_id = $1 AND product_id = $2 RETURNING product_id',
      [userId, product_id]
    );

    if (result.rows.length === 0) {
      return res.status(404).json({ error: 'Cart item not found' });
    }

    res.json({ success: true });
  } catch (err) {
    console.error('Delete cart item error:', err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

export default router;
