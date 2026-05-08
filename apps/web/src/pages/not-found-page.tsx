import { Link } from 'react-router-dom'

export function NotFoundPage() {
  return (
    <section className="module-page">
      <header className="module-page__header">
        <div>
          <p className="eyebrow">404</p>
          <h1>页面不存在</h1>
          <p className="module-page__subtitle">当前路由还没有被映射到实际页面。</p>
        </div>
      </header>

      <article className="card card--wide">
        <p className="card__label">建议操作</p>
        <p className="card__body">
          回到 <Link to="/">首页</Link> 继续。
        </p>
      </article>
    </section>
  )
}

export default NotFoundPage
