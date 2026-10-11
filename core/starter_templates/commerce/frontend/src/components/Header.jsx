import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { useCart } from '../context/CartContext';
import './Header.css';

function Header() {
  const { user, isAuthenticated, isAdmin, logout } = useAuth();
  const { getCartItemCount } = useCart();
  const navigate = useNavigate();
  const cartCount = getCartItemCount();

  const handleLogout = () => {
    logout();
    navigate('/');
  };

  return (
    <header className="header">
      <div className="header-container">
        <Link to="/" className="logo">
          <span className="logo-mark" aria-hidden="true">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M6 7h12l-1 13H7L6 7z" /><path d="M9 7a3 3 0 0 1 6 0" /></svg>
          </span>
          내 상점
        </Link>

        <nav className="nav">
          <Link to="/" className="nav-link">
            홈
          </Link>

          {isAuthenticated ? (
            <>
              <Link to="/cart" className="nav-link cart-link">
                장바구니
                {cartCount > 0 && <span className="cart-badge">{cartCount}</span>}
              </Link>

              <Link to="/orders" className="nav-link">
                주문 내역
              </Link>

              {isAdmin && (
                <>
                  <Link to="/admin/products" className="nav-link admin-link">
                    상품 관리
                  </Link>
                  <Link to="/admin/orders" className="nav-link admin-link">
                    주문 관리
                  </Link>
                </>
              )}

              <div className="user-menu">
                <span className="user-name">{user?.name}</span>
                <button onClick={handleLogout} className="logout-btn">
                  로그아웃
                </button>
              </div>
            </>
          ) : (
            <>
              <Link to="/register" className="nav-link">
                회원가입
              </Link>
              <Link to="/login" className="nav-link login-link">
                로그인
              </Link>
            </>
          )}
        </nav>
      </div>
    </header>
  );
}

export default Header;
