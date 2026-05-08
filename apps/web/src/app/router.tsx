import { lazy, Suspense, type ReactNode } from 'react'
import { createBrowserRouter, Navigate, RouterProvider, useLocation } from 'react-router-dom'

import { adminPages } from '@/app/navigation'
import { canAccessRole, type PermissionRole } from '@/app/roles'
import { useAuth } from '@/app/use-auth'
import { WECOM_DEFAULT_BADGE_PATH, getDefaultPortalPath, isWecomBrowser, normalizeRedirectPath } from '@/app/wecom'
import { AdminShell } from '@/layouts/admin-shell'
import { WecomShell } from '@/layouts/wecom-shell'

const LoginPage = lazy(() => import('@/pages/login-page'))
const NotFoundPage = lazy(() => import('@/pages/not-found-page'))
const DashboardPage = lazy(() => import('@/pages/dashboard-page'))
const ProfilePage = lazy(() => import('@/pages/admin/profile-page'))
const PreferencesPage = lazy(() => import('@/pages/admin/preferences-page'))
const IotCapabilitiesPage = lazy(() => import('@/pages/admin/iot-capabilities-page'))
const HotwordsPage = lazy(() => import('@/pages/admin/hotwords-page'))
const StaffPage = lazy(() => import('@/pages/admin/staff-page'))
const OrganizationPage = lazy(() => import('@/pages/admin/organization-page'))
const PositionsPage = lazy(() => import('@/pages/admin/positions-page'))
const InstitutionsPage = lazy(() => import('@/pages/admin/institutions-page'))
const DingtalkBadgePage = lazy(() => import('@/pages/admin/dingtalk-badge-page'))
const DingtalkAudioArchivePage = lazy(() => import('@/pages/admin/dingtalk-audio-archive-page'))
const DingtalkAudioAnalysisPage = lazy(() => import('@/pages/admin/dingtalk-audio-analysis-page'))
const DingtalkAudioAnalysisDetailPage = lazy(() => import('@/pages/admin/dingtalk-audio-analysis-detail-page'))
const AuditLogsPage = lazy(() => import('@/pages/admin/audit-logs-page'))
const AsrMonitoringPage = lazy(() => import('@/pages/admin/asr-monitoring-page'))
const SapPushMonitoringPage = lazy(() => import('@/pages/admin/sap-push-monitoring-page'))
const CustomersPage = lazy(() => import('@/pages/admin/customers-page'))
const CustomerDetailPage = lazy(() => import('@/pages/admin/customer-detail-page'))
const VisitsPage = lazy(() => import('@/pages/admin/visits-page'))
const VisitDetailPage = lazy(() => import('@/pages/admin/visit-detail-page'))
const RecordingDetailPage = lazy(() => import('@/pages/admin/recording-detail-page'))
const TranscriptsPage = lazy(() => import('@/pages/admin/transcripts-page'))
const TranscriptDetailPage = lazy(() => import('@/pages/admin/transcript-detail-page'))
const TagPackagesPage = lazy(() => import('@/pages/admin/tag-packages-page'))
const VisitOrdersPage = lazy(() => import('@/pages/admin/visit-orders-page'))
const WecomBadgePage = lazy(() => import('@/pages/wecom/wecom-badge-page'))
const WecomHomePage = lazy(() => import('@/pages/wecom/wecom-home-page'))
const WecomCustomersPage = lazy(() => import('@/pages/wecom/wecom-customers-page'))
const WecomVisitsPage = lazy(() => import('@/pages/wecom/wecom-visits-page'))
const WecomVisitDetailPage = lazy(() => import('@/pages/wecom/wecom-visit-detail-page'))
const WecomCustomerDetailPage = lazy(() => import('@/pages/wecom/wecom-customer-detail-page'))
const WecomRecordingsPage = lazy(() => import('@/pages/wecom/wecom-recordings-page'))
const WecomRecordingDetailPage = lazy(() => import('@/pages/wecom/wecom-recording-detail-page'))

function Lazy({ children }: { children: ReactNode }) {
  return <Suspense fallback={<div style={{ padding: 48, textAlign: 'center' }}>加载中…</div>}>{children}</Suspense>
}

function AuthLoading() {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100vh' }}>
      加载中…
    </div>
  )
}

function RequireAuth({ children }: { children: ReactNode }) {
  const auth = useAuth()
  const location = useLocation()

  if (auth.status === 'loading') {
    return <AuthLoading />
  }

  if (auth.status === 'unauthenticated') {
    const redirect = `${location.pathname}${location.search}${location.hash}`
    const params = new URLSearchParams({ redirect })
    if (isWecomBrowser()) {
      params.set('wecom', '1')
    }
    return <Navigate replace to={`/login?${params.toString()}`} />
  }

  return <>{children}</>
}

function RequireRole({ children, minRole }: { children: ReactNode; minRole: PermissionRole }) {
  const auth = useAuth()
  if (auth.status !== 'authenticated') {
    return <AuthLoading />
  }
  if (!canAccessRole(auth.user.role, minRole)) {
    return <Navigate replace to="/admin/dashboard" />
  }
  return <>{children}</>
}

