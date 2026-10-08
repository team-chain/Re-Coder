import express from 'express';
import pool from '../db.js';
import stripe, { STRIPE_WEBHOOK_SECRET } from '../payment.js';
const router = express.Router();
const supported = new Set(['payment_intent.succeeded','payment_intent.payment_failed','payment_intent.canceled']);
router.post('/stripe', express.raw({type:'application/json',limit:'100kb'}), async (req,res) => {
  let event;
  try { event = stripe.webhooks.constructEvent(req.body,req.headers['stripe-signature'],STRIPE_WEBHOOK_SECRET); }
  catch { return res.status(400).json({error:'Invalid signature'}); }
  if (!supported.has(event.type)) return res.json({received:true});
  const intent = event.data?.object;
  const orderId = intent?.metadata?.order_id, userId = intent?.metadata?.user_id;
  if (!/^\d+$/.test(String(orderId)) || !/^\d+$/.test(String(userId)) || typeof event.id !== 'string')
    return res.status(400).json({error:'Invalid event'});
  let client;
  try {
    client = await pool.connect();
    await client.query('BEGIN');
    const row = await client.query('SELECT id,user_id,total_amount,status,payment_id FROM orders WHERE id=$1 FOR UPDATE',[orderId]);
    const order = row.rows[0];
    if (!order || order.payment_id !== intent.id || order.user_id !== Number(userId) || order.total_amount !== intent.amount || intent.currency !== 'usd') {
      await client.query('ROLLBACK'); return res.status(400).json({error:'Payment does not match order'});
    }
    // Event acknowledgement and business updates commit or roll back together.
    const fresh = await client.query('INSERT INTO stripe_events(event_id,event_type,order_id) VALUES($1,$2,$3) ON CONFLICT(event_id) DO NOTHING RETURNING id',[event.id,event.type,orderId]);
    if (!fresh.rowCount) { await client.query('COMMIT'); return res.json({received:true}); }
    if (event.type === 'payment_intent.succeeded') {
      if (order.status === 'cancelled') { await client.query('ROLLBACK'); return res.status(400).json({error:'Order is closed'}); }
      if (['pending','unpaid'].includes(order.status)) {
        await client.query("UPDATE orders SET status='paid',updated_at=NOW() WHERE id=$1",[orderId]);
        await client.query("UPDATE inventory_reservations SET status='confirmed' WHERE order_id=$1 AND status='reserved'",[orderId]);
      }
    } else if (event.type === 'payment_intent.canceled' && ['pending','unpaid'].includes(order.status)) {
      const released = await client.query("UPDATE inventory_reservations SET status='released' WHERE order_id=$1 AND status='reserved' RETURNING product_id,quantity",[orderId]);
      for (const item of released.rows.sort((a,b)=>a.product_id-b.product_id))
        await client.query('UPDATE products SET stock=stock+$1,updated_at=NOW() WHERE id=$2',[item.quantity,item.product_id]);
      await client.query("UPDATE orders SET status='cancelled',updated_at=NOW() WHERE id=$1",[orderId]);
    }
    // Failed attempts remain retryable. Late cancellation/failure never overwrites paid.
    await client.query('COMMIT'); return res.json({received:true});
  } catch {
    if (client) await client.query('ROLLBACK').catch(()=>{});
    return res.status(500).json({error:'Payment event processing failed'});
  } finally { client?.release(); }
});
export default router;
