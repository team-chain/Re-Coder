import express from 'express';
import pool from '../db.js';
import { authMiddleware, adminMiddleware } from '../middleware.js';

const router = express.Router();

/**
 * POST /api/admin/products
 * Create a new product (admin only)
 */
router.post('/products', authMiddleware, adminMiddleware, async (req, res) => {
  try {
    const { name, description, price, stock, image_url } = req.body;

    // Validation
    if (!name || price === undefined || stock === undefined) {
      return res.status(400).json({ error: '상품명, 가격, 재고는 필수입니다' });
    }

    const priceNum = price;
    const stockNum = stock;

    if (!Number.isSafeInteger(priceNum) || priceNum < 100 || priceNum > 99999999) {
      return res.status(400).json({ error: '가격은 100원 이상 1억 원 미만의 정수(원)로 입력하세요' });
    }

    if (!Number.isSafeInteger(stockNum) || stockNum < 0 || stockNum > 2147483647) {
      return res.status(400).json({ error: '재고는 0 이상의 정수로 입력하세요' });
    }

    // Create product
    const result = await pool.query(
      `INSERT INTO products (name, description, price, stock, image_url)
       VALUES ($1, $2, $3, $4, $5)
       RETURNING id, name, description, price, stock, image_url, created_at`,
      [name, description || null, priceNum, stockNum, image_url || null]
    );

    const product = result.rows[0];

    res.status(201).json({
      id: product.id,
      name: product.name,
      description: product.description,
      price: parseFloat(product.price),
      stock: product.stock,
      image_url: product.image_url,
      created_at: product.created_at
    });
  } catch (err) {
    console.error('Create product error:', err);
    res.status(500).json({ error: '일시적인 오류가 발생했습니다. 잠시 후 다시 시도하세요' });
  }
});

/**
 * PUT /api/admin/products/:id
 * Update product (admin only)
 */
router.put('/products/:id', authMiddleware, adminMiddleware, async (req, res) => {
  try {
    const { id } = req.params;
    const { name, description, price, stock, image_url } = req.body;

    // Get current product
    const currentResult = await pool.query(
      'SELECT id, name, description, price, stock, image_url FROM products WHERE id = $1',
      [id]
    );

    if (currentResult.rows.length === 0) {
      return res.status(404).json({ error: '상품을 찾을 수 없습니다' });
    }

    const current = currentResult.rows[0];

    // Use provided values or keep existing
    const newName = name !== undefined ? name : current.name;
    const newDescription = description !== undefined ? description : current.description;
    const newPrice = price !== undefined ? price : parseFloat(current.price);
    const newStock = stock !== undefined ? stock : current.stock;
    const newImageUrl = image_url !== undefined ? image_url : current.image_url;

    // Validation
    if (!Number.isSafeInteger(newPrice) || newPrice < 100 || newPrice > 99999999) {
      return res.status(400).json({ error: '가격은 100원 이상 1억 원 미만의 정수(원)로 입력하세요' });
    }

    if (!Number.isSafeInteger(newStock) || newStock < 0 || newStock > 2147483647) {
      return res.status(400).json({ error: '재고는 0 이상의 정수로 입력하세요' });
    }

    // Update product
    const result = await pool.query(
      `UPDATE products
       SET name = $1, description = $2, price = $3, stock = $4, image_url = $5, updated_at = NOW()
       WHERE id = $6
       RETURNING id, name, description, price, stock, image_url, created_at, updated_at`,
      [newName, newDescription || null, newPrice, newStock, newImageUrl || null, id]
    );

    const product = result.rows[0];

    res.json({
      id: product.id,
      name: product.name,
      description: product.description,
      price: parseFloat(product.price),
      stock: product.stock,
      image_url: product.image_url,
      created_at: product.created_at,
      updated_at: product.updated_at
    });
  } catch (err) {
    console.error('Update product error:', err);
    res.status(500).json({ error: '일시적인 오류가 발생했습니다. 잠시 후 다시 시도하세요' });
  }
});

/**
 * DELETE /api/admin/products/:id
 * Delete product (admin only, only if stock is 0)
 */
router.delete('/products/:id', authMiddleware, adminMiddleware, async (req, res) => {
  try {
    const { id } = req.params;

    // Check product stock
    const productResult = await pool.query(
      'SELECT id, stock FROM products WHERE id = $1',
      [id]
    );

    if (productResult.rows.length === 0) {
      return res.status(404).json({ error: '상품을 찾을 수 없습니다' });
    }

    const product = productResult.rows[0];

    if (product.stock !== 0) {
      return res.status(400).json({ error: '재고가 남은 상품은 삭제할 수 없습니다' });
    }

    // Delete product
    await pool.query('DELETE FROM products WHERE id = $1', [id]);

    res.json({ success: true });
  } catch (err) {
    console.error('Delete product error:', err);
    res.status(500).json({ error: '일시적인 오류가 발생했습니다. 잠시 후 다시 시도하세요' });
  }
});

/**
 * GET /api/admin/orders
 * Get all orders (admin only)
 */
router.get('/orders', authMiddleware, adminMiddleware, async (req, res) => {
  try {
    const limit = Math.min(parseInt(req.query.limit) || 50, 100);
    const offset = parseInt(req.query.offset) || 0;

    // Get total count
    const countResult = await pool.query('SELECT COUNT(*) FROM orders');
    const total = parseInt(countResult.rows[0].count);

    // Get orders
    const result = await pool.query(
      `SELECT id, user_id, total_amount, status, payment_id, created_at
       FROM orders
       ORDER BY created_at DESC
       LIMIT $1 OFFSET $2`,
      [limit, offset]
    );

    const orders = result.rows.map(row => ({
      id: row.id,
      user_id: row.user_id,
      total_amount: row.total_amount,
      status: row.status,
      payment_id: row.payment_id,
      created_at: row.created_at
    }));

    res.json({ orders, total });
  } catch (err) {
    console.error('Get admin orders error:', err);
    res.status(500).json({ error: '일시적인 오류가 발생했습니다. 잠시 후 다시 시도하세요' });
  }
});

/**
 * GET /api/admin/orders/:id
 * Get order details (admin only)
 */
router.get('/orders/:id', authMiddleware, adminMiddleware, async (req, res) => {
  try {
    const { id } = req.params;

    // Get order
    const orderResult = await pool.query(
      `SELECT id, user_id, total_amount, status, payment_id, created_at
       FROM orders
       WHERE id = $1`,
      [id]
    );

    if (orderResult.rows.length === 0) {
      return res.status(404).json({ error: '주문을 찾을 수 없습니다' });
    }

    const order = orderResult.rows[0];

    // Get order items
    const itemsResult = await pool.query(
      `SELECT oi.product_id, oi.quantity, oi.price, p.name
       FROM order_items oi
       LEFT JOIN products p ON p.id = oi.product_id
       WHERE oi.order_id = $1`,
      [id]
    );

    const items = itemsResult.rows.map(row => ({
      product_id: row.product_id,
      quantity: row.quantity,
      price: row.price,
      name: row.name
    }));

    res.json({
      id: order.id,
      user_id: order.user_id,
      items,
      total_amount: order.total_amount,
      status: order.status,
      payment_id: order.payment_id,
      created_at: order.created_at
    });
  } catch (err) {
    console.error('Get admin order error:', err);
    res.status(500).json({ error: '일시적인 오류가 발생했습니다. 잠시 후 다시 시도하세요' });
  }
});

export default router;
