import { useState, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { loadStripe } from '@stripe/stripe-js';
import { Elements, CardElement, useStripe, useElements } from '@stripe/react-stripe-js';
import { useCart } from '../context/CartContext';
import { useAuth } from '../context/AuthContext';
import { ordersAPI } from '../api/client';
import '../styles/Checkout.css';


function CheckoutForm() {
  const stripe = useStripe();
  const elements = useElements();
  const navigate = useNavigate();
  const { items, total, clearCart } = useCart();
  const { user } = useAuth();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [clientSecret, setClientSecret] = useState(null);
  const [orderId, setOrderId] = useState(null);
  const idempotencyKeyRef = useRef(null);

  const handleCreateOrder = async () => {
    if (!idempotencyKeyRef.current) idempotencyKeyRef.current = crypto.randomUUID();
    const response = await ordersAPI.create(idempotencyKeyRef.current);
    setClientSecret(response.data.client_secret);
    setOrderId(response.data.order_id);
    return response.data;
  };

  const handleSubmit = async (e) => {
    e.preventDefault();

    if (!stripe || !elements) {
      setError('Payment system not ready');
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const order = clientSecret ? {client_secret: clientSecret, order_id: orderId} : await handleCreateOrder();
      const result = await stripe.confirmCardPayment(order.client_secret, {
        payment_method: {
          card: elements.getElement(CardElement),
          billing_details: {
            name: user.name,
            email: user.email
          }
        }
      });

      if (result.error) {
        setError(result.error.message);
      } else if (result.paymentIntent.status === 'succeeded') {
        // Payment successful - wait for webhook confirmation
        clearCart();
        navigate(`/order-confirmation/${order.order_id}`);
      } else if (result.paymentIntent.status === 'requires_action') {
        setError('Payment requires additional authentication');
      } else {
        setError('Payment failed');
      }
    } catch (err) {
      setError(err.message || 'Payment processing error');
      console.error('Payment error:', err);
    } finally {
      setLoading(false);
    }
  };

  if (items.length === 0) {
    return (
      <div className="checkout-empty">
        <p>Your cart is empty</p>
      </div>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="checkout-form">
      <div className="checkout-section">
        <h3>Order Summary</h3>
        <div className="order-summary">
          {items.map((item) => (
            <div key={item.product_id} className="summary-item">
              <span>Product {item.product_id}</span>
              <span>x{item.quantity}</span>
              <span>${(item.price * item.quantity).toFixed(2)}</span>
            </div>
          ))}
          <div className="summary-total">
            <strong>Total: ${total.toFixed(2)}</strong>
          </div>
        </div>
      </div>

      <div className="checkout-section">
        <h3>Payment Details</h3>
        <div className="card-element-wrapper">
          <CardElement
            options={{
              style: {
                base: {
                  fontSize: '16px',
                  color: '#424770',
                  '::placeholder': {
                    color: '#aab7c4'
                  }
                },
                invalid: {
                  color: '#fa755a'
                }
              }
            }}
          />
        </div>
      </div>

      {error && <div className="error-message">{error}</div>}

      <button
        type="submit"
        disabled={loading || !stripe}
        className="checkout-button"
      >
        {loading ? 'Processing...' : `Pay $${total.toFixed(2)}`}
      </button>
    </form>
  );
}

function MockCheckout() {
  const navigate = useNavigate();
  const { fetchCart } = useCart();
  const key = useRef(crypto.randomUUID());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const create = async () => {
    setBusy(true); setError('');
    try {
      const response = await ordersAPI.create(key.current);
      await fetchCart();
      navigate(`/orders/${response.data.order_id}`);
    } catch (err) { setError(err.response?.data?.error || 'Order creation failed'); }
    finally { setBusy(false); }
  };
  return <section className="checkout-container">
    <h1>Test checkout</h1>
    <p>Mock payment provider. No card is charged. Payment stays pending until a signed test webhook is received.</p>
    {error && <p role="alert">{error}</p>}
    <button onClick={create} disabled={busy}>{busy ? 'Creating...' : 'Create test order'}</button>
  </section>;
}

function Checkout() {
  const { items } = useCart();
  const navigate = useNavigate();
  const [config, setConfig] = useState(null);
  const [stripePromise, setStripePromise] = useState(null);
  useEffect(() => {
    let active = true;
    fetch('/api/config').then(r => { if (!r.ok) throw Error('Config unavailable'); return r.json(); }).then(value => {
      if (!active) return;
      setConfig(value);
      if (value.stripe_public_key) setStripePromise(loadStripe(value.stripe_public_key));
    }).catch(() => { if (active) setConfig({}); });
    return () => { active = false; };
  }, []);
  if (config === null) return <p role="status">Loading payment settings...</p>;
  if (config.test_payment_mode && items.length > 0) return <MockCheckout />;
  if (!config.stripe_public_key) {
    return (
      <div className="checkout-container">
        <div className="checkout-empty">
          <p>Payment service is not configured. Please contact support.</p>
          <button onClick={() => navigate('/')} className="back-button">
            Back to Home
          </button>
        </div>
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div className="checkout-container">
        <div className="checkout-empty">
          <p>Your cart is empty</p>
          <button onClick={() => navigate('/')} className="back-button">
            Continue Shopping
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="checkout-container">
      <div className="checkout-wrapper">
        <h1>Checkout</h1>
        <Elements stripe={stripePromise}>
          <CheckoutForm />
        </Elements>
      </div>
    </div>
  );
}

export default Checkout;