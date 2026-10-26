// Normalize FastAPI validation errors and protocol error envelopes for the UI.
export function errorMessage(data, fallback = '请求失败') {
  const detail = data?.detail ?? data?.error?.message
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map((item) => `${(item.loc || []).join('.')}: ${item.msg || '参数无效'}`).join('；')
  }
  return detail?.message || fallback
}
