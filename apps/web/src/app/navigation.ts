import type { PermissionRole } from '@/app/roles'

export type ModulePageDefinition = {
  path: string
  title: string
  navTitle?: string
  subtitle: string
  summary: string
  focusLabel: string
  checkpoints: string[]
  minRole?: PermissionRole
}

export type SidebarItemDefinition = {
  path: string
  label: string
  description: string
  minRole?: PermissionRole
}

export type SidebarSectionDefinition = {
  key:
    | 'overview'
    | 'customer-center'
    | 'configuration'
    | 'recording-center'
    | 'system-center'
  title: string
  description: string
  items: SidebarItemDefinition[]
}

export const adminPages: ModulePageDefinition[] = [
  {
    path: 'dashboard',
    title: '数据总览',
    subtitle: '查看核心指标、任务进度与系统健康状态。',
    summary: '统一查看分析、录音、客户和任务处理情况。',
    focusLabel: '运营总览',
    checkpoints: ['任务趋势', '分析总量', '系统健康'],
    minRole: 'staff',
  },
  {
    path: 'visits',
    title: '接诊记录',
    subtitle: '管理接诊单、录音关联与分析结果。',
    summary: '客户中心的核心工作台，串起客户、录音、转写和分析结果。',
    focusLabel: '业务对象',
    checkpoints: ['时间筛选', '卡片列表', '详情弹窗'],
    minRole: 'staff',
  },
  {
    path: 'visit-orders',
    title: '到诊单据',
    subtitle: '查看按录音上下文同步的到诊单据。',
    summary: '展示与录音指定时间、机构和参与角色匹配的到诊单据，支持按需同步。',
    focusLabel: '业务对象',
    checkpoints: ['同步触发', '时间筛选', '顾问筛选'],
    minRole: 'staff',
  },
  {
    path: 'customers',
    title: '客户档案',
    subtitle: '管理客户资料、来访动态与接诊历史。',
    summary: '沉淀客户信息、来访轨迹和录音分析结果。',
    focusLabel: '业务对象',
    checkpoints: ['客户卡片', '来访时间线', '客户详情'],
    minRole: 'staff',
  },
  {
    path: 'preferences',
    title: '偏好设置',
    subtitle: '配置接诊字段、推送策略与系统交互偏好。',
    summary: '统一维护接诊采集规则、多录音归并和系统联动配置。',
    focusLabel: '配置对象',
    checkpoints: ['接诊参数', '推送开关', '岗位联动'],
    minRole: 'system_admin',
  },
  {
    path: 'iot-capabilities',
    title: 'IOT配置',
    subtitle: '管理设备管理平台额外控制、任务和回调能力。',
    summary: '统一开关 IOT 附加接口，默认关闭后按需启用。',
    focusLabel: '配置对象',
    checkpoints: ['能力开关', '回调入口', '高风险确认'],
    minRole: 'system_admin',
  },
  {
    path: 'hotwords',
    title: '热词管理',
    subtitle: '维护竞品、顾虑、项目与公共词库。',
    summary: '为识别、分析和运营配置提供热词基础。',
    focusLabel: '配置对象',
    checkpoints: ['词库列表', '热词标签', '导出'],
    minRole: 'system_admin',
  },
  {
    path: 'tag-packages',
    title: '标签配置',
    subtitle: '维护客户需求标签和标签分类。',
    summary: '沉淀需求标签、客户画像和标签分类结构。',
    focusLabel: '运营对象',
    checkpoints: ['标签卡片', '标签组', '分类展示'],
    minRole: 'system_admin',
  },
  {
    path: 'profile',
    title: '个人中心',
    subtitle: '查看个人资料、修改密码与最近活动。',
    summary: '维护当前登录账号的基础信息与安全设置。',
    focusLabel: '系统对象',
    checkpoints: ['资料卡片', '密码修改', '最近活动'],
    minRole: 'staff',
  },
  {
    path: 'staff',
    title: '人员管理',
    subtitle: '维护人员、员工编号、岗位、机构归属和设备工牌信息。',
    summary: '系统管理中的人员工作台，负责顾问资料、机构归属和设备工牌信息维护。',
    focusLabel: '系统对象',
    checkpoints: ['人员列表', '机构归属', '员工编号与设备工牌'],
    minRole: 'hospital_admin',
  },
  {
    path: 'organization',
    title: '组织架构',
    subtitle: '配置机构内组织层级、成员归属和人员管理关系。',
    summary: '为机构维护多层组织树，并明确每位员工可以管理哪些人员。',
    focusLabel: '系统对象',
    checkpoints: ['组织树', '成员归属', '管理关系'],
    minRole: 'hospital_admin',
  },
  {
    path: 'positions',
    title: '岗位管理',
    subtitle: '维护岗位类型、映射角色和服务能力。',
    summary: '管理岗位定义，并控制岗位和系统角色映射。',
    focusLabel: '系统对象',
    checkpoints: ['岗位筛选', '岗位列表', '岗位编辑'],
    minRole: 'system_admin',
  },
  {
    path: 'institutions',
    title: '机构管理',
    subtitle: '管理机构名称、机构编码和企微入口。',
    summary: '为多企业微信主体维护机构编码、域名和企微应用配置。',
    focusLabel: '系统对象',
    checkpoints: ['机构配置', '公网入口', '企微应用'],
    minRole: 'system_admin',
  },
  {
    path: 'dingtalk-badge',
    title: '朗姿工牌',
    subtitle: '管理智能工牌设备，查看状态、绑定员工、控制录音。',
    summary: '设备列表、在线状态、绑定解绑、录音控制与音频文件。',
    focusLabel: '系统对象',
    checkpoints: ['设备列表', '绑定解绑', '录音控制'],
    minRole: 'hospital_admin',
  },
  {
    path: 'audit-logs',
    title: '操作日志',
    subtitle: '查看系统操作日志和登录记录。',
    summary: '用于追踪后台登录、人员变更和配置变动。',
    focusLabel: '系统对象',
    checkpoints: ['时间筛选', 'IP 检索', '日志列表'],
    minRole: 'system_admin',
  },
  {
    path: 'asr-monitoring',
    title: 'ASR监控',
    subtitle: '查看腾讯云 ASR 官方用量、额度状态和请求明细。',
    summary: '集中监控官方用量统计、本地精确请求审计和历史云审计请求记录。',
    focusLabel: '系统对象',
    checkpoints: ['官方用量', '额度状态', '请求明细'],
    minRole: 'system_admin',
  },
  {
    path: 'sap-push-monitoring',
    title: 'SAP回传',
    subtitle: '查看咨询单自动/手动回传结果与失败原因。',
    summary: '集中查看每次回传的最终结果、业务返回状态与失败原因。',
    focusLabel: '系统对象',
    checkpoints: ['结果总览', '失败原因', '自动回传'],
    minRole: 'system_admin',
  },
  {
    path: 'llm-results',
    title: '分析结果',
    navTitle: '分析结果',
    subtitle: '集中查看工牌归档录音的分析结论与评分明细。',
    summary: '汇总录音质检、面诊结果、过程评价和业务标签，方便快速复盘。',
    focusLabel: '业务对象',
    checkpoints: ['结果列表', '评分排序', '详情查看'],
    minRole: 'staff',
  },
  {
    path: 'recordings',
    title: '录音列表',
    navTitle: '录音列表',
    subtitle: '查看工牌归档录音、音频播放、逐字稿和分析结果。',
    summary: '按员工、机构、转写状态和分析状态筛选录音，快速定位处理进度。',
    focusLabel: '业务对象',
    checkpoints: ['录音筛选', '转写触发', '匹配入口'],
    minRole: 'staff',
  },
]

