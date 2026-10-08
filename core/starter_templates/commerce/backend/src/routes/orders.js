import express from 'express';
import pool from '../db.js';
import { authMiddleware } from '../middleware.js';
import stripe from '../payment.js';
import { expirePendingOrders } from '../order_expiry.js';

const router = express.Router();

const ORDER_EXPIRY_MINUTES = 15;
const MAX_QUANTITY_PER_ITEM = 1000;

/**
 * POST /api/orders
 * Create a new order and initialize Stripe payment
 */
router.post('/', authMiddleware, async (req, res) => {
  let client;
  try {
    client = await pool.connect();
    const userId = req.user.userId;
    const { idempotency_key } = req.body;

    // Validate idempotency_key
    if (!idempotency_key || typeof idempotency_key !== 'string') {
      return res.status(400).json({ error: 'idempotency_key is required' });
    }
    if (idempotency_key.length < 8 || idempotency_key.length > 128) {
      return res.status(400).json({ error: 'Invalid idempotency_key length' });
    }

    await client.query('BEGIN');

    // Lock user row for serialization
    await client.query(
      'SELECT id FROM users WHERE id = $1 FOR UPDATE',
      [userId]
    );

    // Check for existing order with same idempotency_key
    const existingOrderResult = await client.query(
      `SELECT id, user_id, total_amount, status, payment_id, created_at
       FROM orders
       WHERE user_id = $1 AND idempotency_key = $2
       FOR UPDATE`,
      [userId, idempotency_key]
    );

    if (existingOrderResult.rows.length > 0) {
      const existingOrder = existingOrderResult.rows[0];
      if (!['unpaid','pending'].includes(existingOrder.status)) {
        await client.query('ROLLBACK');
        return res.status(409).json({ error: 'Order is already closed' });
      }

      // Verify ownership
      if (existingOrder.user_id !== userId) {
        await client.query('ROLLBACK');
        return res.status(403).json({ error: 'Access denied' });
      }

      // If payment_id exists, retrieve from Stripe
      if (existingOrder.payment_id) {
        try {
          const paymentIntent = await stripe.paymentIntents.retrieve(existingOrder.payment_id);
          await client.query('COMMIT');
          return res.status(200).json({
            order_id: existingOrder.id,
            total_amount: existingOrder.total_amount,
            client_secret: paymentIntent.client_secret
          });
        } catch (stripeErr) {
          await client.query('ROLLBACK');
          console.error('Stripe retrieve error:', stripeErr);
          return res.status(502).json({ error: 'Payment service unavailable' });
        }
      }

      // If no payment_id, create PaymentIntent for existing order
      try {
        const userResult = await client.query(
          'SELECT email FROM users WHERE id = $1',
          [userId]
        );
        const userEmail = userResult.rows[0].email;

        const paymentIntent = await stripe.paymentIntents.create(
          {
            amount: existingOrder.total_amount,
            currency: 'usd',
            metadata: {
              order_id: existingOrder.id.toString(),
              user_id: userId.toString(),
              idempotency_key: idempotency_key
            },
            receipt_email: userEmail
          },
          {
            idempotencyKey: `order-${existingOrder.id}`
          }
        );

        await client.query(
          `UPDATE orders SET payment_id = $1, status = 'pending', updated_at = NOW() WHERE id = $2`,
          [paymentIntent.id, existingOrder.id]
        );

        await client.query('COMMIT');

        return res.status(200).json({
          order_id: existingOrder.id,
          total_amount: existingOrder.total_amount,
          client_secret: paymentIntent.client_secret
        });
      } catch (stripeErr) {
        await client.query('ROLLBACK');
        console.error('Stripe create error for existing order:', stripeErr);
        if (stripeErr.statusCode && stripeErr.statusCode >= 400 && stripeErr.statusCode < 500) {
          return res.status(400).json({ error: 'Payment initialization failed' });
        }
        return res.status(502).json({ error: 'Payment service unavailable' });
      }
    }

    // Get user email for Stripe
    const userResult = await client.query(
      'SELECT email FROM users WHERE id = $1',
      [userId]
    );
    const userEmail = userResult.rows[0].email;

    // Get cart items and lock products in sorted order (prevent deadlock)
    const cartResult = await client.query(
      `SELECT c.product_id, c.quantity
       FROM carts c
       WHERE c.user_id = $1
       ORDER BY c.product_id`,
      [userId]
    );

    if (cartResult.rows.length === 0) {
      await client.query('ROLLBACK');
      return res.status(400).json({ error: 'Cart is empty' });
    }

    // Lock products in sorted order
    const productIds = cartResult.rows.map(item => item.product_id);
    const productsResult = await client.query(
      `SELECT id, price, stock
       FROM products
       WHERE id = ANY($1::int[])
       ORDER BY id
       FOR UPDATE`,
      [productIds]
    );

    const productsMap = {};
    productsResult.rows.forEach(p => {
      productsMap[p.id] = p;
    });

    // Validate stock and calculate total
    let totalAmount = 0;
    const orderItems = [];

    for (const item of cartResult.rows) {
      const product = productsMap[item.product_id];

      if (!product) {
        await client.query('ROLLBACK');
        return res.status(400).json({ error: 'Invalid product in cart' });
      }

      // Validate quantity
      if (item.quantity <= 0 || !Number.isInteger(item.quantity)) {
        await client.query('ROLLBACK');
        return res.status(400).json({ error: 'Invalid quantity' });
      }

      if (item.quantity > MAX_QUANTITY_PER_ITEM) {
        await client.query('ROLLBACK');
        return res.status(400).json({ error: 'Quantity exceeds maximum allowed' });
      }

      if (item.quantity > product.stock) {
        await client.query('ROLLBACK');
        return res.status(400).json({ error: 'Insufficient stock' });
      }

      const priceInCents = Math.round(parseFloat(product.price) * 100);
      const itemTotal = priceInCents * item.quantity;
      totalAmount += itemTotal;

      orderItems.push({
        product_id: item.product_id,
        quantity: item.quantity,
        price: priceInCents
      });
    }

    if (!Number.isSafeInteger(totalAmount) || totalAmount < 50 || totalAmount > 99999999) {
      await client.query('ROLLBACK');
      return res.status(400).json({ error: 'Order amount is outside payment limits' });
    }

    // Calculate expiry time
    const expiresAt = new Date(Date.now() + ORDER_EXPIRY_MINUTES * 60 * 1000);

    // Create order in unpaid status
    const orderResult = await client.query(
      `INSERT INTO orders (user_id, total_amount, status, idempotency_key, expires_at)
       VALUES ($1, $2, 'unpaid', $3, $4)
       RETURNING id`,
      [userId, totalAmount, idempotency_key, expiresAt]
    );

    const orderId = orderResult.rows[0].id;

    // Insert order items
    for (const item of orderItems) {
      await client.query(
        `INSERT INTO order_items (order_id, product_id, quantity, price)
         VALUES ($1, $2, $3, $4)`,
        [orderId, item.product_id, item.quantity, item.price]
      );
    }

    // Deduct stock immediately
    for (const item of orderItems) {
      await client.query(
        `UPDATE products SET stock = stock - $1, updated_at = NOW() WHERE id = $2`,
        [item.quantity, item.product_id]
      );
    }

    // Create inventory reservations
    for (const item of orderItems) {
      await client.query(
        `INSERT INTO inventory_reservations (order_id, product_id, quantity, status)
         VALUES ($1, $2, $3, 'reserved')`,
        [orderId, item.product_id, item.quantity]
      );
    }

    // Clear cart
    await client.query(
      'DELETE FROM carts WHERE user_id = $1',
      [userId]
    );

    await client.query('COMMIT');

    // After commit, re-acquire order lock and create Stripe PaymentIntent
    await client.query('BEGIN');

    const locked = await client.query('SELECT status FROM orders WHERE id = $1 FOR UPDATE', [orderId]);
    if (!['unpaid','pending'].includes(locked.rows[0]?.status)) {
      await client.query('ROLLBACK');
      return res.status(409).json({ error: 'Order is already closed' });
    }

    let paymentIntent;
    try {
      paymentIntent = await stripe.paymentIntents.create(
        {
          amount: totalAmount,
          currency: 'usd',
          metadata: {
            order_id: orderId.toString(),
            user_id: userId.toString(),
            idempotency_key: idempotency_key
          },
          receipt_email: userEmail
        },
        {
          idempotencyKey: `order-${orderId}`
        }
      );
    } catch (stripeErr) {
      await client.query('ROLLBACK');
      console.error('Stripe create error:', stripeErr);
      if (stripeErr.statusCode && stripeErr.statusCode >= 400 && stripeErr.statusCode < 500) {
        return res.status(400).json({ error: 'Payment initialization failed' });
      }
      // Uncertain failure - keep order and reservations for retry
      return res.status(502).json({ error: 'Payment service unavailable' });
    }

    // Update order with payment_id and status
    await client.query(
      `UPDATE orders SET payment_id = $1, status = 'pending', updated_at = NOW() WHERE id = $2`,
      [paymentIntent.id, orderId]
    );

    await client.query('COMMIT');

    res.status(201).json({
      order_id: orderId,
      total_amount: totalAmount,
      client_secret: paymentIntent.client_secret
    });
  } catch (err) {
    if (client) {
      await client.query('ROLLBACK').catch(() => {});
    }
    console.error('Create order error:', err);
    res.status(500).json({ error: 'Internal server error' });
  } finally {
    if (client) {
      client.release();
    }
  }
});

