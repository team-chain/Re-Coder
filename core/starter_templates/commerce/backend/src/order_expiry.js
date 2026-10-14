import pool from './db.js';
import stripe from './payment.js';

// Separate transactions and SKIP LOCKED permit multiple ECS tasks to run safely.
export async function expirePendingOrders(userId = null) {
  const candidates = await pool.query(
    `SELECT id FROM orders WHERE expires_at < NOW()
     AND status IN ('unpaid', 'pending') AND ($1::int IS NULL OR user_id=$1)
     ORDER BY expires_at, id LIMIT 50`, [userId]);
  let released = 0;
  for (const { id } of candidates.rows) {
    const client = await pool.connect();
    try {
      await client.query('BEGIN');
      const locked = await client.query(
        `SELECT id, payment_id FROM orders WHERE id=$1 AND expires_at < NOW()
         AND status IN ('unpaid', 'pending') FOR UPDATE SKIP LOCKED`, [id]);
      const order = locked.rows[0];
      if (!order) { await client.query('ROLLBACK'); continue; }
      if (order.payment_id) {
        try {
          const payment = await stripe.paymentIntents.cancel(order.payment_id);
          if (payment.status !== 'canceled') { await client.query('ROLLBACK'); continue; }
        } catch {
          // A timeout or already successful payment needs reconciliation, not stock release.
          await client.query('ROLLBACK'); continue;
        }
      }
      const reservations = await client.query(
        `UPDATE inventory_reservations SET status='released'
         WHERE order_id=$1 AND status='reserved' RETURNING product_id, quantity`, [id]);
      for (const item of reservations.rows.sort((a,b) => a.product_id-b.product_id))
        await client.query('UPDATE products SET stock=stock+$1, updated_at=NOW() WHERE id=$2', [item.quantity,item.product_id]);
      await client.query("UPDATE orders SET status='cancelled', updated_at=NOW() WHERE id=$1", [id]);
      await client.query('COMMIT');
      released++;
    } catch (error) {
      await client.query('ROLLBACK').catch(() => {});
      throw error;
    } finally { client.release(); }
  }
  return released;
}

export function startOrderExpiry() {
  let running = false;
  const tick = async () => {
    if (running) return;
    running = true;
    try { await expirePendingOrders(); }
    catch { console.error('Order expiry failed; will retry on the next interval'); }
    finally { running = false; }
  };
  const timer = setInterval(tick, 60_000);
  timer.unref();
  void tick();
  return () => clearInterval(timer);
}
