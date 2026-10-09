import { ReactNode } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider, useAuth } from './hooks/auth'
import { SystemProvider } from './hooks/system'
import Layout from './components/Layout'
import Login from './pages/Login'
import AccountSetup from './pages/AccountSetup'
import Dashboard from './pages/Dashboard'
import Strategies from './pages/Strategies'
import StrategyDetail from './pages/StrategyDetail'
import Orders from './pages/Orders'
import Trades from './pages/Trades'
import Positions from './pages/Positions'
import Risk from './pages/Risk'
import Health from './pages/Health'
import Faults from './pages/Faults'
import Audit from './pages/Audit'
import Settings from './pages/Settings'
import Landing from './pages/Landing'

function Protected({ children }: { children: ReactNode }) {
  const { user } = useAuth()
  if (!user) return <Navigate to="/welcome" replace />
  return <SystemProvider>{children}</SystemProvider>
}

function PublicOnly({ children }: { children: ReactNode }) {
  const { user } = useAuth()
  return user ? <Navigate to="/" replace /> : <>{children}</>
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/welcome" element={<PublicOnly><Landing /></PublicOnly>} />
          <Route path="/login" element={<PublicOnly><Login mode="login" /></PublicOnly>} />
          <Route path="/register" element={<PublicOnly><Login mode="register" /></PublicOnly>} />
          <Route path="/setup" element={<Protected><AccountSetup /></Protected>} />
          <Route element={<Protected><Layout /></Protected>}>
            <Route index element={<Dashboard />} />
            <Route path="strategies" element={<Strategies />} />
            <Route path="strategies/:id" element={<StrategyDetail />} />
            <Route path="orders" element={<Orders />} />
            <Route path="trades" element={<Trades />} />
            <Route path="positions" element={<Positions />} />
            <Route path="risk" element={<Risk />} />
            <Route path="health" element={<Health />} />
            <Route path="faults" element={<Faults />} />
            <Route path="audit" element={<Audit />} />
            <Route path="settings" element={<Settings />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  )
}
