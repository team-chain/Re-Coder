import { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { ordersAPI, productsAPI } from '../api/client';
import '../styles/OrderDetail.css';

function OrderDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { isAuthenticated } = useAuth();
  const [order, setOrder] = useState(null);
  const [products, setProducts] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState(null);
  const [cancelSuccess, setCancelSuccess] = useState(false);

  useEffect(() => {
    if (!isAuthenticated) {
      navigate('/login');
      return;
    }

    fetchOrderDetail();
  }, [id, isAuthenticated, navigate]);

  const fetchOrderDetail = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await ordersAPI.getById(id);
      setOrder(response.data);

      // Fetch product details for each item
      const productMap = {};
      for (const item of response.data.items) {
        try {
          const productResponse = await productsAPI.getById(item.product_id);
          productMap[item.product_id] = productResponse.data;
        } catch (err) {
          console.error(`Failed to fetch product ${item.product_id}:`, err);
          productMap[item.product_id] = {
            id: item.product_id,
            name: `Product #${item.product_id}`,
            image_url: 'https://placehold.co/100x100?text=Product'
          };
        }
      }
      setProducts(productMap);
    } catch (err) {
      console.error('Fetch order detail error:', err);
      if (err.response?.status === 404) {
        setError('Order not found');
      } else if (err.response?.status === 403) {
        setError('You do not have access to this order');
      } else {
        setError(err.response?.data?.error || 'Failed to fetch order details');
      }
    } finally {
      setLoading(false);
    }
  };

  const handleCancelOrder = async () => {
    if (!window.confirm('Are you sure you want to cancel this order?')) {
      return;
    }

    setCancelling(true);
    setCancelError(null);
    setCancelSuccess(false);

    try {
      await ordersAPI.cancel(id);
      setCancelSuccess(true);
      // Refresh order details
      setTimeout(() => {
        fetchOrderDetail();
      }, 1000);
    } catch (err) {
      console.error('Cancel order error:', err);
      setCancelError(err.response?.data?.error || 'Failed to cancel order');
    } finally {
      setCancelling(false);
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

  const canCancelOrder = order && (order.status === 'unpaid' || order.status === 'pending');

  if (loading) {
    return (
      <div className="order-detail-page">
        <div className="loading">Loading order details...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="order-detail-page">
        <div className="order-detail-container">
          <button
            className="btn btn-secondary"
            onClick={() => navigate('/orders')}
          >
            ← Back to Orders
          </button>
          <div className="alert alert-danger">
            {error}
          </div>
        </div>
      </div>
    );
  }

  if (!order) {
    return (
      <div className="order-detail-page">
        <div className="order-detail-container">
          <button
            className="btn btn-secondary"
            onClick={() => navigate('/orders')}
          >
            ← Back to Orders
          </button>
          <div className="alert alert-danger">
            Order not found
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="order-detail-page">
      <div className="order-detail-container">
        <button
          className="btn btn-secondary"
          onClick={() => navigate('/orders')}
        >
          ← Back to Orders
        </button>

        <div className="order-detail-header">
          <div>
            <h1>Order #{order.id}</h1>
            <p className="order-date">{formatDate(order.created_at)}</p>
          </div>
          <div className="order-status-section">
            <span className={`badge ${getStatusBadgeClass(order.status)}`}>
              {getStatusLabel(order.status)}
            </span>
          </div>
        </div>

        {cancelSuccess && (
          <div className="alert alert-success">
            Order cancelled successfully
          </div>
        )}

        {cancelError && (
          <div className="alert alert-danger">
            {cancelError}
          </div>
        )}

        <div className="order-detail-content">
          <div className="order-items-section">
            <h2>Order Items</h2>
            <div className="order-items">
              {order.items && order.items.length > 0 ? (
                <table className="items-table">
                  <thead>
                    <tr>
                      <th>Product</th>
                      <th>Quantity</th>
                      <th>Unit Price</th>
                      <th>Subtotal</th>
                    </tr>
                  </thead>
                  <tbody>
                    {order.items.map((item, index) => {
                      const product = products[item.product_id];
                      const subtotal = item.price * item.quantity;
                      return (
                        <tr key={index}>
                          <td>
                            <div className="product-cell">
                              {product && product.image_url && (
                                <img
                                  src={product.image_url}
                                  alt={product.name}
                                  className="product-image"
                                />
                              )}
                              <div>
                                <p className="product-name">
                                  {product ? product.name : `Product #${item.product_id}`}
                                </p>
                              </div>
                            </div>
                          </td>
                          <td className="quantity-cell">{item.quantity}</td>
                          <td className="price-cell">${formatPrice(item.price)}</td>
                          <td className="subtotal-cell">${formatPrice(subtotal)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              ) : (
                <p>No items in this order</p>
              )}
            </div>
          </div>

          <div className="order-summary-section">
            <div className="order-summary">
              <h2>Order Summary</h2>

              <div className="summary-row">
                <span className="label">Subtotal:</span>
                <span className="value">${formatPrice(order.total_amount)}</span>
              </div>

              <div className="summary-row total">
                <span className="label">Total:</span>
                <span className="value">${formatPrice(order.total_amount)}</span>
              </div>

              {order.payment_id && (
                <div className="summary-row">
                  <span className="label">Payment ID:</span>
                  <span className="value payment-id">{order.payment_id}</span>
                </div>
              )}
            </div>

            {canCancelOrder && (
              <div className="order-actions">
                <button
                  className="btn btn-danger"
                  onClick={handleCancelOrder}
                  disabled={cancelling}
                >
                  {cancelling ? 'Cancelling...' : 'Cancel Order'}
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default OrderDetail;
