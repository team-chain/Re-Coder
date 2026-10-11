import { createContext, useContext, useState, useEffect } from 'react';
import { useAuth } from './AuthContext';

const CartContext = createContext();

export function CartProvider({ children }) {
  const { token, isAuthenticated, logout } = useAuth();
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Fetch cart when authenticated
  useEffect(() => {
    if (isAuthenticated && token) {
      fetchCart();
    } else {
      setItems([]);
      setTotal(0);
    }
  }, [isAuthenticated, token]);

  const fetchCart = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch('/api/cart', {
        headers: {
          'Authorization': `Bearer ${token}`
        }
      });

      if (response.status === 401) {
        logout();
        throw new Error('로그인이 만료되었습니다. 다시 로그인하세요');
      }
      if (!response.ok) {
        throw new Error('장바구니를 불러오지 못했습니다');
      }

      const data = await response.json();
      setItems(data.items || []);
      setTotal(data.total || 0);
    } catch (err) {
      setError(err.message);
      console.error('Fetch cart error:', err);
    } finally {
      setLoading(false);
    }
  };

  const addToCart = async (productId, quantity) => {
    if (!isAuthenticated || !token) {
      setError('로그인 후 장바구니에 담을 수 있습니다');
      throw new Error('로그인이 필요합니다');
    }

    setError(null);
    try {
      const response = await fetch('/api/cart', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`
        },
        body: JSON.stringify({ product_id: productId, quantity })
      });

      if (response.status === 401) {
        logout();
        throw new Error('로그인이 만료되었습니다. 다시 로그인하세요');
      }
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || '장바구니에 담지 못했습니다');
      }

      // Refresh cart
      await fetchCart();
    } catch (err) {
      setError(err.message);
      throw err;
    }
  };

  const updateCartItem = async (productId, quantity) => {
    if (!isAuthenticated || !token) {
      setError('로그인 후 수량을 바꿀 수 있습니다');
      throw new Error('로그인이 필요합니다');
    }

    setError(null);
    try {
      const response = await fetch(`/api/cart/${productId}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`
        },
        body: JSON.stringify({ quantity })
      });

      if (response.status === 401) {
        logout();
        throw new Error('로그인이 만료되었습니다. 다시 로그인하세요');
      }
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || '수량을 바꾸지 못했습니다');
      }

      // Refresh cart
      await fetchCart();
    } catch (err) {
      setError(err.message);
      throw err;
    }
  };

  const removeFromCart = async (productId) => {
    if (!isAuthenticated || !token) {
      setError('로그인 후 상품을 뺄 수 있습니다');
      throw new Error('로그인이 필요합니다');
    }

    setError(null);
    try {
      const response = await fetch(`/api/cart/${productId}`, {
        method: 'DELETE',
        headers: {
          'Authorization': `Bearer ${token}`
        }
      });

      if (response.status === 401) {
        logout();
        throw new Error('로그인이 만료되었습니다. 다시 로그인하세요');
      }
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || '상품을 빼지 못했습니다');
      }

      // Refresh cart
      await fetchCart();
    } catch (err) {
      setError(err.message);
      throw err;
    }
  };

  const clearCart = () => {
    setItems([]);
    setTotal(0);
  };

  const getCartItemCount = () => {
    return items.reduce((sum, item) => sum + item.quantity, 0);
  };

  return (
    <CartContext.Provider
      value={{
        items,
        total,
        loading,
        error,
        fetchCart,
        addToCart,
        updateCartItem,
        removeFromCart,
        clearCart,
        getCartItemCount
      }}
    >
      {children}
    </CartContext.Provider>
  );
}

export function useCart() {
  const context = useContext(CartContext);
  if (!context) {
    throw new Error('useCart must be used within CartProvider');
  }
  return context;
}
