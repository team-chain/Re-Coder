import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { ordersAPI } from '../api/client';
import '../styles/Orders.css';
import { formatWon } from '../utils/money';
import { statusLabel, formatDateTime } from '../utils/labels';

function Orders() {
  const navigate = useNavigate();
  const { isAuthenticated } = useAuth();
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!isAuthenticated) {
      navigate('/login');
      return;
    }

    fetchOrders();
  }, [isAuthenticated, navigate]);

  const fetchOrders = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await ordersAPI.getAll();
      setOrders(response.data.orders || []);
    } catch (err) {
      console.error('Fetch orders error:', err);
      setError(err.response?.data?.error || '주문 내역을 불러오지 못했습니다');
    } finally {
      setLoading(false);
    }
  };

  const getStatusBadgeClass = (status) => {
    switch (status) {
      case 'paid':
        return 'badge-success';
      case 'pending':
        return 'badge-warning';
      case 'failed':
        return 'badge-danger';
      case 'cancelled':
        return 'badge-secondary';
      case 'unpaid':
        return 'badge-info';
      default:
        return 'badge-secondary';
    }
  };

  const getStatusLabel = (status) => {
    return statusLabel(status);
  };

  const formatDate = (dateString) => {
    return formatDateTime(dateString);
  };

  const formatPrice = (cents) => {
    return formatWon(cents);
  };

  if (loading) {
    return (
      <div className="orders-page">
        <div className="loading">주문 내역을 불러오는 중…</div>
      </div>
    );
  }

  return (
    <div className="orders-page">
      <div className="orders-container">
        <h1>주문 내역</h1>

        {error && (
          <div className="alert alert-danger">
            {error}
          </div>
        )}

        {orders.length === 0 ? (
          <div className="empty-state">
            <p>아직 주문한 상품이 없습니다.</p>
            <button
              className="btn btn-primary"
              onClick={() => navigate('/')}
            >
              쇼핑하러 가기
            </button>
          </div>
        ) : (
          <div className="orders-list">
            {orders.map((order) => (
              <div key={order.id} className="order-card">
                <div className="order-header">
                  <div className="order-info">
                    <h3>주문번호 {order.id}</h3>
                    <p className="order-date">{formatDate(order.created_at)}</p>
                  </div>
                  <div className="order-status">
                    <span className={`badge ${getStatusBadgeClass(order.status)}`}>
                      {getStatusLabel(order.status)}
                    </span>
                  </div>
                </div>

                <div className="order-body">
                  <div className="order-amount">
                    <span className="label">결제 금액</span>
                    <span className="amount">{formatPrice(order.total_amount)}</span>
                  </div>
                </div>

                <div className="order-footer">
                  <button
                    className="btn btn-secondary"
                    onClick={() => navigate(`/orders/${order.id}`)}
                  >
                    주문 상세
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default Orders;
