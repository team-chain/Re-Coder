import { useState, useEffect } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { productsAPI } from '../api/client';
import { useCart } from '../context/CartContext';
import '../styles/Home.css';
import { formatWon } from '../utils/money';

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
  const { isAuthenticated } = useAuth();
  const navigate = useNavigate();
  const [added, setAdded] = useState(null);
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
      setError(err.response?.data?.error || '상품을 불러오지 못했습니다');
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
    if (!isAuthenticated) {
      navigate('/login', { state: { from: { pathname: '/' } } });
      return;
    }
    setAddingToCart((prev) => ({ ...prev, [productId]: true }));
    setCartError(null);
    try {
      await addToCart(productId, 1);
      setAdded(productName);
      setTimeout(() => setAdded(null), 2500);
    } catch (err) {
      setCartError(err.message || `${productName}을(를) 장바구니에 담지 못했습니다`);
      console.error('Add to cart error:', err);
    } finally {
      setAddingToCart((prev) => ({ ...prev, [productId]: false }));
    }
  };

  return (
    <div className="home">
      <div className="home-header">
        <div>
          <h1>전체 상품</h1>
          {total > 0 && <p className="home-count">상품 {total.toLocaleString('ko-KR')}개</p>}
        </div>
        <form onSubmit={handleSearch} className="search-form">
          <input
            type="text"
            placeholder="상품명으로 검색"
            aria-label="상품 검색"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="search-input"
          />
          <button type="submit" className="search-button">
            검색
          </button>
        </form>
      </div>

      {error && <div className="error-message">{error}</div>}
      {cartError && <div className="error-message">{cartError}</div>}
      {added && <div className="toast" role="status">{added}을(를) 장바구니에 담았습니다 · <Link to="/cart">장바구니 보기</Link></div>}

      {loading && offset === 0 ? (
        <div className="loading">상품을 불러오는 중…</div>
      ) : products.length === 0 ? (
        <div className="no-products">{query ? `'${query}' 검색 결과가 없습니다` : '등록된 상품이 없습니다'}</div>
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
                    {product.description?.length > 60 ? `${product.description.substring(0, 60)}…` : product.description}
                  </p>
                </Link>
                <div className="product-footer">
                  <div className="product-price">{formatWon(product.price)}</div>
                  <div className="product-stock">
                    {product.stock > 0 ? (
                      product.stock <= 5 ? <span className="low-stock">품절 임박 · {product.stock}개 남음</span> : <span className="in-stock">무료배송</span>
                    ) : (
                      <span className="out-of-stock">품절</span>
                    )}
                  </div>
                </div>
                <button
                  onClick={() => handleAddToCart(product.id, product.name)}
                  disabled={product.stock === 0 || addingToCart[product.id]}
                  className="add-to-cart-button"
                >
                  {product.stock === 0 ? '품절' : addingToCart[product.id] ? '담는 중…' : '장바구니 담기'}
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
                {loading ? '불러오는 중…' : `상품 더 보기 (${products.length}/${total})`}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default Home;
