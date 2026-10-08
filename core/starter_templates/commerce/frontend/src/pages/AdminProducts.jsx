import { useState, useEffect } from 'react';
import { useAuth } from '../context/AuthContext';
import { adminAPI } from '../api/client';
import '../styles/AdminProducts.css';

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
        throw new Error('Failed to fetch products');
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
      setError('Name, price, and stock are required');
      return;
    }

    const price = parseFloat(formData.price);
    const stock = parseInt(formData.stock);

    if (isNaN(price) || price <= 0) {
      setError('Price must be a positive number');
      return;
    }

    if (!Number.isInteger(stock) || stock < 0) {
      setError('Stock must be a non-negative integer');
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
        setSuccess('Product updated successfully');
      } else {
        // Create product
        await adminAPI.createProduct(
          formData.name,
          formData.description,
          price,
          stock,
          formData.image_url
        );
        setSuccess('Product created successfully');
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
      price: product.price.toString(),
      stock: product.stock.toString(),
      image_url: product.image_url || ''
    });
    setEditingId(product.id);
    setShowForm(true);
  };

  const handleDelete = async (id) => {
    if (!window.confirm('Are you sure you want to delete this product?')) {
      return;
    }

    setError(null);
    setSuccess(null);

    try {
      await adminAPI.deleteProduct(id);
      setSuccess('Product deleted successfully');
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
        <h1>Product Management</h1>
        <button
          className="btn btn-primary"
          onClick={() => setShowForm(!showForm)}
        >
          {showForm ? 'Cancel' : 'Add New Product'}
        </button>
      </div>

      {error && <div className="alert alert-error">{error}</div>}
      {success && <div className="alert alert-success">{success}</div>}

      {showForm && (
        <div className="product-form-container">
          <form className="product-form" onSubmit={handleSubmit}>
            <h2>{editingId ? 'Edit Product' : 'Create New Product'}</h2>

            <div className="form-group">
              <label htmlFor="name">Product Name *</label>
              <input
                type="text"
                id="name"
                name="name"
                value={formData.name}
                onChange={handleInputChange}
                placeholder="Enter product name"
                required
              />
            </div>

            <div className="form-group">
              <label htmlFor="description">Description</label>
              <textarea
                id="description"
                name="description"
                value={formData.description}
                onChange={handleInputChange}
                placeholder="Enter product description"
                rows="4"
              />
            </div>

            <div className="form-row">
              <div className="form-group">
                <label htmlFor="price">Price (USD) *</label>
                <input
                  type="number"
                  id="price"
                  name="price"
                  value={formData.price}
                  onChange={handleInputChange}
                  placeholder="0.00"
                  step="0.01"
                  min="0"
                  required
                />
              </div>

              <div className="form-group">
                <label htmlFor="stock">Stock *</label>
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
              <label htmlFor="image_url">Image URL</label>
              <input
                type="url"
                id="image_url"
                name="image_url"
                value={formData.image_url}
                onChange={handleInputChange}
                placeholder="https://example.com/image.jpg"
              />
            </div>

            <div className="form-actions">
              <button type="submit" className="btn btn-primary">
                {editingId ? 'Update Product' : 'Create Product'}
              </button>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={handleCancel}
              >
                Cancel
              </button>
            </div>
          </form>
        </div>
      )}

      {loading ? (
        <div className="loading">Loading products...</div>
      ) : products.length === 0 ? (
        <div className="empty-state">No products found</div>
      ) : (
        <div className="products-table-container">
          <table className="products-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Name</th>
                <th>Price</th>
                <th>Stock</th>
                <th>Image</th>
                <th>Actions</th>
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
                        <p className="product-desc">{product.description.substring(0, 50)}...</p>
                      )}
                    </div>
                  </td>
                  <td>${product.price.toFixed(2)}</td>
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
                      <span className="no-image">No image</span>
                    )}
                  </td>
                  <td>
                    <div className="action-buttons">
                      <button
                        className="btn btn-sm btn-secondary"
                        onClick={() => handleEdit(product)}
                      >
                        Edit
                      </button>
                      <button
                        className="btn btn-sm btn-danger"
                        onClick={() => handleDelete(product.id)}
                        disabled={product.stock !== 0}
                        title={product.stock !== 0 ? 'Cannot delete product with stock' : ''}
                      >
                        Delete
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