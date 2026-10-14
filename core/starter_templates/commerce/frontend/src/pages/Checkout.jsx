import { useState, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
// /pure: 실제 결제 키가 있을 때만 Stripe.js 를 불러온다(모의 결제·오프라인에서 불필요한 외부 로드 방지).
import { loadStripe } from '@stripe/stripe-js/pure';
import { Elements, CardElement, useStripe, useElements } from '@stripe/react-stripe-js';
import { useCart } from '../context/CartContext';
import { useAuth } from '../context/AuthContext';
import { ordersAPI } from '../api/client';
import '../styles/Checkout.css';
import { formatWon } from '../utils/money';


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
      setError('결제 모듈을 준비하는 중입니다. 잠시 후 다시 시도하세요');
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
        setError('카드사 추가 인증이 필요합니다');
      } else {
        setError('결제에 실패했습니다');
      }
    } catch (err) {
      setError(err.message || '결제 처리 중 오류가 발생했습니다');
      console.error('Payment error:', err);
    } finally {
      setLoading(false);
    }
  };

  if (items.length === 0) {
    return (
      <div className="checkout-empty">
        <p>장바구니가 비어 있습니다</p>
      </div>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="checkout-form">
      <div className="checkout-section">
        <h3>주문 상품</h3>
        <div className="order-summary">
          {items.map((item) => (
            <div key={item.product_id} className="summary-item">
              <span>{item.name || `상품 #${item.product_id}`}</span>
              <span>× {item.quantity}</span>
              <span>{formatWon(item.price * item.quantity)}</span>
            </div>
          ))}
          <div className="summary-total">
            <span>최종 결제 금액</span>
            <strong>{formatWon(total)}</strong>
          </div>
        </div>
      </div>

      <div className="checkout-section">
        <h3>카드 정보</h3>
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
        {loading ? '결제하는 중…' : `${formatWon(total)} 결제하기`}
      </button>
    </form>
  );
}

function MockCheckout() {
  const navigate = useNavigate();
  const { items, total, fetchCart } = useCart();
  const key = useRef(crypto.randomUUID());
  const [busy, setBusy] = useState(false);
  const [agreed, setAgreed] = useState(false);
  const [error, setError] = useState('');
  const create = async () => {
    if (!agreed) { setError('주문 내용 확인 및 결제 동의에 체크해 주세요'); return; }
    setBusy(true); setError('');
    try {
      const response = await ordersAPI.create(key.current);
      await fetchCart();
      navigate(`/orders/${response.data.order_id}`);
    } catch (err) { setError(err.response?.data?.error || '주문을 만들지 못했습니다. 잠시 후 다시 시도하세요'); }
    finally { setBusy(false); }
  };
  return (
    <div className="checkout-container">
      <div className="checkout-wrapper">
        <h1>주문·결제</h1>
        <div className="checkout-section">
          <h3>주문 상품 {items.length}개</h3>
          <div className="order-summary">
            {items.map((item) => (
              <div key={item.product_id} className="summary-item">
                <span>{item.name || `상품 #${item.product_id}`}</span>
                <span>× {item.quantity}</span>
                <span>{formatWon(item.price * item.quantity)}</span>
              </div>
            ))}
            <div className="summary-item"><span>배송비</span><span></span><span>무료</span></div>
            <div className="summary-total">
              <span>최종 결제 금액</span>
              <strong>{formatWon(total)}</strong>
            </div>
          </div>
        </div>
        <div className="checkout-section">
          <h3>결제 수단</h3>
          <p className="test-mode-note">테스트 결제 모드 — 실제로 청구되지 않습니다. 주문 후 몇 초 안에 '결제 완료(테스트)'로 바뀝니다. 결제 키를 넣으면 실제 결제로 전환됩니다.</p>
        </div>
        <label className="consent">
          <input type="checkbox" checked={agreed} onChange={(e) => { setAgreed(e.target.checked); setError(''); }} />
          <span>주문 내용을 확인했으며, 결제에 동의합니다 (필수)</span>
        </label>
        {error && <p className="error-message" role="alert">{error}</p>}
        <button className="checkout-button" onClick={create} disabled={busy}>
          {busy ? '주문하는 중…' : `${formatWon(total)} 테스트 결제하기`}
        </button>
        <p className="checkout-note">교환·반품은 받은 날로부터 7일 이내 신청할 수 있습니다.</p>
      </div>
    </div>
  );
}

function Checkout() {
  const { items } = useCart();
  const navigate = useNavigate();
  const [config, setConfig] = useState(null);
  const [stripePromise, setStripePromise] = useState(null);
  useEffect(() => {
    let active = true;
    fetch('/api/config').then(r => { if (!r.ok) throw Error('결제 설정을 불러오지 못했습니다'); return r.json(); }).then(value => {
      if (!active) return;
      setConfig(value);
      if (value.stripe_public_key) setStripePromise(loadStripe(value.stripe_public_key));
    }).catch(() => { if (active) setConfig({}); });
    return () => { active = false; };
  }, []);
  if (config === null) return <p role="status" className="loading">결제 설정을 불러오는 중…</p>;
  if (config.test_payment_mode && items.length > 0) return <MockCheckout />;
  if (!config.stripe_public_key) {
    return (
      <div className="checkout-container">
        <div className="checkout-empty">
          <p>결제 서비스가 아직 설정되지 않았습니다. 고객센터로 문의하세요.</p>
          <button onClick={() => navigate('/')} className="back-button">
            홈으로
          </button>
        </div>
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div className="checkout-container">
        <div className="checkout-empty">
          <p>장바구니가 비어 있습니다</p>
          <button onClick={() => navigate('/')} className="back-button">
            쇼핑 계속하기
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="checkout-container">
      <div className="checkout-wrapper">
        <h1>주문·결제</h1>
        <Elements stripe={stripePromise}>
          <CheckoutForm />
        </Elements>
      </div>
    </div>
  );
}

export default Checkout;