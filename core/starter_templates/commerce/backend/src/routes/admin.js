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
      return res.status(400).json({ error: 'name, price, and stock are required' });
    }

    const priceNum = price;
    const stockNum = stock;

    if (!Number.isFinite(priceNum) || priceNum <= 0 || priceNum > 21474836.47 || Math.abs(priceNum * 100 - Math.round(priceNum * 100)) > 0.000001) {
      return res.status(400).json({ error: 'price must be a positive number' });
    }

    if (!Number.isSafeInteger(stockNum) || stockNum < 0 || stockNum > 2147483647) {
      return res.status(400).json({ error: 'stock must be a non-negative integer' });
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
    res.status(500).json({ error: 'Internal server error' });
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
      return res.status(404).json({ error: 'Product not found' });
    }

    const current = currentResult.rows[0];

    // Use provided values or keep existing
    const newName = name !== undefined ? name : current.name;
    const newDescription = description !== undefined ? description : current.description;
    const newPrice = price !== undefined ? price : parseFloat(current.price);
    const newStock = stock !== undefined ? stock : current.stock;
    const newImageUrl = image_url !== undefined ? image_url : current.image_url;

    // Validation
    if (!Number.isFinite(newPrice) || newPrice <= 0 || newPrice > 21474836.47 || Math.abs(newPrice * 100 - Math.round(newPrice * 100)) > 0.000001) {
      return res.status(400).json({ error: 'price must be a positive number' });
    }

    if (!Number.isSafeInteger(newStock) || newStock < 0 || newStock > 2147483647) {
      return res.status(400).json({ error: 'stock must be a non-negative integer' });
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
    res.status(500).json({ error: 'Internal server error' });
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
      return res.status(404).json({ error: 'Product not found' });
    }

    const product = productResult.rows[0];

    if (product.stock !== 0) {
      return res.status(400).json({ error: 'Cannot delete product with non-zero stock' });
    }

    // Delete product
    await pool.query('DELETE FROM products WHERE id = $1', [id]);

    res.json({ success: true });
  } catch (err) {
    console.error('Delete product error:', err);
    res.status(500).json({ error: 'Internal server error' });
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
    res.status(500).json({ error: 'Internal server error' });
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
      return res.status(404).json({ error: 'Order not found' });
    }

    const order = orderResult.rows[0];

    // Get order items
    const itemsResult = await pool.query(
      `SELECT product_id, quantity, price
       FROM order_items
       WHERE order_id = $1`,
      [id]
    );

    const items = itemsResult.rows.map(row => ({
      product_id: row.product_id,
      quantity: row.quantity,
      price: row.price
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
    res.status(500).json({ error: 'Internal server error' });
  }
});

export default router;
