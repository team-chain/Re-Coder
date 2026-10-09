import { useState, useEffect } from 'react';
import { useAuth } from '../context/AuthContext';
import { adminAPI } from '../api/client';
import '../styles/AdminOrders.css';

function AdminOrders() {
  const { token } = useAuth();
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [selectedOrder, setSelectedOrder] = useState(null);
  const [orderDetails, setOrderDetails] = useState(null);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [pagination, setPagination] = useState({
    limit: 20,
    offset: 0,
    total: 0
  });

  useEffect(() => {
    fetchOrders();
  }, [pagination.offset]);

  const fetchOrders = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(
        `/api/admin/orders?limit=${pagination.limit}&offset=${pagination.offset}`,
        {
          headers: {
            'Authorization': `Bearer ${token}`
          }
        }
      );

      if (!response.ok) {
        throw new Error('Failed to fetch orders');
      }

      const data = await response.json();
      setOrders(data.orders || []);
      setPagination(prev => ({
        ...prev,
        total: data.total || 0
      }));
    } catch (err) {
      setError(err.message);
      console.error('Fetch orders error:', err);
    } finally {
      setLoading(false);
    }
  };

  const fetchOrderDetails = async (orderId) => {
    setDetailsLoading(true);
    setError(null);
    try {
      const response = await fetch(`/api/admin/orders/${orderId}`, {
        headers: {
          'Authorization': `Bearer ${token}`
        }
      });

      if (!response.ok) {
        throw new Error('Failed to fetch order details');
      }

      const data = await response.json();
      setOrderDetails(data);
      setSelectedOrder(orderId);
    } catch (err) {
      setError(err.message);
      console.error('Fetch order details error:', err);
    } finally {
      setDetailsLoading(false);
    }
  };

  const handleViewDetails = (orderId) => {
    if (selectedOrder === orderId) {
      setSelectedOrder(null);
      setOrderDetails(null);
    } else {
      fetchOrderDetails(orderId);
    }
  };

  const handlePreviousPage = () => {
    if (pagination.offset > 0) {
      setPagination(prev => ({
        ...prev,
        offset: Math.max(0, prev.offset - prev.limit)
      }));
    }
  };

  const handleNextPage = () => {
    if (pagination.offset + pagination.limit < pagination.total) {
      setPagination(prev => ({
        ...prev,
        offset: prev.offset + prev.limit
      }));
    }
  };

  const getStatusBadgeClass = (status) => {
    switch (status) {
      case 'paid':
        return 'status-paid';
      case 'pending':
        return 'status-pending';
      case 'failed':
        return 'status-failed';
      case 'cancelled':
        return 'status-cancelled';
      case 'unpaid':
        return 'status-unpaid';
      default:
        return 'status-unknown';
    }
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

  const currentPage = Math.floor(pagination.offset / pagination.limit) + 1;
  const totalPages = Math.ceil(pagination.total / pagination.limit);

  return (
    <div className="admin-orders">
      <div className="admin-header">
        <h1>Order Management</h1>
        <div className="order-stats">
          <span>Total Orders: {pagination.total}</span>
        </div>
      </div>

      {error && <div className="alert alert-error">{error}</div>}

      {loading ? (
        <div className="loading">Loading orders...</div>
      ) : orders.length === 0 ? (
        <div className="empty-state">No orders found</div>
      ) : (
        <>
          <div className="orders-table-container">
            <table className="orders-table">
              <thead>
                <tr>
                  <th>Order ID</th>
                  <th>User ID</th>
                  <th>Total Amount</th>
                  <th>Status</th>
                  <th>Created At</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {orders.map(order => (
                  <tr key={order.id} className={selectedOrder === order.id ? 'expanded' : ''}>
                    <td>#{order.id}</td>
                    <td>{order.user_id}</td>
                    <td>${(order.total_amount / 100).toFixed(2)}</td>
                    <td>
                      <span className={`status-badge ${getStatusBadgeClass(order.status)}`}>
                        {order.status.charAt(0).toUpperCase() + order.status.slice(1)}
                      </span>
                    </td>
                    <td>{formatDate(order.created_at)}</td>
                    <td>
                      <button
                        className="btn btn-sm btn-secondary"
                        onClick={() => handleViewDetails(order.id)}
                      >
                        {selectedOrder === order.id ? 'Hide' : 'View'} Details
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {selectedOrder && orderDetails && (
            <div className="order-details-container">
              <div className="order-details-header">
                <h2>Order #{orderDetails.id} Details</h2>
                <button
                  className="btn btn-sm btn-secondary"
                  onClick={() => {
                    setSelectedOrder(null);
                    setOrderDetails(null);
                  }}
                >
                  Close
                </button>
              </div>

              {detailsLoading ? (
                <div className="loading">Loading order details...</div>
              ) : (
                <div className="order-details-content">
                  <div className="details-section">
                    <h3>Order Information</h3>
                    <div className="details-grid">
                      <div className="detail-item">
                        <label>Order ID:</label>
                        <span>{orderDetails.id}</span>
                      </div>
                      <div className="detail-item">
                        <label>User ID:</label>
                        <span>{orderDetails.user_id}</span>
                      </div>
                      <div className="detail-item">
                        <label>Status:</label>
                        <span className={`status-badge ${getStatusBadgeClass(orderDetails.status)}`}>
                          {orderDetails.status.charAt(0).toUpperCase() + orderDetails.status.slice(1)}
                        </span>
                      </div>
                      <div className="detail-item">
                        <label>Payment ID:</label>
                        <span className="payment-id">{orderDetails.payment_id || 'N/A'}</span>
                      </div>
                      <div className="detail-item">
                        <label>Total Amount:</label>
                        <span className="total-amount">${(orderDetails.total_amount / 100).toFixed(2)}</span>
                      </div>
                      <div className="detail-item">
                        <label>Created At:</label>
                        <span>{formatDate(orderDetails.created_at)}</span>
                      </div>
                    </div>
                  </div>

                  <div className="details-section">
                    <h3>Order Items</h3>
                    {orderDetails.items && orderDetails.items.length > 0 ? (
                      <table className="items-table">
                        <thead>
                          <tr>
                            <th>Product ID</th>
                            <th>Quantity</th>
                            <th>Price (USD)</th>
                            <th>Subtotal</th>
                          </tr>
                        </thead>
                        <tbody>
                          {orderDetails.items.map((item, index) => (
                            <tr key={index}>
                              <td>{item.product_id}</td>
                              <td>{item.quantity}</td>
                              <td>${(item.price / 100).toFixed(2)}</td>
                              <td>${((item.price * item.quantity) / 100).toFixed(2)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    ) : (
                      <p>No items in this order</p>
                    )}
                  </div>
                </div>
              )}
            </div>
          )}

          <div className="pagination">
            <button
              className="btn btn-secondary"
              onClick={handlePreviousPage}
              disabled={pagination.offset === 0}
            >
              Previous
            </button>
            <span className="page-info">
              Page {currentPage} of {totalPages}
            </span>
            <button
              className="btn btn-secondary"
              onClick={handleNextPage}
              disabled={pagination.offset + pagination.limit >= pagination.total}
            >
              Next
            </button>
          </div>
        </>
      )}
    </div>
  );
}

export default AdminOrders;