export const adminSidebarSections: SidebarSectionDefinition[] = [
  {
    key: 'overview',
    title: '管理驾驶舱',
    description: '系统总览与核心指标',
    items: [
      {
        path: 'dashboard',
        label: '数据总览',
        description: '查看指标和系统健康',
        minRole: 'staff',
      },
    ],
  },
  {
    key: 'customer-center',
    title: '客户中心',
    description: '客户与接诊工作台',
    items: [
      {
        path: 'visits',
        label: '接诊记录',
        description: '接诊单、录音关联与分析结果',
        minRole: 'staff',
      },
      {
        path: 'customers',
        label: '客户档案',
        description: '客户信息、动态与历史档案',
        minRole: 'staff',
      },
      {
        path: 'visit-orders',
        label: '到诊单据',
        description: '按录音上下文同步的到诊单据',
        minRole: 'staff',
      },
    ],
  },
  {
    key: 'configuration',
    title: '规则配置',
    description: '偏好、热词与标签规则配置',
    items: [
      {
        path: 'preferences',
        label: '偏好设置',
        description: '接诊参数、推送与交互偏好',
        minRole: 'system_admin',
      },
      {
        path: 'iot-capabilities',
        label: 'IOT配置',
        description: '额外 IOT 接口和回调开关',
        minRole: 'system_admin',
      },
      {
        path: 'hotwords',
        label: '热词管理',
        description: '竞品、顾虑和项目热词库',
        minRole: 'system_admin',
      },      
      {
        path: 'tag-packages',
        label: '标签配置',
        description: '标签和标签分类',
        minRole: 'system_admin',
      },
    ],
  },
  {
    key: 'recording-center',
    title: '录音复盘',
    description: '围绕录音内容查看音频、逐字稿和匹配结果',
    items: [
      {
        path: 'llm-results',
        label: '分析结果',
        description: '查看录音分析结果和评分明细',
        minRole: 'staff',
      },
      {
        path: 'recordings',
        label: '录音列表',
        description: '查看录音、转写和分析进度',
        minRole: 'staff',
      },
    ],
  },
  {
    key: 'system-center',
    title: '系统管理',
    description: '人员、设备与日志管理',
    items: [
      {
        path: 'staff',
        label: '人员管理',
        description: '人员、员工编号和设备工牌信息',
        minRole: 'hospital_admin',
      },
      {
        path: 'organization',
        label: '组织架构',
        description: '组织层级、成员归属和管理关系',
        minRole: 'hospital_admin',
      },
      {
        path: 'dingtalk-badge',
        label: '朗姿工牌',
        description: '设备状态、绑定与录音控制',
        minRole: 'hospital_admin',
      },
      {
        path: 'institutions',
        label: '机构管理',
        description: '企微主体、域名和机构绑定',
        minRole: 'hospital_admin',
      },
      {
        path: 'audit-logs',
        label: '操作日志',
        description: '系统操作和登录记录',
        minRole: 'system_admin',
      },
      {
        path: 'asr-monitoring',
        label: 'ASR监控',
        description: '腾讯云 ASR 用量和请求审计',
        minRole: 'system_admin',
      },
      {
        path: 'sap-push-monitoring',
        label: 'SAP回传',
        description: '咨询单回传结果与失败原因',
        minRole: 'system_admin',
      },
    ],
  },
]
