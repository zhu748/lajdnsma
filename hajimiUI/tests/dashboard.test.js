import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useDashboardStore } from '../src/stores/dashboard'
import { errorMessage } from '../src/services/http'

const response = (data = {}, status = 200) => ({ ok: status < 400, status, json: async () => data })
const deferred = () => {
  let resolve
  const promise = new Promise((r) => { resolve = r })
  return { promise, resolve }
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  setActivePinia(createPinia())
  vi.stubGlobal('fetch', vi.fn())
})
afterEach(() => vi.unstubAllGlobals())

describe('dashboard session and polling', () => {
  it('does not unlock before the server verifies a saved password', async () => {
    sessionStorage.setItem('gw_pw', 'secret')
    const store = useDashboardStore()
    expect(store.isUnlocked).toBe(false)
    fetch.mockResolvedValue(response({ key_count: 2 }))
    await store.fetchDashboardData()
    expect(store.isUnlocked).toBe(true)
    expect(store.status.keyCount).toBe(2)
  })

  it('shares an in-flight fetch and waits for its result', async () => {
    const store = useDashboardStore()
    store.setSessionPassword('secret')
    const pending = deferred()
    fetch.mockReturnValue(pending.promise)
    const first = store.fetchDashboardData()
    const second = store.fetchDashboardData()
    expect(fetch).toHaveBeenCalledTimes(1)
    pending.resolve(response({ key_count: 7 }))
    await Promise.all([first, second])
    expect(store.status.keyCount).toBe(7)
    expect(store.isRefreshing).toBe(false)
  })

  it('ignores a late response after locking and clears visible data', async () => {
    const store = useDashboardStore()
    store.setSessionPassword('old')
    store.logs = [{ message: 'sensitive' }]
    const pending = deferred()
    fetch.mockReturnValue(pending.promise)
    const request = store.fetchDashboardData()
    const signal = fetch.mock.calls[0][1].signal
    store.setSessionPassword('')
    expect(signal.aborted).toBe(true)
    pending.resolve(response({ logs: [{ message: 'late' }] }))
    await request
    expect(store.logs).toEqual([])
    expect(store.isUnlocked).toBe(false)
    expect(store.lastUpdated).toBe(null)
  })

  it('cannot let a previous session 401 clear a new session', async () => {
    const store = useDashboardStore()
    const old = deferred()
    store.setSessionPassword('old')
    fetch.mockReturnValueOnce(old.promise).mockResolvedValueOnce(response({ key_count: 4 }))
    const first = store.fetchDashboardData()
    store.setSessionPassword('new')
    await store.fetchDashboardData()
    old.resolve(response({}, 401))
    await first
    expect(store.sessionPassword).toBe('new')
    expect(store.isUnlocked).toBe(true)
    expect(store.status.keyCount).toBe(4)
  })

  it('keeps the login locked on HTTP failure', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const store = useDashboardStore()
    store.setSessionPassword('secret')
    fetch.mockResolvedValue(response({}, 503))
    await store.fetchDashboardData()
    expect(store.isUnlocked).toBe(false)
    expect(store.lastError).toBe('HTTP 503')
    console.error.mockRestore()
  })

  it('drops invalid credentials and preserves an actionable error', async () => {
    const store = useDashboardStore()
    store.setSessionPassword('wrong')
    fetch.mockResolvedValue(response({}, 401))
    await store.fetchDashboardData()
    expect(store.sessionPassword).toBe('')
    expect(store.lastError).toBe('AUTH_FAILED')
    expect(store.isRefreshing).toBe(false)
  })

  it('formats validation errors instead of displaying object Object', () => {
    expect(errorMessage({ detail: [{ loc: ['body', 'value'], msg: 'must be positive' }] }))
      .toBe('body.value: must be positive')
  })
})

it('clears configuration and counters when the session changes', async () => {
  const store = useDashboardStore()
  store.setSessionPassword('secret')
  fetch.mockResolvedValue(response({ key_count: 2, pvp_key: 'private-id', max_requests_per_minute: 8 }))
  await store.fetchDashboardData()
  store.setSessionPassword('')
  expect(store.config.pvpKey).toBe('')
  expect(store.config.maxRequestsPerMinute).toBe(0)
  expect(store.status.keyCount).toBe(0)
})

it('cannot let a dashboard poll started before a save overwrite the saved config', async () => {
  const store = useDashboardStore()
  store.setSessionPassword('secret')
  const pending = deferred()
  fetch.mockReturnValueOnce(pending.promise).mockResolvedValueOnce(response({ success: true }))
  const poll = store.fetchDashboardData()
  await store.updateConfig('fakeStreaming', true)
  store.config.fakeStreaming = true
  pending.resolve(response({ fake_streaming: false }))
  await poll
  expect(store.config.fakeStreaming).toBe(true)
  expect(store.lastError).toBe('')
})

it('rejects the result of a config save from an old session', async () => {
  const store = useDashboardStore()
  store.setSessionPassword('secret')
  const pending = deferred()
  fetch.mockReturnValueOnce(pending.promise)
  const save = store.updateConfig('fakeStreaming', true)
  store.setSessionPassword('new')
  pending.resolve(response({ success: true }))
  await expect(save).rejects.toThrow('会话已变更')
})

it('applies stale-poll protection to model mapping updates too', async () => {
  const store = useDashboardStore()
  store.setSessionPassword('secret')
  const pending = deferred()
  fetch.mockReturnValueOnce(pending.promise).mockResolvedValueOnce(response({ success: true }))
  const poll = store.fetchDashboardData()
  await store.postConfig('/api/update-responses-model-mapping', { default_model: 'new', aliases: {} })
  store.config.responsesDefaultModel = 'new'
  pending.resolve(response({ responses_default_model: 'old' }))
  await poll
  expect(store.config.responsesDefaultModel).toBe('new')
})