function LoginRoute() {
  const auth = useAuth()
  const location = useLocation()
  const searchParams = new URLSearchParams(location.search)
  const preferWecom = searchParams.get('wecom') === '1' || isWecomBrowser()
  const redirectPath = normalizeRedirectPath(searchParams.get('redirect'), { preferWecom })

  if (auth.status === 'loading') {
    return <AuthLoading />
  }

  if (auth.status === 'authenticated') {
    return <Navigate replace to={redirectPath} />
  }

  return (
    <Lazy>
      <LoginPage />
    </Lazy>
  )
}

function LandingRoute() {
  const auth = useAuth()

  if (auth.status === 'loading') {
    return <AuthLoading />
  }

  if (auth.status === 'unauthenticated') {
    const preferWecom = isWecomBrowser()
    const target = getDefaultPortalPath({ preferWecom })
    const params = new URLSearchParams({ redirect: target })
    if (preferWecom) {
      params.set('wecom', '1')
    }
    return <Navigate replace to={`/login?${params.toString()}`} />
  }

  return <Navigate replace to={getDefaultPortalPath()} />
}

const router = createBrowserRouter([
  {
    path: '/login',
    element: <LoginRoute />,
  },
  {
    path: '/',
    element: <LandingRoute />,
  },
  {
    path: '/admin',
    element: (
      <RequireAuth>
        <AdminShell items={adminPages} />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <Navigate replace to="dashboard" /> },
      { path: 'dashboard', element: <Lazy><DashboardPage /></Lazy> },
      { path: 'profile', element: <Lazy><ProfilePage /></Lazy> },
      { path: 'preferences', element: <Lazy><PreferencesPage /></Lazy> },
      {
        path: 'iot-capabilities',
        element: (
          <RequireRole minRole="system_admin">
            <Lazy><IotCapabilitiesPage /></Lazy>
          </RequireRole>
        ),
      },
      { path: 'hotwords', element: <Lazy><HotwordsPage /></Lazy> },
      { path: 'tag-packages', element: <Lazy><TagPackagesPage /></Lazy> },
      { path: 'staff', element: <Lazy><StaffPage /></Lazy> },
      { path: 'organization', element: <Lazy><OrganizationPage /></Lazy> },
      { path: 'positions', element: <Lazy><PositionsPage /></Lazy> },
      {
        path: 'institutions',
        element: (
          <RequireRole minRole="hospital_admin">
            <Lazy><InstitutionsPage /></Lazy>
          </RequireRole>
        ),
      },
      { path: 'dingtalk-badge', element: <Lazy><DingtalkBadgePage /></Lazy> },
      { path: 'audit-logs', element: <Lazy><AuditLogsPage /></Lazy> },
      { path: 'asr-monitoring', element: <Lazy><AsrMonitoringPage /></Lazy> },
      { path: 'sap-push-monitoring', element: <Lazy><SapPushMonitoringPage /></Lazy> },
      { path: 'customers', element: <Lazy><CustomersPage /></Lazy> },
      { path: 'customers/:customerId', element: <Lazy><CustomerDetailPage /></Lazy> },
      { path: 'visits', element: <Lazy><VisitsPage /></Lazy> },
      { path: 'visits/:visitId', element: <Lazy><VisitDetailPage /></Lazy> },
      { path: 'visit-orders', element: <Lazy><VisitOrdersPage /></Lazy> },
      { path: 'sap-hana-visit-orders', element: <Navigate replace to="/admin/visit-orders" /> },
      { path: 'llm-results', element: <Lazy><DingtalkAudioAnalysisPage /></Lazy> },
      { path: 'llm-results/:fileId', element: <Lazy><DingtalkAudioAnalysisDetailPage /></Lazy> },
      { path: 'recordings', element: <Lazy><DingtalkAudioArchivePage /></Lazy> },
      { path: 'recordings/:recordingId', element: <Lazy><RecordingDetailPage /></Lazy> },
      { path: 'transcripts', element: <Lazy><TranscriptsPage /></Lazy> },
      { path: 'transcripts/:transcriptId', element: <Lazy><TranscriptDetailPage /></Lazy> },
    ],
  },
  {
    path: '/wecom',
    element: (
      <RequireAuth>
        <WecomShell />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <Navigate replace to={WECOM_DEFAULT_BADGE_PATH} /> },
      { path: 'home', element: <Navigate replace to={WECOM_DEFAULT_BADGE_PATH} /> },
      { path: 'badge', element: <Lazy><WecomBadgePage /></Lazy> },
      { path: 'overview', element: <Lazy><WecomHomePage /></Lazy> },
      { path: 'customers', element: <Lazy><WecomCustomersPage /></Lazy> },
      { path: 'visits', element: <Lazy><WecomVisitsPage /></Lazy> },
      { path: 'visits/:visitId', element: <Lazy><WecomVisitDetailPage /></Lazy> },
      { path: 'customers/:customerId', element: <Lazy><WecomCustomerDetailPage /></Lazy> },
      { path: 'recordings', element: <Lazy><WecomRecordingsPage /></Lazy> },
      { path: 'recordings/:recordingId', element: <Lazy><WecomRecordingDetailPage /></Lazy> },
      { path: 'profile', element: <Navigate replace to="/wecom/badge" /> },
    ],
  },
  {
    path: '*',
    element: <Lazy><NotFoundPage /></Lazy>,
  },
])

export function AppRouter() {
  return <RouterProvider router={router} />
}
