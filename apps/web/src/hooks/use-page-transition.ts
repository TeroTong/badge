import { useEffect, useState } from 'react'

export function usePageTransition({
  page,
  loadedPage,
  isFetching,
  isInitialLoading,
  isError = false,
  minDurationMs = 450,
}: {
  page: number
  loadedPage?: number | null
  isFetching: boolean
  isInitialLoading: boolean
  isError?: boolean
  minDurationMs?: number
}) {
  const [pageTransition, setPageTransition] = useState<{ page: number; startedAt: number } | null>(null)
  const isPageFetching = isFetching && !isInitialLoading
  const displayedPage = loadedPage ?? page
  const isShowingStalePage = displayedPage !== page
  const isPageTransitionVisible = Boolean(pageTransition) || isShowingStalePage
  const transitionPage = pageTransition?.page ?? page

  useEffect(() => {
    if (pageTransition && isError) {
      const timer = window.setTimeout(() => setPageTransition(null), 0)
      return () => window.clearTimeout(timer)
    }
    if (!pageTransition || loadedPage !== pageTransition.page || isFetching) return

    const elapsed = Date.now() - pageTransition.startedAt
    const delay = Math.max(0, minDurationMs - elapsed)
    const timer = window.setTimeout(() => setPageTransition(null), delay)
    return () => window.clearTimeout(timer)
  }, [isError, isFetching, loadedPage, minDurationMs, pageTransition])

  const beginPageTransition = (nextPage: number, force = false) => {
    if (force || nextPage !== page) {
      setPageTransition({ page: nextPage, startedAt: Date.now() })
    }
  }

  return {
    beginPageTransition,
    displayedPage,
    isPageFetching,
    isPageTransitionVisible,
    isShowingStalePage,
    transitionPage,
  }
}
