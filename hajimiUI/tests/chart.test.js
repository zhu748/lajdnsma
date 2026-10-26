import { it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { useDashboardStore } from '../src/stores/dashboard'
import ApiCallsChart from '../src/components/dashboard/status/ApiCallsChart.vue'

const chart = vi.hoisted(() => ({ setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() }))
vi.mock('echarts/core', () => ({
  use: vi.fn(), init: () => chart,
  graphic: { LinearGradient: class {} },
}))
vi.mock('echarts/charts', () => ({ LineChart: {} }))
vi.mock('echarts/components', () => ({ GridComponent: {}, TooltipComponent: {}, LegendComponent: {} }))
vi.mock('echarts/renderers', () => ({ CanvasRenderer: {} }))

it('refreshes when poll replaces the series arrays and disposes on unmount', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useDashboardStore()
  const wrapper = mount(ApiCallsChart, { global: { plugins: [pinia] } })
  store.setSessionPassword('test')
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({
    calls_time_series: [{ time: '12:00', value: 5 }],
    tokens_time_series: [{ time: '12:00', value: 100 }],
  }) }))
  await store.fetchDashboardData()
  await nextTick()
  await nextTick()
  const option = chart.setOption.mock.calls.at(-1)[0]
  expect(option.series[0].data).toEqual([5])
  expect(option.series[1].data).toEqual([100])
  wrapper.unmount()
  expect(chart.dispose).toHaveBeenCalledOnce()
  vi.unstubAllGlobals()
})
