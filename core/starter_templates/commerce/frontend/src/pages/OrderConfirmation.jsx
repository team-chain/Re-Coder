import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { ordersAPI } from '../api/client';
import '../styles/OrderConfirmation.css';

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
        setError(err.response?.data?.error || 'Failed to fetch order');
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
        <div className="confirmation-loading">Loading order details...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="confirmation-container">
        <div className="confirmation-error">
          <p>{error}</p>
          <button onClick={() => navigate('/')} className="back-button">
            Back to Home
          </button>
        </div>
      </div>
    );
  }

  if (!order) {
    return (
      <div className="confirmation-container">
        <div className="confirmation-error">
          <p>Order not found</p>
          <button onClick={() => navigate('/')} className="back-button">
            Back to Home
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
          <h1>Order Confirmed</h1>
          <p>Thank you for your purchase!</p>
        </div>

        <div className="confirmation-content">
          <div className="order-info-section">
            <h2>Order Details</h2>
            <div className="info-grid">
              <div className="info-item">
                <label>Order ID</label>
                <span className="order-id">#{order.id}</span>
              </div>
              <div className="info-item">
                <label>Status</label>
                <span
                  className="order-status"
                  style={{ color: statusColor[order.status] }}
                >
                  {order.status.toUpperCase()}
                </span>
              </div>
              <div className="info-item">
                <label>Order Date</label>
                <span>{new Date(order.created_at).toLocaleDateString()}</span>
              </div>
              <div className="info-item">
                <label>Total Amount</label>
                <span className="total-amount">
                  ${(order.total_amount / 100).toFixed(2)}
                </span>
              </div>
            </div>
          </div>

          <div className="order-items-section">
            <h2>Items</h2>
            <div className="items-list">
              {order.items && order.items.length > 0 ? (
                order.items.map((item, index) => (
                  <div key={index} className="item-row">
                    <div className="item-info">
                      <span className="item-id">Product #{item.product_id}</span>
                      <span className="item-quantity">Qty: {item.quantity}</span>
                    </div>
                    <span className="item-price">
                      ${(item.price / 100).toFixed(2)}
                    </span>
                  </div>
                ))
              ) : (
                <p>No items in order</p>
              )}
            </div>
          </div>

          {order.payment_id && (
            <div className="payment-info-section">
              <h2>Payment Information</h2>
              <div className="payment-details">
                <div className="detail-row">
                  <label>Payment ID</label>
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
            View All Orders
          </button>
          <button
            onClick={() => navigate('/')}
            className="continue-shopping-button"
          >
            Continue Shopping
          </button>
        </div>

        <div className="confirmation-footer">
          <p>
            A confirmation email has been sent to your registered email address.
          </p>
        </div>
      </div>
    </div>
  );
}

export default OrderConfirmation;
