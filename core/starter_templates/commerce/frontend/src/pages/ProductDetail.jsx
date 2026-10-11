import { useState, useEffect } from 'react';
import { useParams, useNavigate, Link } from 'react-router-dom';
import { productsAPI } from '../api/client';
import { useCart } from '../context/CartContext';
import { useAuth } from '../context/AuthContext';
import '../styles/ProductDetail.css';
import { formatWon } from '../utils/money';

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
        setError(err.response?.data?.error || '상품을 불러오지 못했습니다');
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
    return <div className="product-detail loading">불러오는 중…</div>;
  }

  if (error && !product) {
    return <div className="product-detail error">{error}</div>;
  }

  if (!product) {
    return <div className="product-detail error">상품을 찾을 수 없습니다</div>;
  }

  const isOutOfStock = product.stock === 0;

  return (
    <div className="product-detail">
      <button className="back-button" onClick={() => navigate('/')}>
        ← 상품 목록
      </button>

      <div className="product-detail-container">
        <div className="product-image-section">
          <img
            src={product.image_url || '/products/placeholder.svg'}
            alt={product.name}
            className="product-image"
          />
        </div>

        <div className="product-info-section">
          <h1 className="product-name">{product.name}</h1>

          <div className="product-price">{formatWon(product.price)}</div>

          <div className="product-stock">
            {isOutOfStock ? (
              <span className="out-of-stock">품절</span>
            ) : (
              <span className={product.stock <= 5 ? 'low-stock' : 'in-stock'}>{product.stock <= 5 ? `품절 임박 · ${product.stock}개 남음` : '구매 가능'}</span>
            )}
          </div>

          <div className="product-description">
            <h3>상품 설명</h3>
            <p>{product.description || '상품 설명이 없습니다'}</p>
          </div>

          {error && <div className="error-message">{error}</div>}
          {addSuccess && (
            <div className="success-message" role="status">장바구니에 담았습니다 · <Link to="/cart">장바구니 보기</Link></div>
          )}

          <div className="product-actions">
            <div className="quantity-selector">
              <label htmlFor="quantity">수량</label>
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
              {isOutOfStock ? '품절' : addingToCart ? '담는 중…' : `장바구니 담기 · ${formatWon(Number(product.price) * quantity)}`}
            </button>
          </div>

          <ul className="product-policy">
            <li><strong>배송</strong> 무료배송 · 결제 후 1~3일 이내 출고</li>
            <li><strong>교환·반품</strong> 받은 날로부터 7일 이내 신청 가능</li>
          </ul>

          {!isAuthenticated && (
            <p className="login-prompt">
              장바구니에 담으려면 <Link to="/login">로그인</Link>하세요.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

export default ProductDetail;
