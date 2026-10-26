// Normalize FastAPI validation errors and protocol error envelopes for the UI.
export function errorMessage(data, fallback = '请求失败') {
  const detail = data?.detail ?? data?.error?.message
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map((item) => `${(item.loc || []).join('.')}: ${item.msg || '参数无效'}`).join('；')
  }
  return detail?.message || fallback
}

// The same timeout and cancellation behavior for every management action.
export async function managementRequest(url, payload, { signal, timeout = 15000 } = {}) {
  const controller = new AbortController()
  const abort = () => controller.abort()
  signal?.addEventListener('abort', abort, { once: true })
  if (signal?.aborted) controller.abort()
  const timer = setTimeout(abort, timeout)
  try {
    const response = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload), signal: controller.signal,
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(errorMessage(data, `HTTP ${response.status}`))
    return data
  } catch (error) {
    if (error.name === 'AbortError') throw new Error(signal?.aborted ? '操作已取消' : '操作超时，请刷新确认服务器状态')
    throw error
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', abort)
  }
}
