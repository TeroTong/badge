type KeepElementInViewOptions = {
  behavior?: ScrollBehavior
  topPadding?: number
  bottomPadding?: number
}

export function keepElementInScrollContainerView(
  container: HTMLElement | null,
  element: HTMLElement | null | undefined,
  options: KeepElementInViewOptions = {},
) {
  if (!container || !element || !container.contains(element)) {
    return
  }

  const containerRect = container.getBoundingClientRect()
  const elementRect = element.getBoundingClientRect()
  const topPadding = options.topPadding ?? Math.min(72, container.clientHeight * 0.22)
  const bottomPadding = options.bottomPadding ?? Math.min(96, container.clientHeight * 0.28)
  const visibleTop = containerRect.top + topPadding
  const visibleBottom = containerRect.bottom - bottomPadding
  const usableHeight = visibleBottom - visibleTop

  let nextTop = container.scrollTop
  if (elementRect.height > usableHeight || elementRect.top < visibleTop) {
    nextTop += elementRect.top - visibleTop
  } else if (elementRect.bottom > visibleBottom) {
    nextTop += elementRect.bottom - visibleBottom
  } else {
    return
  }

  const maxTop = Math.max(0, container.scrollHeight - container.clientHeight)
  const clampedTop = Math.max(0, Math.min(maxTop, nextTop))
  if (Math.abs(clampedTop - container.scrollTop) < 1) {
    return
  }

  container.scrollTo({
    top: clampedTop,
    behavior: options.behavior ?? 'smooth',
  })
}
