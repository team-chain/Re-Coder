import { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { ordersAPI, productsAPI } from '../api/client';
import '../styles/OrderDetail.css';
import { formatWon } from '../utils/money';
import { statusLabel, formatDateTime } from '../utils/labels';

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

  // 결제 웹훅은 주문 직후 몇 초 뒤에 도착한다 — 결제 대기 동안 잠시 상태를 다시 읽는다.
  useEffect(() => {
    if (!order || (order.status !== 'pending' && order.status !== 'unpaid')) return undefined;
    let tries = 0;
    let stopped = false;
    const timer = setInterval(async () => {
      tries += 1;
      if (tries > 15) {
        clearInterval(timer);
        return;
      }
      try {
        const response = await ordersAPI.getById(id);
        if (!stopped) setOrder(response.data);
      } catch (err) {
        // 다음 주기에 다시 시도한다.
      }
    }, 2000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [id, order?.status]);

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
            name: item.name || `상품 #${item.product_id}`,
            image_url: '/products/placeholder.svg'
          };
        }
      }
      setProducts(productMap);
    } catch (err) {
      console.error('Fetch order detail error:', err);
      if (err.response?.status === 404) {
        setError('주문을 찾을 수 없습니다');
      } else if (err.response?.status === 403) {
        setError('이 주문을 볼 권한이 없습니다');
      } else {
        setError(err.response?.data?.error || '주문 정보를 불러오지 못했습니다');
      }
    } finally {
      setLoading(false);
    }
  };

  const handleCancelOrder = async () => {
    if (!window.confirm('이 주문을 취소할까요?')) {
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
      setCancelError(err.response?.data?.error || '주문을 취소하지 못했습니다');
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
    return statusLabel(status);
  };

  const formatDate = (dateString) => {
    return formatDateTime(dateString);
  };

  const formatPrice = (cents) => {
    return formatWon(cents);
  };

  const canCancelOrder = order && (order.status === 'unpaid' || order.status === 'pending');

  if (loading) {
    return (
      <div className="order-detail-page">
        <div className="loading">주문 정보를 불러오는 중…</div>
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
            ← 주문 내역
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
            ← 주문 내역
          </button>
          <div className="alert alert-danger">
            주문을 찾을 수 없습니다
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
          ← 주문 내역
        </button>

        <div className="order-detail-header">
          <div>
            <h1>주문번호 {order.id}</h1>
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
            주문을 취소했습니다
          </div>
        )}

        {cancelError && (
          <div className="alert alert-danger">
            {cancelError}
          </div>
        )}

        {(order.status === 'pending' || order.status === 'unpaid') && (
          <div className="alert alert-info" role="status">
            결제를 확인하는 중입니다. 확인되면 이 화면에서 '결제 완료'로 바뀝니다.
          </div>
        )}

        <div className="order-detail-content">
          <div className="order-items-section">
            <h2>주문 상품</h2>
            <div className="order-items">
              {order.items && order.items.length > 0 ? (
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
                                  {product ? product.name : (item.name || `상품 #${item.product_id}`)}
                                </p>
                              </div>
                            </div>
                          </td>
                          <td className="quantity-cell">{item.quantity}</td>
                          <td className="price-cell">{formatPrice(item.price)}</td>
                          <td className="subtotal-cell">{formatPrice(subtotal)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              ) : (
                <p>주문 상품이 없습니다</p>
              )}
            </div>
          </div>

          <div className="order-summary-section">
            <div className="order-summary">
              <h2>결제 정보</h2>

              <div className="summary-row">
                <span className="label">상품 금액</span>
                <span className="value">{formatPrice(order.total_amount)}</span>
              </div>

              <div className="summary-row total">
                <span className="label">결제 금액</span>
                <span className="value">{formatPrice(order.total_amount)}</span>
              </div>

              {order.payment_id && (
                <div className="summary-row">
                  <span className="label">결제 번호</span>
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
                  {cancelling ? '취소하는 중…' : '주문 취소'}
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
