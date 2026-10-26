import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { useDashboardStore } from '../src/stores/dashboard'
import LogSection from '../src/components/dashboard/LogSection.vue'
import ConfigSection from '../src/components/dashboard/ConfigSection.vue'
import RequestActivity from '../src/components/dashboard/RequestActivity.vue'
import StatusStats from '../src/components/dashboard/status/StatusStats.vue'
import ApiKeyStats from '../src/components/dashboard/status/ApiKeyStats.vue'
import BasicConfig from '../src/components/dashboard/config/BasicConfig.vue'
import { managementRequest } from '../src/services/http'
let wrapper
beforeEach(() => { sessionStorage.clear(); setActivePinia(createPinia()); vi.stubGlobal('fetch', vi.fn()) })
afterEach(() => { wrapper?.unmount(); wrapper = null; vi.useRealTimers(); vi.unstubAllGlobals() })
const entry = i => ({ log_id: `log-${i}`, event: 'request_completed', request_id: `${i}`, level: 'INFO', message: 'completed', timestamp: 'now' })
it('follows large batches and preserves expansion by request identity', async () => {
  const store = useDashboardStore(); store.logs = Array.from({length:20}, (_,i) => entry(i))
  wrapper = mount(LogSection)
  const container = wrapper.get('.log-container').element; let top = 0
  Object.defineProperties(container, {scrollHeight:{get:()=>store.logs.length*30}, clientHeight:{get:()=>100}, scrollTop:{get:()=>top,set:v=>top=Math.max(0,Math.min(v,store.logs.length*30-100))}})
  await nextTick(); await nextTick()
  store.logs = [...store.logs, entry(20)]; await nextTick(); await nextTick()
  store.logs = [...store.logs, entry(21), entry(22), entry(23)]; await nextTick(); await nextTick()
  expect(top).toBe(container.scrollHeight-100)
  wrapper.findAll('details')[1].element.open = true
  store.logs = store.logs.slice(1); await nextTick(); await nextTick()
  expect(wrapper.findAll('details')[0].element.open).toBe(true)
  expect(wrapper.findAll('details')[0].text()).toContain('请求 1')
})
it('freezes logs until explicitly resumed', async () => {
  const store=useDashboardStore(); store.logs=[entry(1)]; wrapper=mount(LogSection)
  await wrapper.findAll('button').find(b=>b.text()==='暂停更新').trigger('click')
  store.logs=[entry(2)]; await nextTick()
  expect(wrapper.text()).toContain('请求 1'); expect(wrapper.text()).not.toContain('请求 2')
  await wrapper.findAll('button').find(b=>b.text()==='恢复实时').trigger('click')
  expect(wrapper.text()).toContain('请求 2')
})
it('retains collapsed drafts and discards only explicitly', async () => {
  const store=useDashboardStore(); store.isConfigLoaded=true; wrapper=mount(ConfigSection)
  await wrapper.findAll('button').find(b=>b.text()==='编辑').trigger('click')
  wrapper.findComponent(BasicConfig).vm.localConfig.maxRequestsPerMinute=22; await nextTick()
  expect(wrapper.text()).toContain('有未保存修改')
  await wrapper.findAll('button').find(b=>b.text()==='收起').trigger('click')
  await wrapper.findAll('button').find(b=>b.text()==='编辑').trigger('click')
  expect(wrapper.findComponent(BasicConfig).vm.localConfig.maxRequestsPerMinute).toBe(22)
  await wrapper.findAll('button').find(b=>b.text()==='取消修改').trigger('click')
  expect(wrapper.findComponent(BasicConfig).vm.localConfig.maxRequestsPerMinute).toBe(0)
})
it('clears old runtime and logs after a server restart', async () => {
  const store=useDashboardStore(); store.setSessionPassword('test')
  fetch.mockResolvedValueOnce({ok:true,status:200,json:async()=>({})}); await store.fetchDashboardData()
  let instance='one'
  fetch.mockImplementation(async url=>({ok:true,status:200,json:async()=>url.startsWith('/api/logs') ? {instance_id:instance,cursor:1,logs:[entry(instance)]} : {instance_id:instance,cursor:1,requests:[{request_id:instance,received_at:'2026-10-26T00:00:00Z',ended_at:'done'}]}}))
  await store.fetchLiveData(); instance='two'; await store.fetchLiveData()
  expect(store.monitor.requests.map(r=>r.request_id)).toEqual(['two'])
  expect(store.logs.map(l=>l.request_id)).toEqual(['two']); expect(store.logRetention.gap).toBe(true)
})
it('aborts management actions on timeout', async () => {
  vi.useFakeTimers(); fetch.mockImplementation((url,{signal})=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')))))
  const assertion=expect(managementRequest('/api/test',{}, {timeout:100})).rejects.toThrow('操作超时')
  await vi.advanceTimersByTimeAsync(100); await assertion
  expect(fetch.mock.calls[0][1].signal.aborted).toBe(true)
})

it('shows retry reasons, wait duration and key transitions in request details', async () => {
  const store = useDashboardStore()
  store.monitor.requests = [{request_id:'trace-one',path:'/v1/chat/completions',received_at:'2026-10-26T00:00:00Z',ended_at:'2026-10-26T00:00:01Z',duration_ms:1000,stage:'成功',outcome:'成功',usage:{total_tokens:12},attempts:[],timeline:[
    {label:'重试退避',elapsed_ms:10,retry_delay:.25,status_code:429,next_attempt_number:2,reason:'上游限流或配额耗尽'},
    {label:'切换密钥',elapsed_ms:260,previous_key:'key#A',next_key:'key#B',reason:'上一密钥配额耗尽'},
  ]}]
  wrapper = mount(RequestActivity)
  await wrapper.get('.request-id').trigger('click')
  const text = wrapper.get('.timeline').text()
  expect(text).toContain('上游限流或配额耗尽')
  expect(text).toContain('HTTP 429')
  expect(text).toContain('250 ms')
  expect(text).toContain('下一次尝试 #2')
  expect(text).toContain('key#A → key#B')
})
it('exposes exact values for every abbreviated overview metric', () => {
  const store = useDashboardStore()
  Object.assign(store.status, {last24hCalls:12345,last24hTokens:1234567,hourlyCalls:2345,minuteCalls:1234,hourlyTokens:45678,minuteTokens:12345})
  wrapper = mount(StatusStats)
  for (const expected of ['12,345','1,234,567','2,345','1,234','45,678']) {
    expect(wrapper.findAll('[title]').some(el => el.attributes('title') === expected)).toBe(true)
  }
})
it('exposes exact totals and per-model counts in the key table', async () => {
  const store = useDashboardStore()
  store.apiKeyStats = [{api_key:'key#demo',calls_24h:12345,total_tokens:1234567,model_stats:{test:{calls:2345,tokens:34567}}}]
  wrapper = mount(ApiKeyStats)
  await wrapper.findAll('button').find(b => b.text().includes('1 个模型')).trigger('click')
  for (const expected of ['12,345','1,234,567','2,345','34,567']) {
    expect(wrapper.findAll('[title]').some(el => el.attributes('title') === expected)).toBe(true)
  }
})
