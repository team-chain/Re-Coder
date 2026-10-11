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
      `SELECT c.product_id, c.quantity, p.price, p.name, p.image_url
       FROM carts c
       JOIN products p ON c.product_id = p.id
       WHERE c.user_id = $1
       ORDER BY c.created_at DESC`,
      [userId]
    );

    const items = result.rows.map(row => ({
      product_id: row.product_id,
      quantity: row.quantity,
      price: parseFloat(row.price),
      name: row.name,
      image_url: row.image_url
    }));

    const total = items.reduce((sum, item) => sum + (item.price * item.quantity), 0);

    res.json({ items, total });
  } catch (err) {
    console.error('Get cart error:', err);
    res.status(500).json({ error: '일시적인 오류가 발생했습니다. 잠시 후 다시 시도하세요' });
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
      return res.status(400).json({ error: '상품과 수량을 지정하세요' });
    }

    if (!Number.isInteger(quantity) || quantity <= 0) {
      return res.status(400).json({ error: '수량은 1 이상의 정수여야 합니다' });
    }

    // Check product exists and has stock
    const productResult = await pool.query(
      'SELECT id, stock FROM products WHERE id = $1',
      [product_id]
    );

    if (productResult.rows.length === 0) {
      return res.status(404).json({ error: '상품을 찾을 수 없습니다' });
    }

    const product = productResult.rows[0];

    if (product.stock < quantity) {
      return res.status(400).json({ error: '재고가 부족합니다' });
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
    res.status(500).json({ error: '일시적인 오류가 발생했습니다. 잠시 후 다시 시도하세요' });
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
      return res.status(400).json({ error: '수량을 입력하세요' });
    }

    if (!Number.isInteger(quantity) || quantity <= 0) {
      return res.status(400).json({ error: '수량은 1 이상의 정수여야 합니다' });
    }

    // Check product exists and has stock
    const productResult = await pool.query(
      'SELECT id, stock FROM products WHERE id = $1',
      [product_id]
    );

    if (productResult.rows.length === 0) {
      return res.status(404).json({ error: '상품을 찾을 수 없습니다' });
    }

    const product = productResult.rows[0];

    if (product.stock < quantity) {
      return res.status(400).json({ error: '재고가 부족합니다' });
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
      return res.status(404).json({ error: '장바구니에 없는 상품입니다' });
    }

    const cartItem = result.rows[0];

    res.json({
      product_id: cartItem.product_id,
      quantity: cartItem.quantity
    });
  } catch (err) {
    console.error('Update cart error:', err);
    res.status(500).json({ error: '일시적인 오류가 발생했습니다. 잠시 후 다시 시도하세요' });
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
      return res.status(404).json({ error: '장바구니에 없는 상품입니다' });
    }

    res.json({ success: true });
  } catch (err) {
    console.error('Delete cart item error:', err);
    res.status(500).json({ error: '일시적인 오류가 발생했습니다. 잠시 후 다시 시도하세요' });
  }
});

export default router;