/**
 * GET /api/orders
 * Get user's orders and clean up expired orders
 */
router.get('/', authMiddleware, async (req, res) => {
  try {
    const userId = req.user.userId;

    await expirePendingOrders(userId);

    // Get all orders
    const result = await pool.query(
      `SELECT id, total_amount, status, created_at
       FROM orders
       WHERE user_id = $1
       ORDER BY created_at DESC`,
      [userId]
    );

    const orders = result.rows.map(row => ({
      id: row.id,
      total_amount: row.total_amount,
      status: row.status,
      created_at: row.created_at
    }));

    res.json({ orders });
  } catch (err) {
    console.error('Get orders error:', err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

/**
 * GET /api/orders/:id
 * Get order details (owner only)
 */
router.get('/:id', authMiddleware, async (req, res) => {
  try {
    const userId = req.user.userId;
    const { id } = req.params;

    // Check order ownership
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

    if (order.user_id !== userId) {
      return res.status(403).json({ error: 'Access denied' });
    }

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
      items,
      total_amount: order.total_amount,
      status: order.status,
      payment_id: order.payment_id,
      created_at: order.created_at
    });
  } catch (err) {
    console.error('Get order error:', err);
    res.status(500).json({ error: 'Internal server error' });
  }
});

/**
 * POST /api/orders/:id/cancel
 * Cancel order (unpaid or pending only)
 */
router.post('/:id/cancel', authMiddleware, async (req, res) => {
  let client;
  try {
    client = await pool.connect();
    const userId = req.user.userId;
    const { id } = req.params;

    await client.query('BEGIN');

    // Check order ownership and status
    const orderResult = await client.query(
      `SELECT id, user_id, status, payment_id FROM orders WHERE id = $1 FOR UPDATE`,
      [id]
    );

    if (orderResult.rows.length === 0) {
      await client.query('ROLLBACK');
      return res.status(404).json({ error: 'Order not found' });
    }

    const order = orderResult.rows[0];

    if (order.user_id !== userId) {
      await client.query('ROLLBACK');
      return res.status(403).json({ error: 'Access denied' });
    }

    // If already cancelled, return idempotent success
    if (order.status === 'cancelled') {
      await client.query('COMMIT');
      return res.json({ status: 'cancelled' });
    }

    // Cannot cancel paid orders
    if (order.status === 'paid') {
      await client.query('ROLLBACK');
      return res.status(409).json({ error: 'Cannot cancel completed payment' });
    }

    // If pending with payment_id, cancel Stripe PaymentIntent
    if (order.status === 'pending' && order.payment_id) {
      try {
        const paymentIntent = await stripe.paymentIntents.cancel(order.payment_id);

        // If payment already succeeded, cannot cancel
        if (paymentIntent.status !== 'canceled') {
          await client.query('ROLLBACK');
          return res.status(409).json({ error: 'Payment already completed' });
        }
      } catch (stripeErr) {
        await client.query('ROLLBACK');
        console.error('Stripe cancel error:', stripeErr);
        return res.status(502).json({ error: 'Payment service unavailable' });
      }
    }

    // Release inventory reservations and restore stock (exactly once)
    const reservationsResult = await client.query(
      `SELECT product_id, quantity FROM inventory_reservations
       WHERE order_id = $1 AND status = 'reserved'`,
      [id]
    );

    for (const reservation of reservationsResult.rows) {
      // Restore stock
      await client.query(
        `UPDATE products SET stock = stock + $1, updated_at = NOW() WHERE id = $2`,
        [reservation.quantity, reservation.product_id]
      );

      // Mark reservation as released
      await client.query(
        `UPDATE inventory_reservations SET status = 'released' WHERE order_id = $1 AND product_id = $2`,
        [id, reservation.product_id]
      );
    }

    // Update order status
    await client.query(
      `UPDATE orders SET status = 'cancelled', updated_at = NOW() WHERE id = $1`,
      [id]
    );

    await client.query('COMMIT');

    res.json({ status: 'cancelled' });
  } catch (err) {
    if (client) {
      await client.query('ROLLBACK').catch(() => {});
    }
    console.error('Cancel order error:', err);
    res.status(500).json({ error: 'Internal server error' });
  } finally {
    if (client) {
      client.release();
    }
  }
});

export default router;
