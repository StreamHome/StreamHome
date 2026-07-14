import React from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { ThemeProvider } from './contexts/ThemeContext';
import MainLayout from './layouts/MainLayout';
import Login from './pages/Login';
import ProfileSelect from './pages/ProfileSelect';
import DashboardRouter from './pages/DashboardRouter';
import AdminCenter from './pages/AdminCenter';

function App() {
  return (
    <ThemeProvider>
      <Router>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/profiles" element={<ProfileSelect />} />
          <Route path="/admin" element={<AdminCenter />} />
          
          {/* Main Layout containing the Dashboard with query parameters */}
          <Route path="/" element={<MainLayout />}>
            <Route index element={<DashboardRouter />} />
          </Route>
          
          <Route path="*" element={<Navigate to="/profiles" replace />} />
        </Routes>
      </Router>
    </ThemeProvider>
  );
}

export default App;
