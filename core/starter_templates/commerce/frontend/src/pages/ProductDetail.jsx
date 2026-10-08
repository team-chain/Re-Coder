import { useState, useEffect } from 'react';
import { useParams, useNavigate, Link } from 'react-router-dom';
import { productsAPI } from '../api/client';
import { useCart } from '../context/CartContext';
import { useAuth } from '../context/AuthContext';
import '../styles/ProductDetail.css';

function ProductDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { isAuthenticated } = useAuth();
  const { addToCart } = useCart();

  const [product, setProduct] = useState(null);
  const [quantity, setQuantity] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [addingToCart, setAddingToCart] = useState(false);
  const [addSuccess, setAddSuccess] = useState(false);

  useEffect(() => {
    const fetchProduct = async () => {
      setLoading(true);
      setError(null);
      try {
        const response = await productsAPI.getById(id);
        setProduct(response.data);
      } catch (err) {
        setError(err.response?.data?.error || 'Failed to load product');
        console.error('Fetch product error:', err);
      } finally {
        setLoading(false);
      }
    };

    fetchProduct();
  }, [id]);

  const handleQuantityChange = (e) => {
    const value = parseInt(e.target.value) || 1;
    if (value > 0 && value <= (product?.stock || 1)) {
      setQuantity(value);
    }
  };

  const handleAddToCart = async () => {
    if (!isAuthenticated) {
      navigate('/login');
      return;
    }

    setAddingToCart(true);
    setError(null);
    try {
      await addToCart(product.id, quantity);
      setAddSuccess(true);
      setTimeout(() => setAddSuccess(false), 3000);
      setQuantity(1);
    } catch (err) {
      setError(err.message);
    } finally {
      setAddingToCart(false);
    }
  };

  if (loading) {
    return <div className="product-detail loading">Loading...</div>;
  }

  if (error && !product) {
    return <div className="product-detail error">Error: {error}</div>;
  }

  if (!product) {
    return <div className="product-detail error">Product not found</div>;
  }

  const isOutOfStock = product.stock === 0;

  return (
    <div className="product-detail">
      <button className="back-button" onClick={() => navigate('/')}>
        ← Back to Products
      </button>

      <div className="product-detail-container">
        <div className="product-image-section">
          <img
            src={product.image_url || 'https://placehold.co/400x400?text=No+Image'}
            alt={product.name}
            className="product-image"
          />
        </div>

        <div className="product-info-section">
          <h1 className="product-name">{product.name}</h1>

          <div className="product-price">${product.price.toFixed(2)}</div>

          <div className="product-stock">
            {isOutOfStock ? (
              <span className="out-of-stock">Out of Stock</span>
            ) : (
              <span className="in-stock">In Stock ({product.stock} available)</span>
            )}
          </div>

          <div className="product-description">
            <h3>Description</h3>
            <p>{product.description || 'No description available'}</p>
          </div>

          {error && <div className="error-message">{error}</div>}
          {addSuccess && (
            <div className="success-message">Added to cart successfully!</div>
          )}

          <div className="product-actions">
            <div className="quantity-selector">
              <label htmlFor="quantity">Quantity:</label>
              <select
                id="quantity"
                value={quantity}
                onChange={handleQuantityChange}
                disabled={isOutOfStock}
              >
                {Array.from({ length: Math.min(product.stock, 10) }, (_, i) => (
                  <option key={i + 1} value={i + 1}>
                    {i + 1}
                  </option>
                ))}
              </select>
            </div>

            <button
              className="add-to-cart-button"
              onClick={handleAddToCart}
              disabled={isOutOfStock || addingToCart}
            >
              {addingToCart ? 'Adding...' : 'Add to Cart'}
            </button>
          </div>

          {!isAuthenticated && (
            <p className="login-prompt">
              Please <Link to="/login">log in</Link> to add items to your cart.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

export default ProductDetail;
