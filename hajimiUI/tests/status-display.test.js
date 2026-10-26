import { expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { useDashboardStore } from '../src/stores/dashboard'
import StatusStats from '../src/components/dashboard/status/StatusStats.vue'
import ApiKeyStats from '../src/components/dashboard/status/ApiKeyStats.vue'

it('keeps empty-state guidance outside the scrollable table and skips the chart until data arrives', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useDashboardStore()
  const wrapper = mount(ApiKeyStats, { global: { plugins: [pinia], stubs: { ApiCallsChart: true } } })
  try {
    expect(wrapper.find('.key-table-scroll').exists()).toBe(false)
    expect(wrapper.text()).toContain('点击「+ 添加」导入密钥。')
    expect(wrapper.findComponent({ name: 'ApiCallsChart' }).exists()).toBe(false)
    store.apiKeyStats = [{ api_key: 'masked', calls_24h: 1, total_tokens: 2, usage_percent: 1 }]
    store.timeSeriesData.calls = [{ time: '12:00', value: 1 }]
    await nextTick()
    expect(wrapper.find('.key-table-scroll').exists()).toBe(true)
    expect(wrapper.find('api-calls-chart-stub').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('密钥池为空')
  } finally { wrapper.unmount() }
})

it('shows stale data explicitly after a failed poll and clears the warning on recovery', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useDashboardStore()
  store.lastUpdated = new Date()
  const wrapper = mount(StatusStats, { global: { plugins: [pinia] } })
  try {
    expect(wrapper.get('.freshness-bar').text()).toBe('刚刚更新')
    store.lastError = 'HTTP 503'
    await nextTick()
    expect(wrapper.get('.freshness-bar').text()).toContain('更新失败 · 上次同步')
    expect(wrapper.find('.freshness-bar__dot--stale').exists()).toBe(true)
    store.lastError = ''
    await nextTick()
    expect(wrapper.get('.freshness-bar').text()).toBe('刚刚更新')
    expect(wrapper.find('.freshness-bar__dot--stale').exists()).toBe(false)
  } finally { wrapper.unmount() }
})
