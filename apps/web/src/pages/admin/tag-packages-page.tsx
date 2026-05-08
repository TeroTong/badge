import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Card, Empty, Tag, App } from 'antd'
import { ImportOutlined } from '@ant-design/icons'

import type { TagCategory } from '@/api/admin'
import * as adminApi from '@/api/admin'
import { ANALYSIS_TAG_CATALOG_GROUPS } from '@/constants/tag-catalog'

const WEIGHT_META: Record<number, { label: string; color: string }> = {
  1: { label: '必问', color: '#f5222d' },
  2: { label: '重要', color: '#fa8c16' },
  3: { label: '一般', color: '#1890ff' },
  4: { label: '次要', color: '#8c8c8c' },
}

const NEGATIVE_PROJECT_TAG_NAME = '负面项目/设备/原材料'

function TagTokens({ category }: { category: TagCategory }) {
  const activeTags = category.tags.filter((t) => t.is_active).sort((a, b) => a.sort_order - b.sort_order)
  return (
    <div className="tag-package-card__tokens">
      {activeTags.length ? (
        activeTags.map((tag) => (
          <Tag key={tag.id} className="tag-package-token">{tag.name}</Tag>
        ))
      ) : (
        <span className="operation-table__muted">
          {category.name === NEGATIVE_PROJECT_TAG_NAME
            ? '开放值 — 未提取到填“无”，提取到填具体项目/设备/原材料名称'
            : '开放值 — 从对话中提取'}
        </span>
      )}
    </div>
  )
}

export function TagPackagesPage() {
  const { data: categories = [], isLoading } = useQuery({
    queryKey: ['tagCategories'],
    queryFn: adminApi.fetchCategories,
  })

  const queryClient = useQueryClient()
  const { message } = App.useApp()

  const importMutation = useMutation({
    mutationFn: () => {
      const items = ANALYSIS_TAG_CATALOG_GROUPS.flatMap((g) =>
        g.items.map((i) => ({ name: i.name, group: i.group, weight: i.weight, description: i.description, options: i.options })),
      )
      return adminApi.bulkImportTags(items)
    },
    onSuccess: (result) => {
      message.success(`导入完成：新增 ${result.categories_created} 个分类，${result.tags_created} 个标签`)
      queryClient.invalidateQueries({ queryKey: ['tagCategories'] })
    },
    onError: () => {
      message.error('导入失败，请重试')
    },
  })

  const hasCategories = categories.length > 0

  return (
    <div className="operation-page">
      <div className="operation-page__header">
        <div className="operation-page__title">
          <span className="operation-page__marker" aria-hidden="true" />
          <div>
            <h1>标签配置</h1>
            <p>按标签包沉淀客户求美需求、顾虑、偏好和画像标签，服务后续洞察和分析链路。</p>
          </div>
        </div>
        {hasCategories ? (
          <Tag color="blue">标签目录已导入</Tag>
        ) : (
          <Button
            type="primary"
            icon={<ImportOutlined />}
            loading={importMutation.isPending}
            onClick={() => importMutation.mutate()}
          >
            导入标签目录
          </Button>
        )}
      </div>

      <Card bordered={false} className="operation-card">
        <div className="tag-package-shell">
          <div className="tag-package-shell__heading">通用标签包</div>

          {isLoading ? null : categories.length ? (
            <div className="tag-package-list">
              {(() => {
                // 1) Group by weight_level
                const byWeight = new Map<number, TagCategory[]>()
                for (const cat of categories) {
                  const wl = cat.weight_level ?? 4
                  if (!byWeight.has(wl)) byWeight.set(wl, [])
                  byWeight.get(wl)!.push(cat)
                }

                return [...byWeight.entries()]
                  .sort(([a], [b]) => a - b)
                  .map(([wl, cats]) => {
                    const meta = WEIGHT_META[wl] ?? { label: `W${wl}`, color: '#8c8c8c' }

                    // 2) Within each weight, group by group_name
                    const subGroups: { label: string; cats: TagCategory[] }[] = []
                    let cur = ''
                    for (const cat of cats) {
                      const gn = cat.group_name ?? cat.name
                      if (gn !== cur) {
                        subGroups.push({ label: gn, cats: [] })
                        cur = gn
                      }
                      subGroups[subGroups.length - 1].cats.push(cat)
                    }

                    return (
                      <div key={wl} className="tag-package-weight-section">
                        <h2 className="tag-package-weight-section__title">
                          <Tag color={meta.color}>{meta.label}</Tag>
                          权重 {wl} 级
                        </h2>

                        <div className="tag-package-weight-section__body">
                          {subGroups.map((sg) => {
                            const isStandalone = sg.cats.length === 1 && sg.cats[0].name === sg.label
                            if (isStandalone) {
                              // 独立大类 — 直接显示为一张卡片
                              const cat = sg.cats[0]
                              return (
                                <div key={sg.label} className="tag-package-card tag-package-card--standalone">
                                  <header>
                                    <h3>{cat.name}</h3>
                                    {cat.description ? <p>{cat.description}</p> : null}
                                  </header>
                                  <TagTokens category={cat} />
                                </div>
                              )
                            }
                            // 大类 + 子标签
                            return (
                              <div key={sg.label} className="tag-package-card tag-package-card--parent">
                                <header>
                                  <h3>{sg.label}</h3>
                                </header>
                                <div className="tag-package-card__children">
                                  {sg.cats.map((cat) => (
                                    <div key={cat.id} className="tag-package-child">
                                      <div className="tag-package-child__header">
                                        <span className="tag-package-child__name">{cat.name}</span>
                                        {cat.description ? (
                                          <span className="tag-package-child__desc">{cat.description}</span>
                                        ) : null}
                                      </div>
                                      <TagTokens category={cat} />
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )
                          })}
                        </div>
                      </div>
                    )
                  })
              })()}
            </div>
          ) : (
            <Empty description="暂无标签包" image={Empty.PRESENTED_IMAGE_SIMPLE} />
          )}
        </div>
      </Card>
    </div>
  )
}

export default TagPackagesPage
