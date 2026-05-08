type WecomPageIntroTone = 'sky' | 'mint' | 'violet' | 'amber' | 'slate'
type WecomPageIntroChipTone = 'blue' | 'emerald' | 'amber' | 'slate' | 'rose'

type WecomPageIntroChip = {
  label: string
  tone?: WecomPageIntroChipTone
}

type WecomPageIntroStat = {
  label: string
  value: string
}

export function WecomPageIntro({
  eyebrow,
  title,
  description,
  tone = 'sky',
  chips = [],
  stats = [],
}: {
  eyebrow: string
  title?: string
  description?: string
  tone?: WecomPageIntroTone
  chips?: WecomPageIntroChip[]
  stats?: WecomPageIntroStat[]
}) {
  const hasVisibleContent = Boolean(title || description || chips.length > 0 || stats.length > 0)
  if (!hasVisibleContent) return null

  return (
    <section className={`wc-page-intro wc-page-intro--${tone}`}>
      <div className="wc-page-intro__copy">
        <span className="wc-page-intro__eyebrow">{eyebrow}</span>
        {title ? <h2 className="wc-page-intro__title">{title}</h2> : null}
        {description ? <p className="wc-page-intro__desc">{description}</p> : null}
      </div>

      {chips.length > 0 ? (
        <div className="wc-page-intro__chips">
          {chips.map((chip) => (
            <span
              key={`${chip.label}-${chip.tone ?? 'blue'}`}
              className={`wc-page-intro__chip wc-page-intro__chip--${chip.tone ?? 'blue'}`}
            >
              {chip.label}
            </span>
          ))}
        </div>
      ) : null}

      {stats.length > 0 ? (
        <div className="wc-page-intro__stats">
          {stats.map((stat) => (
            <div key={stat.label} className="wc-page-intro__stat">
              <label>{stat.label}</label>
              <strong>{stat.value}</strong>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  )
}

export default WecomPageIntro
