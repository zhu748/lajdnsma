export const exact = value => value == null ? '未提供' : Number(value).toLocaleString('zh-CN')
export const duration = value => value == null ? '—' : value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(2)} s`
export const dateTime = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
