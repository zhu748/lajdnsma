import { afterEach, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { useDashboardStore } from '../src/stores/dashboard'
import StatusSection from '../src/components/dashboard/StatusSection.vue'

let wrapper
afterEach(() => { wrapper?.unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('submits reset once on repeated Enter and closes after the refreshed data arrives', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useDashboardStore()
  store.setSessionPassword('local-test')
  const refresh = vi.spyOn(store, 'fetchDashboardData').mockResolvedValue()
  let complete
  vi.stubGlobal('fetch', vi.fn(() => new Promise(resolve => { complete = resolve })))
  wrapper = mount(StatusSection, { attachTo: document.body, global: { plugins: [pinia], stubs: { StatusStats: true, ApiKeyStats: true } } })
  await wrapper.findAll('button').find(b => b.text().includes('重置计数')).trigger('click')
  const input = wrapper.get('input')
  await input.trigger('keyup', { key: 'Enter' })
  await input.trigger('keyup', { key: 'Enter' })
  expect(fetch).toHaveBeenCalledOnce()
  expect(wrapper.get('[aria-label="关闭重置计数窗口"]').attributes('disabled')).toBeDefined()
  complete({ ok: true, json: async () => ({}) })
  await flushPromises()
  expect(refresh).toHaveBeenCalledOnce()
  expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
})

it('aborts the pending reset request when the panel unmounts', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  useDashboardStore().setSessionPassword('local-test')
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
  wrapper = mount(StatusSection, { global: { plugins: [pinia], stubs: { StatusStats: true, ApiKeyStats: true } } })
  await wrapper.findAll('button').find(b => b.text().includes('重置计数')).trigger('click')
  await wrapper.get('input').trigger('keyup', { key: 'Enter' })
  const signal = fetch.mock.calls[0][1].signal
  wrapper.unmount()
  wrapper = null
  expect(signal.aborted).toBe(true)
})
