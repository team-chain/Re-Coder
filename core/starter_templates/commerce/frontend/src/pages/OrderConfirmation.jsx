import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { ordersAPI } from '../api/client';
import '../styles/OrderConfirmation.css';
import { formatWon } from '../utils/money';
import { statusLabel, formatDateTime } from '../utils/labels';

function OrderConfirmation() {
  const { orderId } = useParams();
  const navigate = useNavigate();
  const [order, setOrder] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    const fetchOrder = async () => {
      try {
        setLoading(true);
        setError(null);
        const response = await ordersAPI.getById(orderId);
        setOrder(response.data);
      } catch (err) {
        setError(err.response?.data?.error || '주문을 불러오지 못했습니다');
        console.error('Fetch order error:', err);
      } finally {
        setLoading(false);
      }
    };

    if (orderId) {
      fetchOrder();
    }
  }, [orderId]);

  if (loading) {
    return (
      <div className="confirmation-container">
        <div className="confirmation-loading">주문 정보를 불러오는 중…</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="confirmation-container">
        <div className="confirmation-error">
          <p>{error}</p>
          <button onClick={() => navigate('/')} className="back-button">
            홈으로
          </button>
        </div>
      </div>
    );
  }

  if (!order) {
    return (
      <div className="confirmation-container">
        <div className="confirmation-error">
          <p>주문을 찾을 수 없습니다</p>
          <button onClick={() => navigate('/')} className="back-button">
            홈으로
          </button>
        </div>
      </div>
    );
  }

  const statusColor = {
    paid: '#4caf50',
    pending: '#ff9800',
    unpaid: '#f44336',
    failed: '#f44336',
    cancelled: '#9e9e9e'
  };

  return (
    <div className="confirmation-container">
      <div className="confirmation-wrapper">
        <div className="confirmation-header">
          <div className="success-icon">✓</div>
          <h1>주문이 완료되었습니다</h1>
          <p>결제가 확인되면 상태가 '결제 완료'로 바뀝니다.</p>
        </div>

        <div className="confirmation-content">
          <div className="order-info-section">
            <h2>주문 정보</h2>
            <div className="info-grid">
              <div className="info-item">
                <label>주문번호</label>
                <span className="order-id">#{order.id}</span>
              </div>
              <div className="info-item">
                <label>주문 상태</label>
                <span
                  className="order-status"
                  style={{ color: statusColor[order.status] }}
                >
                  {statusLabel(order.status)}
                </span>
              </div>
              <div className="info-item">
                <label>주문 일시</label>
                <span>{formatDateTime(order.created_at)}</span>
              </div>
              <div className="info-item">
                <label>결제 금액</label>
                <span className="total-amount">
                  {formatWon(order.total_amount)}
                </span>
              </div>
            </div>
          </div>

          <div className="order-items-section">
            <h2>주문 상품</h2>
            <div className="items-list">
              {order.items && order.items.length > 0 ? (
                order.items.map((item, index) => (
                  <div key={index} className="item-row">
                    <div className="item-info">
                      <span className="item-id">{item.name || `상품 #${item.product_id}`}</span>
                      <span className="item-quantity">{formatWon(item.price)} × {item.quantity}</span>
                    </div>
                    <span className="item-price">
                      {formatWon(item.price * item.quantity)}
                    </span>
                  </div>
                ))
              ) : (
                <p>주문 상품이 없습니다</p>
              )}
            </div>
          </div>

          {order.payment_id && (
            <div className="payment-info-section">
              <h2>결제 정보</h2>
              <div className="payment-details">
                <div className="detail-row">
                  <label>결제 번호</label>
                  <span className="payment-id">{order.payment_id}</span>
                </div>
              </div>
            </div>
          )}
        </div>

        <div className="confirmation-actions">
          <button
            onClick={() => navigate('/orders')}
            className="view-orders-button"
          >
            주문 내역 보기
          </button>
          <button
            onClick={() => navigate('/')}
            className="continue-shopping-button"
          >
            쇼핑 계속하기
          </button>
        </div>

        <div className="confirmation-footer">
          <p>
            주문 상태는 '주문 내역'에서 언제든 확인할 수 있습니다.
          </p>
        </div>
      </div>
    </div>
  );
}

export default OrderConfirmation;
