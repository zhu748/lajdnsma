import { beforeEach, afterEach, it, expect, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia } from 'pinia'
import DashboardView from '../src/views/DashboardView.vue'

let wrapper
beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.stubGlobal('fetch', vi.fn())
  vi.spyOn(console, 'error').mockImplementation(() => {})
  wrapper = mount(DashboardView, {
    global: { plugins: [createPinia()], stubs: { StatusSection: true, ConfigSection: true, LogSection: true } },
  })
})
afterEach(() => { wrapper.unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('keeps the password form visible and displays failed network verification', async () => {
  fetch.mockResolvedValue({ ok: false, status: 503 })
  await wrapper.get('#lock-pw').setValue('test')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(wrapper.find('#lock-pw').exists()).toBe(true)
  expect(wrapper.text()).toContain('HTTP 503')
})

it('unlocks after verification, exposes mode-switch errors, and can lock again', async () => {
  fetch.mockImplementation(async url => url === '/api/update-config'
    ? { ok: false, status: 400, json: async () => ({ detail: '尚未配置 Vertex 凭证' }) }
    : { ok: true, status: 200, json: async () => ({}) })
  await wrapper.get('#lock-pw').setValue('test')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(wrapper.find('#lock-pw').exists()).toBe(false)
  await wrapper.findAll('button').find(b => b.text().startsWith('Vertex')).trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('尚未配置 Vertex 凭证')
  await wrapper.findAll('button').find(b => b.text() === '锁定').trigger('click')
  expect(wrapper.find('#lock-pw').exists()).toBe(true)
})
