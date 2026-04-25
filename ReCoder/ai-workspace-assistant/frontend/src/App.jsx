import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import Sessions from './pages/Sessions';
import Chat from './pages/Chat';

function RequireAuth({ children }) {
  const token = localStorage.getItem('token');
  if (!token) {
    return <Navigate to="/login" replace />;
  }
  return children;
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/dashboard" element={<RequireAuth><Dashboard /></RequireAuth>} />
        <Route path="/sessions" element={<RequireAuth><Sessions /></RequireAuth>} />
        <Route path="/chat" element={<RequireAuth><Chat /></RequireAuth>} />
        <Route path="/" element={<RequireAuth><Navigate to="/dashboard" replace /></RequireAuth>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
