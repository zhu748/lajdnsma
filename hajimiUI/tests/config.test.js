import { beforeEach, afterEach, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import BasicConfig from '../src/components/dashboard/config/BasicConfig.vue'
import FeaturesConfig from '../src/components/dashboard/config/FeaturesConfig.vue'
import ConfigSection from '../src/components/dashboard/ConfigSection.vue'
import { useDashboardStore } from '../src/stores/dashboard'

let wrapper
beforeEach(() => { sessionStorage.clear(); setActivePinia(createPinia()) })
afterEach(() => { wrapper?.unmount(); vi.restoreAllMocks() })

it.each([BasicConfig, FeaturesConfig])('refreshes untouched fields and preserves pending edits', async Component => {
  const store = useDashboardStore()
  store.isConfigLoaded = true
  wrapper = mount(Component)
  const key = Component === BasicConfig ? 'maxRequestsPerMinute' : 'maxRetryNum'
  store.config[key] = 5
  await nextTick()
  expect(wrapper.vm.localConfig[key]).toBe(5)
  wrapper.vm.localConfig[key] = 9
  store.config[key] = 7
  await nextTick()
  expect(wrapper.vm.localConfig[key]).toBe(9)
})

it('records the submitted value even if the draft changes during a save', async () => {
  const store = useDashboardStore()
  store.isConfigLoaded = true
  wrapper = mount(BasicConfig)
  let resolve
  vi.spyOn(store, 'updateConfig').mockImplementation(() => new Promise(r => { resolve = r }))
  wrapper.vm.localConfig.maxRequestsPerMinute = 5
  const save = wrapper.vm.saveComponentConfigs('secret')
  wrapper.vm.localConfig.maxRequestsPerMinute = 9
  resolve({})
  await save
  expect(store.config.maxRequestsPerMinute).toBe(5)
  expect(wrapper.vm.localConfig.maxRequestsPerMinute).toBe(9)
})

it('preserves edits back to the old server value while a save is pending', async () => {
  const store = useDashboardStore()
  store.isConfigLoaded = true
  wrapper = mount(BasicConfig)
  let complete
  vi.spyOn(store, 'updateConfig').mockImplementation(() => new Promise(resolve => { complete = resolve }))
  wrapper.vm.localConfig.maxRequestsPerMinute = 5
  const save = wrapper.vm.saveComponentConfigs('secret')
  wrapper.vm.localConfig.maxRequestsPerMinute = 0
  complete({})
  await save
  await nextTick()
  expect(store.config.maxRequestsPerMinute).toBe(5)
  expect(wrapper.vm.localConfig.maxRequestsPerMinute).toBe(0)
})

it.each(['session', 'unmount'])('stops subsequent config writes after %s changes', async reason => {
  const store = useDashboardStore()
  store.isConfigLoaded = true
  wrapper = mount(BasicConfig)
  let complete
  const update = vi.spyOn(store, 'updateConfig').mockImplementation(() => new Promise(resolve => { complete = resolve }))
  wrapper.vm.localConfig.maxRequestsPerMinute = 5
  wrapper.vm.localConfig.maxRequestsPerDayPerIp = 9
  const save = wrapper.vm.saveComponentConfigs('secret')
  if (reason === 'session') store.setSessionPassword('new')
  else { wrapper.unmount(); wrapper = null }
  complete({})
  await expect(save).rejects.toThrow(reason === 'session' ? '会话已变更' : '配置面板已关闭')
  expect(update).toHaveBeenCalledOnce()
  expect(store.config.maxRequestsPerMinute).toBe(0)
})

it('skips unchanged model mappings and keeps edits made during a mapping save', async () => {
  const store = useDashboardStore()
  store.isConfigLoaded = true
  wrapper = mount(FeaturesConfig)
  const update = vi.spyOn(store, 'updateConfig').mockResolvedValue({})
  const post = vi.spyOn(store, 'postConfig').mockResolvedValue({})
  await wrapper.vm.saveComponentConfigs('secret')
  expect(post).not.toHaveBeenCalled()
  let complete
  post.mockImplementationOnce(() => new Promise(resolve => { complete = resolve }))
  wrapper.vm.localConfig.responsesDefaultModel = 'first-model'
  const save = wrapper.vm.saveComponentConfigs('secret')
  wrapper.vm.localConfig.responsesDefaultModel = 'second-model'
  complete({})
  await save
  expect(store.config.responsesDefaultModel).toBe('first-model')
  expect(wrapper.vm.localConfig.responsesDefaultModel).toBe('second-model')
  expect(post).toHaveBeenCalledOnce()
})

it.each([
  ['maxThinkingLevels', [{ model: 'test', level: 'invalid' }]],
  ['maxThinkingLevels', [{ model: 'test', level: '-2' }]],
  ['maxThinkingLevels', [{ model: 'test', level: 'high' }, { model: 'test', level: 'low' }]],
  ['responsesModelAliases', [{ alias: 'test', model: '' }]],
  ['responsesModelAliases', [{ alias: 'test', model: 'a' }, { alias: 'test', model: 'b' }]],
  ['maxRetryNum', 1.5],
  ['fakeStreamingInterval', Infinity],
])('validates %s before writing any feature settings', async (key, value) => {
  const store = useDashboardStore()
  store.isConfigLoaded = true
  wrapper = mount(FeaturesConfig)
  const update = vi.spyOn(store, 'updateConfig').mockResolvedValue({})
  const post = vi.spyOn(store, 'postConfig').mockResolvedValue({})
  wrapper.vm.localConfig.searchMode = true
  wrapper.vm.localConfig[key] = value
  const result = await wrapper.vm.saveComponentConfigs('secret')
  expect(result.success).toBe(false)
  expect(update).not.toHaveBeenCalled()
  expect(post).not.toHaveBeenCalled()
})

it('does not silently discard prototype-named model aliases', async () => {
  const store = useDashboardStore()
  store.isConfigLoaded = true
  wrapper = mount(FeaturesConfig)
  const post = vi.spyOn(store, 'postConfig').mockResolvedValue({})
  wrapper.vm.localConfig.responsesModelAliases = [{ alias: '__proto__', model: 'test' }]
  expect((await wrapper.vm.saveComponentConfigs('secret')).success).toBe(true)
  expect(Object.hasOwn(post.mock.calls[0][1].aliases, '__proto__')).toBe(true)
})

it('checks both panels before Save All writes basic configuration', async () => {
  const store = useDashboardStore()
  store.isConfigLoaded = true
  const update = vi.spyOn(store, 'updateConfig').mockResolvedValue({})
  wrapper = mount(ConfigSection)
  await wrapper.findAll('button').find(button => button.text() === '编辑').trigger('click')
  wrapper.findComponent(BasicConfig).vm.localConfig.maxRequestsPerMinute = 10
  wrapper.findComponent(FeaturesConfig).vm.localConfig.maxThinkingLevels = [{ model: 'test', level: 'bad' }]
  await wrapper.find('input[type="password"]').setValue('secret')
  await wrapper.findAll('button').find(button => button.text() === '全部保存').trigger('click')
  expect(update).not.toHaveBeenCalled()
  expect(wrapper.text()).toContain('思考等级值无效')
})
