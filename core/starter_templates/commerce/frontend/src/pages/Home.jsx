import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { productsAPI } from '../api/client';
import { useCart } from '../context/CartContext';
import '../styles/Home.css';

function Home() {
  const [products, setProducts] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const { addToCart } = useCart();
  const [addingToCart, setAddingToCart] = useState({});
  const [cartError, setCartError] = useState(null);

  const LIMIT = 20;

  useEffect(() => {
    fetchProducts();
  }, [query, offset]);

  const fetchProducts = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await productsAPI.getAll(LIMIT, offset, query || null);
      const { products: newProducts, total: totalCount } = response.data;

      if (offset === 0) {
        setProducts(newProducts);
      } else {
        setProducts((prev) => [...prev, ...newProducts]);
      }

      setTotal(totalCount);
      setHasMore(offset + LIMIT < totalCount);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to load products');
      console.error('Fetch products error:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleSearch = (e) => {
    e.preventDefault();
    setOffset(0);
    setQuery(search.trim());
  };

  const handleLoadMore = () => {
    setOffset((prev) => prev + LIMIT);
  };

  const handleAddToCart = async (productId, productName) => {
    setAddingToCart((prev) => ({ ...prev, [productId]: true }));
    setCartError(null);
    try {
      await addToCart(productId, 1);
    } catch (err) {
      setCartError(`Failed to add ${productName} to cart`);
      console.error('Add to cart error:', err);
    } finally {
      setAddingToCart((prev) => ({ ...prev, [productId]: false }));
    }
  };

  return (
    <div className="home">
      <div className="home-header">
        <h1>Shopping Mall</h1>
        <form onSubmit={handleSearch} className="search-form">
          <input
            type="text"
            placeholder="Search products..."
            aria-label="Search products"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="search-input"
          />
          <button type="submit" className="search-button">
            Search
          </button>
        </form>
      </div>

      {error && <div className="error-message">{error}</div>}
      {cartError && <div className="error-message">{cartError}</div>}

      {loading && offset === 0 ? (
        <div className="loading">Loading products...</div>
      ) : products.length === 0 ? (
        <div className="no-products">No products found</div>
      ) : (
        <>
          <div className="products-grid">
            {products.map((product) => (
              <div key={product.id} className="product-card">
                <Link to={`/products/${product.id}`} className="product-link">
                  <img
                    src={product.image_url}
                    alt={product.name}
                    className="product-image"
                  />
                  <h3 className="product-name">{product.name}</h3>
                  <p className="product-description">
                    {product.description?.substring(0, 100)}...
                  </p>
                </Link>
                <div className="product-footer">
                  <div className="product-price">${product.price.toFixed(2)}</div>
                  <div className="product-stock">
                    {product.stock > 0 ? (
                      <span className="in-stock">In Stock ({product.stock})</span>
                    ) : (
                      <span className="out-of-stock">Out of Stock</span>
                    )}
                  </div>
                </div>
                <button
                  onClick={() => handleAddToCart(product.id, product.name)}
                  disabled={product.stock === 0 || addingToCart[product.id]}
                  className="add-to-cart-button"
                >
                  {addingToCart[product.id] ? 'Adding...' : 'Add to Cart'}
                </button>
              </div>
            ))}
          </div>

          {hasMore && (
            <div className="load-more-container">
              <button
                onClick={handleLoadMore}
                disabled={loading}
                className="load-more-button"
              >
                {loading ? 'Loading...' : 'Load More'}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default Home;
