import {
  AudioOutlined,
  DashboardOutlined,
  LeftOutlined,
  LogoutOutlined,
  SettingOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import type { ModulePageDefinition, SidebarSectionDefinition } from '@/app/navigation'
import { adminSidebarSections } from '@/app/navigation'
import { roleLabel, roleLevel } from '@/app/roles'
import { useAuth } from '@/app/use-auth'
import { isWecomBrowser } from '@/app/wecom'

const SECTION_ICONS = {
  overview: DashboardOutlined,
  'customer-center': TeamOutlined,
  'recording-center': AudioOutlined,
  configuration: SettingOutlined,
  'system-center': SettingOutlined,
} satisfies Record<SidebarSectionDefinition['key'], typeof DashboardOutlined>

type AdminShellProps = {
  items: ModulePageDefinition[]
}

function isSectionActive(pathname: string, section: SidebarSectionDefinition) {
  return section.items.some((item) => pathname === `/admin/${item.path}` || pathname.startsWith(`/admin/${item.path}/`))
}

export function AdminShell({ items }: AdminShellProps) {
  const auth = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const showMobileBack = typeof window !== 'undefined' && (isWecomBrowser() || window.innerWidth < 768)

  const userRole = auth.status === 'authenticated' ? auth.user.role : 'staff'
  const userLevel = roleLevel(userRole)
  const pageMap = new Map(items.map((item) => [item.path, item]))

  const visibleSections = adminSidebarSections
    .map((section) => ({
      ...section,
      items: section.items.filter((item) => {
        const minLevel = roleLevel(item.minRole ?? 'staff')
        if (userLevel < minLevel) return false
        return item.path === 'dashboard' || pageMap.has(item.path)
      }),
    }))
    .filter((section) => section.items.length > 0)

  return (
    <div className="shell shell--desktop shell--refined">
      <aside className="shell__sidebar shell__sidebar--refined">
        <div className="brand-block brand-block--refined">
          <div className="brand-mark" aria-hidden="true">
            <svg className="brand-mark__svg" viewBox="0 0 220 76" xmlns="http://www.w3.org/2000/svg">
              <defs>
                <linearGradient id="lancyBrandPanel" x1="10" y1="8" x2="208" y2="66" gradientUnits="userSpaceOnUse">
                  <stop offset="0" stopColor="#FFFFFF" />
                  <stop offset="0.54" stopColor="#EEF5FF" />
                  <stop offset="1" stopColor="#D8E7FF" />
                </linearGradient>
                <linearGradient id="lancyBrandStroke" x1="12" y1="8" x2="206" y2="70" gradientUnits="userSpaceOnUse">
                  <stop offset="0" stopColor="#B8D2FF" />
                  <stop offset="1" stopColor="#4F84E8" />
                </linearGradient>
                <linearGradient id="lancyBrandBadge" x1="24" y1="18" x2="96" y2="58" gradientUnits="userSpaceOnUse">
                  <stop offset="0" stopColor="#3E78EC" />
                  <stop offset="1" stopColor="#1E56D8" />
                </linearGradient>
                <linearGradient id="lancyBrandWave" x1="36" y1="57" x2="96" y2="18" gradientUnits="userSpaceOnUse">
                  <stop offset="0" stopColor="#8DB6FF" />
                  <stop offset="1" stopColor="#DCEBFF" />
                </linearGradient>
                <radialGradient id="lancyBrandGlow" cx="0" cy="0" r="1" gradientUnits="userSpaceOnUse" gradientTransform="translate(176 16) rotate(144) scale(72 48)">
                  <stop offset="0" stopColor="#FFFFFF" stopOpacity="0.92" />
                  <stop offset="1" stopColor="#FFFFFF" stopOpacity="0" />
                </radialGradient>
              </defs>
              <rect x="1" y="1" width="218" height="74" rx="22" fill="url(#lancyBrandPanel)" />
              <rect x="1" y="1" width="218" height="74" rx="22" fill="url(#lancyBrandGlow)" />
              <rect x="1" y="1" width="218" height="74" rx="22" fill="none" stroke="url(#lancyBrandStroke)" strokeWidth="1.3" />
              <rect x="16" y="12" width="88" height="52" rx="18" fill="url(#lancyBrandBadge)" />
              <path d="M34 24H58" stroke="rgba(255,255,255,0.42)" strokeWidth="2.4" strokeLinecap="round" />
              <path d="M34 29H50" stroke="rgba(255,255,255,0.3)" strokeWidth="1.8" strokeLinecap="round" />
              <path
                d="M34 48V24"
                fill="none"
                stroke="#FFFFFF"
                strokeWidth="5.6"
                strokeLinecap="round"
              />
              <path
                d="M34 48H58"
                fill="none"
                stroke="#FFFFFF"
                strokeWidth="5.2"
                strokeLinecap="round"
              />
              <path
                d="M48 45C52.3 39.8 56.4 37.6 60.6 37.6C65.8 37.6 68.3 42 73.5 42C78.1 42 82.8 38.8 88.8 33"
                fill="none"
                stroke="url(#lancyBrandWave)"
                strokeWidth="2.8"
                strokeLinecap="round"
              />
              <circle cx="52" cy="28" r="2.5" fill="#D7E6FF" />
              <circle cx="61" cy="23.5" r="1.8" fill="#E8F2FF" />
              <circle cx="70" cy="20" r="1.35" fill="#F5FAFF" />
              <path d="M132 23H196" stroke="#A5C4FF" strokeWidth="1.4" strokeLinecap="round" opacity="0.84" />
              <text
                x="122"
                y="51"
                fill="#1B4FC8"
                fontFamily="Segoe UI, PingFang SC, Microsoft YaHei, sans-serif"
                fontSize="27"
                fontWeight="800"
                letterSpacing="0.22"
              >
                朗姿
              </text>
            </svg>
          </div>
          <div className="brand-block__copy">
            <p className="eyebrow">朗姿智能工牌</p>
            <h1>朗姿智能工牌系统</h1>
            <p>客户、接诊、录音与分析一体化后台</p>
          </div>
        </div>

        <nav className="sidebar-sections">
          {visibleSections.map((section) => {
            const Icon = SECTION_ICONS[section.key]
            const active = isSectionActive(location.pathname, section)

            return (
              <section
                key={section.key}
                className={`sidebar-section${active ? ' sidebar-section--active' : ''}`}
              >
                <div className="sidebar-section__header">
                  <div className="sidebar-section__icon">
                    <Icon />
                  </div>
                  <div className="sidebar-section__copy">
                    <strong>{section.title}</strong>
                    <p>{section.description}</p>
                  </div>
                </div>

                <div className="sidebar-section__links">
                  {section.items.map((item) => (
                    <NavLink
                      key={item.path}
                      className={({ isActive }) =>
                        `sidebar-link${isActive ? ' sidebar-link--active' : ''}`
                      }
                      to={item.path}
                      end={item.path === 'dashboard'}
                      title={item.description}
                    >
                      <div className="sidebar-link__row">
                        <span className="sidebar-link__label">{item.label}</span>
                        <span className="sidebar-link__indicator" aria-hidden="true" />
                      </div>
                    </NavLink>
                  ))}
                </div>
              </section>
            )
          })}
        </nav>

        {auth.status === 'authenticated' && (
          <div className="sidebar-user sidebar-user--refined">
            <div className="sidebar-user__rail">
              <div className="sidebar-user__avatar" aria-hidden="true">
                <UserOutlined />
              </div>
              <div>
                <strong>{auth.user.display_name || auth.user.username}</strong>
                <p>{roleLabel(auth.user.role)}</p>
              </div>
            </div>
            <div className="sidebar-user__actions">
              <NavLink className="sidebar-user__action" to="/admin/profile">
                <span className="sidebar-user__action-icon" aria-hidden="true">
                  <UserOutlined />
                </span>
                <span>个人中心</span>
              </NavLink>
              <button className="sidebar-user__action sidebar-user__action--button" onClick={auth.logout} type="button">
                <span className="sidebar-user__action-icon" aria-hidden="true">
                  <LogoutOutlined />
                </span>
                <span>退出</span>
              </button>
            </div>
          </div>
        )}
      </aside>

      <main className="shell__content shell__content--refined">
          {showMobileBack && (
            <button
              className="wc-floating-back"
              onClick={() => navigate(-1)}
              type="button"
            >
              <LeftOutlined /> 返回
            </button>
          )}
        <Outlet />
      </main>
    </div>
  )
}
