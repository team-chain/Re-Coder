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
        throw new Error('Please log in again');
      }
      if (!response.ok) {
        throw new Error('Failed to fetch cart');
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
      setError('Please login to add items to cart');
      throw new Error('Not authenticated');
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
        throw new Error('Please log in again');
      }
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to add to cart');
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
      setError('Please login to update cart');
      throw new Error('Not authenticated');
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
        throw new Error('Please log in again');
      }
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to update cart');
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
      setError('Please login to remove items from cart');
      throw new Error('Not authenticated');
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
        throw new Error('Please log in again');
      }
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to remove from cart');
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
