import express from 'express';
import pool from '../db.js';

const router = express.Router();

/**
 * GET /api/products
 * Get products with pagination and search
 * Query params: limit (default 20), offset (default 0), search (optional)
 */
router.get('/', async (req, res) => {
  try {
    const limit = Math.min(parseInt(req.query.limit) || 20, 100);
    const offset = parseInt(req.query.offset) || 0;
    const search = req.query.search ? `%${req.query.search}%` : null;

    let query = 'SELECT id, name, description, price, stock, image_url FROM products';
    let countQuery = 'SELECT COUNT(*) FROM products';
    const params = [];

    if (search) {
      query += ' WHERE name ILIKE $1 OR description ILIKE $1';
      countQuery += ' WHERE name ILIKE $1 OR description ILIKE $1';
      params.push(search);
    }

    // Get total count
    const countResult = await pool.query(countQuery, params);
    const total = parseInt(countResult.rows[0].count);

    // Get products
    query += ' ORDER BY created_at DESC LIMIT $' + (params.length + 1) + ' OFFSET $' + (params.length + 2);
    params.push(limit, offset);

    const result = await pool.query(query, params);

    const products = result.rows.map(p => ({
      id: p.id,
      name: p.name,
      description: p.description,
      price: parseFloat(p.price),
      stock: p.stock,
      image_url: p.image_url
    }));

    res.json({ products, total });
  } catch (err) {
    console.error('Get products error:', err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

/**
 * GET /api/products/:id
 * Get product details
 */
router.get('/:id', async (req, res) => {
  try {
    const { id } = req.params;

    const result = await pool.query(
      'SELECT id, name, description, price, stock, image_url, created_at FROM products WHERE id = $1',
      [id]
    );

    if (result.rows.length === 0) {
      return res.status(404).json({ error: 'Product not found' });
    }

    const product = result.rows[0];

    res.json({
      id: product.id,
      name: product.name,
      description: product.description,
      price: parseFloat(product.price),
      stock: product.stock,
      image_url: product.image_url,
      created_at: product.created_at
    });
  } catch (err) {
    console.error('Get product error:', err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

export default router;
