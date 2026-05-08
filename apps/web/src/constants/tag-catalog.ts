/**
 * 完整的客户画像标签目录 — 与后端内置标签目录保持同步。
 *
 * 二级结构：大类(group) → 子标签(name)
 * 当 group === name 时表示独立标签（大类本身就是标签）。
 */

export type TagCatalogItem = {
  /** 标签名称，与后端 TagCategory.name 一致 */
  name: string
  /** 所属大类名称（group_name） */
  group: string
  /** 权重级别: 1=必问, 2=重要, 3=一般, 4=次要 */
  weight: number
  /** 中文描述 */
  description: string
  /** 可选值示例（空数组表示自由文本） */
  options: string[]
}

export type TagCatalogGroup = {
  weight: number
  label: string
  color: string
  items: TagCatalogItem[]
}

const W1_ITEMS: TagCatalogItem[] = [
  // 出生日期: standalone
  { name: '出生日期', group: '出生日期', weight: 1, description: '', options: [] },
  // 健康风险/禁忌: standalone
  { name: '健康风险/禁忌', group: '健康风险/禁忌', weight: 1, description: '无风险禁忌、过敏史、疤痕体质、备孕/妊娠/哺乳、精神类疾病、传染性疾病、高血压、糖尿病、心脑血管病、免疫系统疾病', options: ['无风险禁忌', '过敏史', '疤痕体质', '备孕/妊娠/哺乳', '精神类疾病', '传染性疾病', '高血压', '糖尿病', '心脑血管病', '免疫系统疾病'] },
  // 治疗历史: 大类 → 3 子标签
  { name: '治疗项目', group: '治疗历史', weight: 1, description: '手术类、注射类、光电类', options: ['手术类', '注射类', '光电类'] },
  { name: '历史用的设备/原材料名称', group: '治疗历史', weight: 1, description: '', options: [] },
  { name: '负面项目/设备/原材料', group: '治疗历史', weight: 1, description: '仅在已明确存在治疗历史但未提取到负面项目/设备/原材料时，或已明确客户未做过医美治疗时，填"无"；若既往治疗本身未提取到，则留空；提取到则填具体项目/设备/原材料名称', options: [] },
  // 倾向治疗方式: 大类 → 5 子标签
  { name: '创伤倾向', group: '倾向治疗方式', weight: 1, description: '手术、微创、皮肤', options: ['手术', '微创', '皮肤'] },
  { name: '疼痛耐受度', group: '倾向治疗方式', weight: 1, description: '高、中、低', options: ['高', '中', '低'] },
  { name: '效果要求', group: '倾向治疗方式', weight: 1, description: '即刻、长期', options: ['即刻', '长期'] },
  { name: '恢复期要求', group: '倾向治疗方式', weight: 1, description: '1-3天、1周、半个月、1个月以上', options: ['1-3天', '1周', '半个月', '1个月以上'] },
  { name: '治疗频次', group: '倾向治疗方式', weight: 1, description: '高频(1月1次)、中频（季度1次）、低频（半年以上1次）', options: ['高频(1月1次)', '中频（季度1次）', '低频（半年以上1次）'] },
  // 常驻城市: standalone
  { name: '常驻城市', group: '常驻城市', weight: 1, description: '外地、本地', options: ['外地', '本地'] },
]

const W2_ITEMS: TagCatalogItem[] = [
  // 成交影响因素: 大类 → 3 子标签
  { name: '医美目的', group: '成交影响因素', weight: 2, description: '悦己、社交、工作、情感', options: ['悦己', '社交', '工作', '情感'] },
  { name: '决策主体', group: '成交影响因素', weight: 2, description: '自主、伴侣、父母、儿女、其它', options: ['自主', '伴侣', '父母', '儿女', '其它'] },
  { name: '价格敏感度', group: '成交影响因素', weight: 2, description: '高、中、低', options: ['高', '中', '低'] },
]

const W3_ITEMS: TagCatalogItem[] = [
  // 家庭情况: 大类 → 3 子标签
  { name: '个人情况', group: '家庭情况', weight: 3, description: '单身、有恋人、已婚', options: ['单身', '有恋人', '已婚'] },
  { name: '亲属/子女情况', group: '家庭情况', weight: 3, description: '无孩、1孩、2孩及以上', options: ['无孩', '1孩', '2孩及以上'] },
  { name: '居住地址', group: '居住地址', weight: 3, description: '', options: [] },
  // 职业: 大类 → 2 子标签
  { name: '行业', group: '职业', weight: 3, description: '房地产/建筑/家居、服饰/美妆、广告/影视/会展、环保/化工/电力、计算机/互联网/通信/电子、教育/培训/法律、金融/保险、酒店/旅游、贸易、美容/保健、其他、汽车/机械、物流/运输/航天、医疗/制药、政府/公共事业、其它', options: ['房地产/建筑/家居', '服饰/美妆', '广告/影视/会展', '环保/化工/电力', '计算机/互联网/通信/电子', '教育/培训/法律', '金融/保险', '酒店/旅游', '贸易', '美容/保健', '其他', '汽车/机械', '物流/运输/航天', '医疗/制药', '政府/公共事业', '其它'] },
  { name: '职位', group: '职业', weight: 3, description: '员工、高管、老板、个体工商户、无业', options: ['员工', '高管', '老板', '个体工商户', '无业'] },
  { name: '特殊身份', group: '特殊身份', weight: 3, description: '黑名单、竞对同行', options: ['黑名单', '竞对同行'] },
]

const W4_ITEMS: TagCatalogItem[] = [
  // 其它信息: 大类 → 5 子标签
  { name: '教育程度', group: '其它信息', weight: 4, description: '研究生、本科、其它', options: ['研究生', '本科', '其它'] },
  { name: '交通工具', group: '其它信息', weight: 4, description: '打车、骑车、开车', options: ['打车', '骑车', '开车'] },
  { name: '业余爱好', group: '其它信息', weight: 4, description: '', options: [] },
  { name: '饮品偏好', group: '其它信息', weight: 4, description: '咖啡、茶、奶茶、果汁、气泡水、白水/矿泉水、功能饮料、酒精饮品、其它', options: ['咖啡', '茶', '奶茶', '果汁', '气泡水', '白水/矿泉水', '功能饮料', '酒精饮品', '其它'] },
  { name: '餐食偏好', group: '其它信息', weight: 4, description: '清淡、香辣、素食、清真、西餐、其它', options: ['清淡', '香辣', '素食', '清真', '西餐', '其它'] },
  { name: '倾向回访方式', group: '其它信息', weight: 4, description: '电话、微信、短信', options: ['电话', '微信', '短信'] },
  { name: '护肤习惯', group: '其它信息', weight: 4, description: '', options: [] },
]

export const TAG_CATALOG_GROUPS: TagCatalogGroup[] = [
  { weight: 1, label: '必问', color: '#f5222d', items: W1_ITEMS },
  { weight: 2, label: '重要', color: '#fa8c16', items: W2_ITEMS },
  { weight: 3, label: '一般', color: '#1890ff', items: W3_ITEMS },
  { weight: 4, label: '次要', color: '#8c8c8c', items: W4_ITEMS },
]

export const ANALYSIS_TAG_CATALOG_GROUPS: TagCatalogGroup[] = TAG_CATALOG_GROUPS

/** 全部标签分类名（扁平列表，按权重排序） */
export const ALL_TAG_NAMES: string[] = TAG_CATALOG_GROUPS.flatMap((g) => g.items.map((i) => i.name))
