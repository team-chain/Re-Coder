import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useCart } from '../context/CartContext';
import { productsAPI } from '../api/client';
import '../styles/Cart.css';
import { formatWon } from '../utils/money';

function Cart() {
  const navigate = useNavigate();
  const { items, total, loading, error, updateCartItem, removeFromCart, fetchCart } = useCart();
  const [products, setProducts] = useState({});
  const [loadingProducts, setLoadingProducts] = useState(false);
  const [updatingItems, setUpdatingItems] = useState({});

  // Fetch product details for cart items
  useEffect(() => {
    const fetchProductDetails = async () => {
      if (items.length === 0) {
        setProducts({});
        return;
      }

      setLoadingProducts(true);
      try {
        const productMap = {};
        for (const item of items) {
          if (!productMap[item.product_id]) {
            const response = await productsAPI.getById(item.product_id);
            productMap[item.product_id] = response.data;
          }
        }
        setProducts(productMap);
      } catch (err) {
        console.error('Failed to fetch product details:', err);
      } finally {
        setLoadingProducts(false);
      }
    };

    fetchProductDetails();
  }, [items]);

  const handleQuantityChange = async (productId, newQuantity) => {
    if (newQuantity <= 0) return;

    setUpdatingItems(prev => ({ ...prev, [productId]: true }));
    try {
      await updateCartItem(productId, newQuantity);
    } catch (err) {
      console.error('Failed to update quantity:', err);
    } finally {
      setUpdatingItems(prev => ({ ...prev, [productId]: false }));
    }
  };

  const handleRemove = async (productId) => {
    try {
      await removeFromCart(productId);
    } catch (err) {
      console.error('Failed to remove item:', err);
    }
  };

  const handleCheckout = () => {
    if (items.length === 0) {
      return;
    }
    navigate('/checkout');
  };

  if (loading) {
    return <div className="cart loading">장바구니를 불러오는 중…</div>;
  }

  if (error) {
    return <div className="cart error">{error}</div>;
  }

  if (items.length === 0) {
    return (
      <div className="cart empty">
        <div className="empty-cart-message">
          <h2>장바구니가 비어 있습니다</h2>
          <p>마음에 드는 상품을 담아 보세요.</p>
          <button
            className="continue-shopping-button"
            onClick={() => navigate('/')}
          >
            쇼핑 계속하기
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="cart">
      <h1>장바구니</h1>

      <div className="cart-container">
        <div className="cart-items-section">
          <table className="cart-table">
            <thead>
              <tr>
                <th>상품</th>
                <th>판매가</th>
                <th>수량</th>
                <th>합계</th>
                <th><span className="sr-only">삭제</span></th>
              </tr>
            </thead>
            <tbody>
              {items.map(item => {
                const product = products[item.product_id];
                const subtotal = item.price * item.quantity;
                const isUpdating = updatingItems[item.product_id];

                return (
                  <tr key={item.product_id} className="cart-item">
                    <td className="product-name-cell">
                      <div className="product-info">
                        {product && (
                          <img
                            src={product.image_url || '/products/placeholder.svg'}
                            alt={product.name}
                            className="product-thumbnail"
                          />
                        )}
                        <div>
                          <div className="product-name">
                            {product?.name || item.name || `상품 #${item.product_id}`}
                          </div>
                          {product?.description && (
                            <div className="product-description">
                              {product.description.length > 40 ? `${product.description.substring(0, 40)}…` : product.description}
                            </div>
                          )}
                        </div>
                      </div>
                    </td>
                    <td className="price-cell">{formatWon(item.price)}</td>
                    <td className="quantity-cell">
                      <div className="quantity-controls">
                        <button
                          className="qty-btn"
                          onClick={() => handleQuantityChange(item.product_id, item.quantity - 1)}
                          disabled={isUpdating || item.quantity <= 1}
                        >
                          −
                        </button>
                        <input
                          type="number"
                          value={item.quantity}
                          onChange={(e) => {
                            const val = parseInt(e.target.value) || 1;
                            if (val > 0) {
                              handleQuantityChange(item.product_id, val);
                            }
                          }}
                          disabled={isUpdating}
                          className="qty-input"
                        />
                        <button
                          className="qty-btn"
                          onClick={() => handleQuantityChange(item.product_id, item.quantity + 1)}
                          disabled={isUpdating || (product && item.quantity >= product.stock)}
                        >
                          +
                        </button>
                      </div>
                    </td>
                    <td className="subtotal-cell">{formatWon(subtotal)}</td>
                    <td className="action-cell">
                      <button
                        className="remove-button"
                        onClick={() => handleRemove(item.product_id)}
                        disabled={isUpdating}
                      >
                        삭제
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        <div className="cart-summary-section">
          <div className="cart-summary">
            <h2>결제 예정 금액</h2>

            <div className="summary-row">
              <span>상품 금액</span>
              <span>{formatWon(total)}</span>
            </div>

            <div className="summary-row">
              <span>배송비</span>
              <span>무료</span>
            </div>

            <div className="summary-divider"></div>

            <div className="summary-row total">
              <span>최종 결제 금액</span>
              <span>{formatWon(total)}</span>
            </div>

            <button
              className="checkout-button"
              onClick={handleCheckout}
              disabled={items.length === 0 || loadingProducts}
            >
              {formatWon(total)} 주문하기
            </button>

            <button
              className="continue-shopping-button"
              onClick={() => navigate('/')}
            >
              쇼핑 계속하기
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default Cart;
