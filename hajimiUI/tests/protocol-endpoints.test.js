import { afterEach, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { useDashboardStore } from '../src/stores/dashboard'
import ProtocolEndpoints from '../src/components/dashboard/ProtocolEndpoints.vue'

let wrapper
afterEach(() => { wrapper?.unmount(); vi.unstubAllGlobals() })
function setup(vertex = false) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useDashboardStore()
  store.config.enableVertex = vertex
  wrapper = mount(ProtocolEndpoints, { global: { plugins: [pinia] } })
  return store
}
it('labels Gemini as AI Studio while other protocols follow the active backend', async () => {
  const store = setup(true)
  expect(wrapper.findAll('.protocol-backend').map(el => el.text())).toEqual(['Vertex AI', 'Vertex AI', 'Vertex AI', 'AI Studio'])
  store.config.enableVertex = false
  await flushPromises()
  expect(wrapper.findAll('.protocol-backend').every(el => el.text() === 'AI Studio')).toBe(true)
})
it('copies the complete endpoint without exposing credentials', async () => {
  const writeText = vi.fn().mockResolvedValue()
  vi.stubGlobal('navigator', { clipboard: { writeText } })
  const store = setup()
  store.setSessionPassword('never-copy-this')
  await wrapper.get('[aria-label="复制 Responses 接口地址"]').trigger('click')
  await flushPromises()
  expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/v1/responses`)
  expect(wrapper.get('[role="status"]').text()).toContain('已复制')
})
it('provides feedback when clipboard access is unavailable', async () => {
  vi.stubGlobal('navigator', {})
  setup()
  await wrapper.get('button').trigger('click')
  await flushPromises()
  expect(wrapper.get('[role="status"]').text()).toContain('手动复制')
  expect(wrapper.get('code').text()).toBe('/v1/chat/completions')
})
