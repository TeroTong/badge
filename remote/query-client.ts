import { QueryClient } from '@tanstack/react-query'

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      // 默认 30s 内重用缓存，避免页面切回时重复请求；后台数据保留 5 分钟。
      staleTime: 30_000,
      gcTime: 5 * 60_000,
    },
  },
})
