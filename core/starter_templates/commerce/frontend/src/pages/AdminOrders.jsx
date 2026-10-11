import { useState, useEffect } from 'react';
import { useAuth } from '../context/AuthContext';
import { adminAPI } from '../api/client';
import '../styles/AdminOrders.css';
import { formatWon } from '../utils/money';
import { statusLabel, formatDateTime } from '../utils/labels';

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
        throw new Error('주문 목록을 불러오지 못했습니다');
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
        throw new Error('주문 정보를 불러오지 못했습니다');
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
    return formatDateTime(dateString);
  };

  const currentPage = Math.floor(pagination.offset / pagination.limit) + 1;
  const totalPages = Math.ceil(pagination.total / pagination.limit);

  return (
    <div className="admin-orders">
      <div className="admin-header">
        <h1>주문 관리</h1>
        <div className="order-stats">
          <span>전체 주문 {pagination.total.toLocaleString('ko-KR')}건</span>
        </div>
      </div>

      {error && <div className="alert alert-error">{error}</div>}

      {loading ? (
        <div className="loading">주문 목록을 불러오는 중…</div>
      ) : orders.length === 0 ? (
        <div className="empty-state">아직 주문이 없습니다</div>
      ) : (
        <>
          <div className="orders-table-container">
            <table className="orders-table">
              <thead>
                <tr>
                  <th>주문번호</th>
                  <th>회원 번호</th>
                  <th>결제 금액</th>
                  <th>상태</th>
                  <th>주문 일시</th>
                  <th><span className="sr-only">관리</span></th>
                </tr>
              </thead>
              <tbody>
                {orders.map(order => (
                  <tr key={order.id} className={selectedOrder === order.id ? 'expanded' : ''}>
                    <td>#{order.id}</td>
                    <td>{order.user_id}</td>
                    <td>{formatWon(order.total_amount)}</td>
                    <td>
                      <span className={`status-badge ${getStatusBadgeClass(order.status)}`}>
                        {statusLabel(order.status)}
                      </span>
                    </td>
                    <td>{formatDate(order.created_at)}</td>
                    <td>
                      <button
                        className="btn btn-sm btn-secondary"
                        onClick={() => handleViewDetails(order.id)}
                      >
                        {selectedOrder === order.id ? '접기' : '상세 보기'}
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
                <h2>주문번호 {orderDetails.id}</h2>
                <button
                  className="btn btn-sm btn-secondary"
                  onClick={() => {
                    setSelectedOrder(null);
                    setOrderDetails(null);
                  }}
                >
                  닫기
                </button>
              </div>

              {detailsLoading ? (
                <div className="loading">주문 정보를 불러오는 중…</div>
              ) : (
                <div className="order-details-content">
                  <div className="details-section">
                    <h3>주문 정보</h3>
                    <div className="details-grid">
                      <div className="detail-item">
                        <label>주문번호</label>
                        <span>{orderDetails.id}</span>
                      </div>
                      <div className="detail-item">
                        <label>회원 번호</label>
                        <span>{orderDetails.user_id}</span>
                      </div>
                      <div className="detail-item">
                        <label>상태</label>
                        <span className={`status-badge ${getStatusBadgeClass(orderDetails.status)}`}>
                          {statusLabel(orderDetails.status)}
                        </span>
                      </div>
                      <div className="detail-item">
                        <label>결제 번호</label>
                        <span className="payment-id">{orderDetails.payment_id || '없음'}</span>
                      </div>
                      <div className="detail-item">
                        <label>결제 금액</label>
                        <span className="total-amount">{formatWon(orderDetails.total_amount)}</span>
                      </div>
                      <div className="detail-item">
                        <label>주문 일시</label>
                        <span>{formatDate(orderDetails.created_at)}</span>
                      </div>
                    </div>
                  </div>

                  <div className="details-section">
                    <h3>주문 상품</h3>
                    {orderDetails.items && orderDetails.items.length > 0 ? (
                      <table className="items-table">
                        <thead>
                          <tr>
                            <th>상품</th>
                            <th>수량</th>
                            <th>판매가</th>
                            <th>합계</th>
                          </tr>
                        </thead>
                        <tbody>
                          {orderDetails.items.map((item, index) => (
                            <tr key={index}>
                              <td>{item.name || `상품 #${item.product_id}`}</td>
                              <td>{item.quantity}</td>
                              <td>{formatWon(item.price)}</td>
                              <td>{formatWon(item.price * item.quantity)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    ) : (
                      <p>주문 상품이 없습니다</p>
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
              이전
            </button>
            <span className="page-info">
              {currentPage} / {Math.max(totalPages, 1)} 페이지
            </span>
            <button
              className="btn btn-secondary"
              onClick={handleNextPage}
              disabled={pagination.offset + pagination.limit >= pagination.total}
            >
              다음
            </button>
          </div>
        </>
      )}
    </div>
  );
}

export default AdminOrders;