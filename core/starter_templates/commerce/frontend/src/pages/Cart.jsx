import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useCart } from '../context/CartContext';
import { productsAPI } from '../api/client';
import '../styles/Cart.css';

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
    return <div className="cart loading">Loading cart...</div>;
  }

  if (error) {
    return <div className="cart error">Error: {error}</div>;
  }

  if (items.length === 0) {
    return (
      <div className="cart empty">
        <div className="empty-cart-message">
          <h2>Your cart is empty</h2>
          <p>Start shopping to add items to your cart.</p>
          <button
            className="continue-shopping-button"
            onClick={() => navigate('/')}
          >
            Continue Shopping
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="cart">
      <h1>Shopping Cart</h1>

      <div className="cart-container">
        <div className="cart-items-section">
          <table className="cart-table">
            <thead>
              <tr>
                <th>Product</th>
                <th>Price</th>
                <th>Quantity</th>
                <th>Subtotal</th>
                <th>Action</th>
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
                            src={product.image_url || 'https://placehold.co/80x80?text=No+Image'}
                            alt={product.name}
                            className="product-thumbnail"
                          />
                        )}
                        <div>
                          <div className="product-name">
                            {product?.name || `Product ${item.product_id}`}
                          </div>
                          {product?.description && (
                            <div className="product-description">
                              {product.description.substring(0, 50)}...
                            </div>
                          )}
                        </div>
                      </div>
                    </td>
                    <td className="price-cell">${item.price.toFixed(2)}</td>
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
                    <td className="subtotal-cell">${subtotal.toFixed(2)}</td>
                    <td className="action-cell">
                      <button
                        className="remove-button"
                        onClick={() => handleRemove(item.product_id)}
                        disabled={isUpdating}
                      >
                        Remove
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
            <h2>Order Summary</h2>

            <div className="summary-row">
              <span>Subtotal:</span>
              <span>${total.toFixed(2)}</span>
            </div>

            <div className="summary-divider"></div>

            <div className="summary-row total">
              <span>Total:</span>
              <span>${total.toFixed(2)}</span>
            </div>

            <button
              className="checkout-button"
              onClick={handleCheckout}
              disabled={items.length === 0 || loadingProducts}
            >
              Proceed to Checkout
            </button>

            <button
              className="continue-shopping-button"
              onClick={() => navigate('/')}
            >
              Continue Shopping
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default Cart;
