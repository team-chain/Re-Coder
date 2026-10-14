import axios from 'axios';

const API_URL = import.meta.env.VITE_API_URL || '';

const client = axios.create({
  baseURL: API_URL,
  headers: {
    'Content-Type': 'application/json'
  }
});

// Add token to requests
client.interceptors.request.use((config) => {
  const token = localStorage.getItem('token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Handle response errors
client.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      // Clear auth on 401
      localStorage.removeItem('token');
      localStorage.removeItem('user');
      window.location.href = '/login';
    }
    return Promise.reject(error);
  }
);

// Auth API
export const authAPI = {
  register: (email, password, name) =>
    client.post('/api/auth/register', { email, password, name }),
  login: (email, password) =>
    client.post('/api/auth/login', { email, password })
};

// Products API
export const productsAPI = {
  getAll: (limit = 20, offset = 0, search = null) =>
    client.get('/api/products', {
      params: { limit, offset, ...(search && { search }) }
    }),
  getById: (id) =>
    client.get(`/api/products/${id}`)
};

// Cart API
export const cartAPI = {
  get: () =>
    client.get('/api/cart'),
  add: (product_id, quantity) =>
    client.post('/api/cart', { product_id, quantity }),
  update: (product_id, quantity) =>
    client.post(`/api/cart/${product_id}`, { quantity }),
  remove: (product_id) =>
    client.delete(`/api/cart/${product_id}`)
};

// Orders API
export const ordersAPI = {
  create: (idempotency_key) =>
    client.post('/api/orders', { idempotency_key }),
  getAll: () =>
    client.get('/api/orders'),
  getById: (id) =>
    client.get(`/api/orders/${id}`),
  cancel: (id) =>
    client.post(`/api/orders/${id}/cancel`)
};

// Admin API
export const adminAPI = {
  createProduct: (name, description, price, stock, image_url) =>
    client.post('/api/admin/products', {
      name,
      description,
      price,
      stock,
      image_url
    }),
  updateProduct: (id, name, description, price, stock, image_url) =>
    client.put(`/api/admin/products/${id}`, {
      name,
      description,
      price,
      stock,
      image_url
    }),
  deleteProduct: (id) =>
    client.delete(`/api/admin/products/${id}`),
  getOrders: (limit = 50, offset = 0) =>
    client.get('/api/admin/orders', { params: { limit, offset } }),
  getOrderById: (id) =>
    client.get(`/api/admin/orders/${id}`)
};

export default client;