import { afterEach, beforeEach, it, expect, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia } from 'pinia'
import ApiKeyStats from '../src/components/dashboard/status/ApiKeyStats.vue'

let wrapper
beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('fetch', vi.fn())
  wrapper = mount(ApiKeyStats, { global: { plugins: [createPinia()], stubs: { ApiCallsChart: true } } })
})
afterEach(() => { wrapper.unmount(); vi.useRealTimers(); vi.unstubAllGlobals() })
const response = data => ({ ok: true, json: async () => data })
async function start() {
  await wrapper.findAll('button').find(b => b.text() === '测试').trigger('click')
  await wrapper.get('input[type=password]').setValue('secret')
  await wrapper.findAll('button').find(b => b.text() === '开始测试').trigger('click')
  await flushPromises()
  await vi.advanceTimersByTimeAsync(1500)
}

it('does not overlap slow progress requests and aborts one on unmount', async () => {
  fetch.mockResolvedValueOnce(response({})).mockImplementationOnce(() => new Promise(() => {}))
  await start()
  await vi.advanceTimersByTimeAsync(3000)
  expect(fetch).toHaveBeenCalledTimes(2)
  const signal = fetch.mock.calls[1][1].signal
  wrapper.unmount()
  expect(signal.aborted).toBe(true)
  await vi.advanceTimersByTimeAsync(30000)
  expect(fetch).toHaveBeenCalledTimes(2)
})

it('stops polling and reports a missing server task', async () => {
  fetch.mockResolvedValueOnce(response({})).mockResolvedValueOnce(response({ is_running: false, is_completed: false }))
  await start()
  await flushPromises()
  expect(wrapper.text()).toContain('测试任务已丢失')
  await vi.advanceTimersByTimeAsync(30000)
  expect(fetch).toHaveBeenCalledTimes(2)
})
