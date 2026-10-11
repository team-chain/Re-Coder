import { useState, useEffect } from 'react';
import { useAuth } from '../context/AuthContext';
import { adminAPI } from '../api/client';
import '../styles/AdminProducts.css';
import { formatWon } from '../utils/money';

function AdminProducts() {
  const { token } = useAuth();
  const [products, setProducts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [formData, setFormData] = useState({
    name: '',
    description: '',
    price: '',
    stock: '',
    image_url: ''
  });

  useEffect(() => {
    fetchProducts();
  }, []);

  const fetchProducts = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch('/api/products?limit=100', {
        headers: {
          'Authorization': `Bearer ${token}`
        }
      });

      if (!response.ok) {
        throw new Error('상품 목록을 불러오지 못했습니다');
      }

      const data = await response.json();
      setProducts(data.products || []);
    } catch (err) {
      setError(err.message);
      console.error('Fetch products error:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleInputChange = (e) => {
    const { name, value } = e.target;
    setFormData(prev => ({
      ...prev,
      [name]: value
    }));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError(null);
    setSuccess(null);

    // Validation
    if (!formData.name || !formData.price || formData.stock === '') {
      setError('상품명, 가격, 재고는 필수입니다');
      return;
    }

    const price = Math.round(Number(formData.price));
    const stock = parseInt(formData.stock);

    if (!Number.isSafeInteger(price) || price < 100 || price > 99999999) {
      setError('가격은 100원 이상 1억 원 미만의 정수(원)로 입력하세요');
      return;
    }

    if (!Number.isInteger(stock) || stock < 0) {
      setError('재고는 0 이상의 정수로 입력하세요');
      return;
    }

    try {
      if (editingId) {
        // Update product
        await adminAPI.updateProduct(
          editingId,
          formData.name,
          formData.description,
          price,
          stock,
          formData.image_url
        );
        setSuccess('상품을 수정했습니다');
      } else {
        // Create product
        await adminAPI.createProduct(
          formData.name,
          formData.description,
          price,
          stock,
          formData.image_url
        );
        setSuccess('상품을 등록했습니다');
      }

      // Reset form
      setFormData({
        name: '',
        description: '',
        price: '',
        stock: '',
        image_url: ''
      });
      setEditingId(null);
      setShowForm(false);

      // Refresh products
      await fetchProducts();
    } catch (err) {
      setError(err.response?.data?.error || err.message);
      console.error('Submit error:', err);
    }
  };

  const handleEdit = (product) => {
    setFormData({
      name: product.name,
      description: product.description || '',
      price: String(Math.round(Number(product.price))),
      stock: product.stock.toString(),
      image_url: product.image_url || ''
    });
    setEditingId(product.id);
    setShowForm(true);
  };

  const handleDelete = async (id) => {
    if (!window.confirm('이 상품을 삭제할까요? 되돌릴 수 없습니다.')) {
      return;
    }

    setError(null);
    setSuccess(null);

    try {
      await adminAPI.deleteProduct(id);
      setSuccess('상품을 삭제했습니다');
      await fetchProducts();
    } catch (err) {
      setError(err.response?.data?.error || err.message);
      console.error('Delete error:', err);
    }
  };

  const handleCancel = () => {
    setFormData({
      name: '',
      description: '',
      price: '',
      stock: '',
      image_url: ''
    });
    setEditingId(null);
    setShowForm(false);
  };

  return (
    <div className="admin-products">
      <div className="admin-header">
        <h1>상품 관리</h1>
        <button
          className="btn btn-primary"
          onClick={() => setShowForm(!showForm)}
        >
          {showForm ? '닫기' : '+ 상품 등록'}
        </button>
      </div>

      {error && <div className="alert alert-error">{error}</div>}
      {success && <div className="alert alert-success">{success}</div>}

      {showForm && (
        <div className="product-form-container">
          <form className="product-form" onSubmit={handleSubmit}>
            <h2>{editingId ? '상품 수정' : '상품 등록'}</h2>

            <div className="form-group">
              <label htmlFor="name">상품명 (필수)</label>
              <input
                type="text"
                id="name"
                name="name"
                value={formData.name}
                onChange={handleInputChange}
                placeholder="예: 무선 헤드폰"
                required
              />
            </div>

            <div className="form-group">
              <label htmlFor="description">상품 설명</label>
              <textarea
                id="description"
                name="description"
                value={formData.description}
                onChange={handleInputChange}
                placeholder="소재, 크기, 구성품 등을 적어 주세요"
                rows="4"
              />
            </div>

            <div className="form-row">
              <div className="form-group">
                <label htmlFor="price">가격(원) (필수)</label>
                <input
                  type="number"
                  id="price"
                  name="price"
                  value={formData.price}
                  onChange={handleInputChange}
                  placeholder="예: 39000"
                  step="1"
                  min="0"
                  required
                />
              </div>

              <div className="form-group">
                <label htmlFor="stock">재고 (필수)</label>
                <input
                  type="number"
                  id="stock"
                  name="stock"
                  value={formData.stock}
                  onChange={handleInputChange}
                  placeholder="0"
                  min="0"
                  required
                />
              </div>
            </div>

            <div className="form-group">
              <label htmlFor="image_url">이미지 주소</label>
              <input
                type="text"
                id="image_url"
                name="image_url"
                value={formData.image_url}
                onChange={handleInputChange}
                placeholder="https://… 또는 /products/파일.svg"
              />
            </div>

            <div className="form-actions">
              <button type="submit" className="btn btn-primary">
                {editingId ? '수정 저장' : '등록하기'}
              </button>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={handleCancel}
              >
                취소
              </button>
            </div>
          </form>
        </div>
      )}

      {loading ? (
        <div className="loading">상품 목록을 불러오는 중…</div>
      ) : products.length === 0 ? (
        <div className="empty-state">등록된 상품이 없습니다</div>
      ) : (
        <div className="products-table-container">
          <table className="products-table">
            <thead>
              <tr>
                <th>번호</th>
                <th>상품</th>
                <th>가격</th>
                <th>재고</th>
                <th>이미지</th>
                <th><span className="sr-only">관리</span></th>
              </tr>
            </thead>
            <tbody>
              {products.map(product => (
                <tr key={product.id}>
                  <td>{product.id}</td>
                  <td>
                    <div className="product-name">
                      <strong>{product.name}</strong>
                      {product.description && (
                        <p className="product-desc">{product.description.length > 40 ? `${product.description.substring(0, 40)}…` : product.description}</p>
                      )}
                    </div>
                  </td>
                  <td>{formatWon(product.price)}</td>
                  <td>
                    <span className={`stock-badge ${product.stock === 0 ? 'out-of-stock' : 'in-stock'}`}>
                      {product.stock}
                    </span>
                  </td>
                  <td>
                    {product.image_url ? (
                      <img
                        src={product.image_url}
                        alt={product.name}
                        className="product-thumbnail"
                      />
                    ) : (
                      <span className="no-image">없음</span>
                    )}
                  </td>
                  <td>
                    <div className="action-buttons">
                      <button
                        className="btn btn-sm btn-secondary"
                        onClick={() => handleEdit(product)}
                      >
                        수정
                      </button>
                      <button
                        className="btn btn-sm btn-danger"
                        onClick={() => handleDelete(product.id)}
                        disabled={product.stock !== 0}
                        title={product.stock !== 0 ? '재고가 남은 상품은 삭제할 수 없습니다 (재고를 0으로 바꾼 뒤 삭제)' : ''}
                      >
                        삭제
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default AdminProducts;