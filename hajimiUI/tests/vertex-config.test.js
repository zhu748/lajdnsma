import { beforeEach, afterEach, it, expect, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import VertexConfig from '../src/components/dashboard/config/VertexConfig.vue'
import { useDashboardStore } from '../src/stores/dashboard'

let wrapper, store
beforeEach(() => {
  setActivePinia(createPinia())
  store = useDashboardStore()
  store.isConfigLoaded = true
  vi.spyOn(store, 'fetchDashboardData').mockResolvedValue()
  wrapper = mount(VertexConfig)
})
afterEach(() => { wrapper?.unmount(); vi.restoreAllMocks() })

it('refreshes untouched switches while preserving edits and keeping stored secrets out of inputs', async () => {
  store.config.fakeStreaming = true
  store.config.vertexExpressApiKey = true
  await nextTick()
  expect(wrapper.get('[aria-label="伪流式"]').attributes('aria-checked')).toBe('true')
  expect(wrapper.get('#vertex-key').element.value).toBe('')
  expect(wrapper.get('#vertex-key').attributes('placeholder')).toContain('已配置')
  await wrapper.get('[aria-label="伪流式"]').trigger('click')
  store.config.enableVertexExpress = true
  await nextTick()
  expect(wrapper.get('[aria-label="伪流式"]').attributes('aria-checked')).toBe('false')
  expect(wrapper.get('[aria-label="Vertex Express"]').attributes('aria-checked')).toBe('true')
})

it('saves one snapshot, preserves newer credential edits, and never sends blank credentials', async () => {
  let complete
  const update = vi.spyOn(store, 'updateConfig').mockImplementation(() => new Promise(resolve => { complete = resolve }))
  await wrapper.get('#vertex-key').setValue('first-key')
  await wrapper.get('#vertex-password').setValue('admin')
  const save = wrapper.vm.saveAll()
  await wrapper.get('#vertex-key').setValue('second-key')
  await wrapper.vm.saveAll() // rapid repeat while the first save is pending
  expect(update).toHaveBeenCalledOnce()
  complete({})
  await save
  expect(update).toHaveBeenCalledWith('vertexExpressApiKey', 'first-key', 'admin')
  expect(wrapper.get('#vertex-key').element.value).toBe('second-key')
  expect(store.config.vertexExpressApiKey).toBe(true)
  expect(wrapper.get('#vertex-password').element.value).toBe('')
  update.mockResolvedValue({})
  await wrapper.get('#vertex-password').setValue('admin')
  await wrapper.vm.saveAll()
  expect(update).toHaveBeenLastCalledWith('vertexExpressApiKey', 'second-key', 'admin')
  expect(wrapper.get('#vertex-key').element.value).toBe('')
  expect(update.mock.calls.some(([key]) => key === 'googleCredentialsJson')).toBe(false)
})

it('stops the rest of a save after logout and does not restore old config', async () => {
  let complete
  const update = vi.spyOn(store, 'updateConfig').mockImplementation(() => new Promise(resolve => { complete = resolve }))
  await wrapper.get('#vertex-key').setValue('first-key')
  await wrapper.get('#vertex-password').setValue('admin')
  await wrapper.get('[aria-label="Vertex Express"]').trigger('click')
  const save = wrapper.vm.saveAll()
  store.setSessionPassword('new-session')
  complete({})
  await save
  await flushPromises()
  expect(update).toHaveBeenCalledOnce()
  expect(store.config.vertexExpressApiKey).toBe(false)
  expect(wrapper.get('[role="alert"]').text()).toContain('会话已变更')
})

it('preserves a switch changed back to its previous value during submission', async () => {
  let complete
  vi.spyOn(store, 'updateConfig').mockImplementation(() => new Promise(resolve => { complete = resolve }))
  await wrapper.get('#vertex-password').setValue('admin')
  await wrapper.get('[aria-label="伪流式"]').trigger('click')
  const save = wrapper.vm.saveAll()
  await wrapper.get('[aria-label="伪流式"]').trigger('click')
  complete({})
  await save
  await nextTick()
  expect(store.config.fakeStreaming).toBe(true)
  expect(wrapper.get('[aria-label="伪流式"]').attributes('aria-checked')).toBe('false')
})
