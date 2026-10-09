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
          🛍️ Shopping Mall
        </Link>

        <nav className="nav">
          <Link to="/" className="nav-link">
            Home
          </Link>

          {isAuthenticated ? (
            <>
              <Link to="/cart" className="nav-link cart-link">
                🛒 Cart
                {cartCount > 0 && <span className="cart-badge">{cartCount}</span>}
              </Link>

              <Link to="/orders" className="nav-link">
                Orders
              </Link>

              {isAdmin && (
                <>
                  <Link to="/admin/products" className="nav-link admin-link">
                    Admin Products
                  </Link>
                  <Link to="/admin/orders" className="nav-link admin-link">
                    Admin Orders
                  </Link>
                </>
              )}

              <div className="user-menu">
                <span className="user-name">{user?.name}</span>
                <button onClick={handleLogout} className="logout-btn">
                  Logout
                </button>
              </div>
            </>
          ) : (
            <>
              <Link to="/register" className="nav-link">
                Register
              </Link>
              <Link to="/login" className="nav-link login-link">
                Login
              </Link>
            </>
          )}
        </nav>
      </div>
    </header>
  );
}

export default Header;
