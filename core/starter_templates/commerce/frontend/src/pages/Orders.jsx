import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { ordersAPI } from '../api/client';
import '../styles/Orders.css';

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
      setError(err.response?.data?.error || 'Failed to fetch orders');
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
    const labels = {
      paid: 'Paid',
      pending: 'Pending',
      failed: 'Failed',
      cancelled: 'Cancelled',
      unpaid: 'Unpaid'
    };
    return labels[status] || status;
  };

  const formatDate = (dateString) => {
    return new Date(dateString).toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    });
  };

  const formatPrice = (cents) => {
    return (cents / 100).toFixed(2);
  };

  if (loading) {
    return (
      <div className="orders-page">
        <div className="loading">Loading orders...</div>
      </div>
    );
  }

  return (
    <div className="orders-page">
      <div className="orders-container">
        <h1>My Orders</h1>

        {error && (
          <div className="alert alert-danger">
            {error}
          </div>
        )}

        {orders.length === 0 ? (
          <div className="empty-state">
            <p>You haven't placed any orders yet.</p>
            <button
              className="btn btn-primary"
              onClick={() => navigate('/')}
            >
              Continue Shopping
            </button>
          </div>
        ) : (
          <div className="orders-list">
            {orders.map((order) => (
              <div key={order.id} className="order-card">
                <div className="order-header">
                  <div className="order-info">
                    <h3>Order #{order.id}</h3>
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
                    <span className="label">Total Amount:</span>
                    <span className="amount">${formatPrice(order.total_amount)}</span>
                  </div>
                </div>

                <div className="order-footer">
                  <button
                    className="btn btn-secondary"
                    onClick={() => navigate(`/orders/${order.id}`)}
                  >
                    View Details
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
