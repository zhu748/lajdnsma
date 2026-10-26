import { defineStore } from 'pinia'
import { ref, watch, computed } from 'vue'
import { errorMessage } from '../services/http'

export const useDashboardStore = defineStore('dashboard', () => {
  // ---------- Session password (post-hardening: every dashboard-data
  // fetch now requires the operator password, so we keep it in memory
  // for the lifetime of the tab and persist it in sessionStorage to
  // survive a page reload). ----------
  const sessionPassword = ref(sessionStorage.getItem('gw_pw') || '')
  const isAuthenticated = ref(false)
  let sessionVersion = 0
  let activeFetch = null
  let dataVersion = 0

  function setSessionPassword(pw) {
    sessionVersion += 1
    activeFetch?.controller.abort()
    activeFetch = null
    isRefreshing.value = false
    isAuthenticated.value = false
    isConfigLoaded.value = false
    status.value = structuredClone(defaultStatus)
    config.value = structuredClone(defaultConfig)
    selectedModel.value = 'all'
    lastUpdated.value = null
    logs.value = []
    apiKeyStats.value = []
    availableModels.value = []
    timeSeriesData.value = { calls: [], tokens: [] }
    sessionPassword.value = pw || ''
    if (pw) {
      sessionStorage.setItem('gw_pw', pw)
    } else {
      sessionStorage.removeItem('gw_pw')
    }
  }

  const isUnlocked = computed(() => Boolean(sessionPassword.value) && isAuthenticated.value)

  // ---------- Stats ----------
  const status = ref({
    keyCount: 0,
    modelCount: 0,
    retryCount: 0,
    last24hCalls: 0,
    hourlyCalls: 0,
    minuteCalls: 0,
    last24hTokens: 0,
    hourlyTokens: 0,
    minuteTokens: 0,
    enableVertex: false,
  })

  const timeSeriesData = ref({
    calls: [],
    tokens: [],
  })

  const config = ref({
    maxRequestsPerMinute: 0,
    maxRequestsPerDayPerIp: 0,
    currentTime: '',
    fakeStreaming: false,
    fakeStreamingInterval: 0,
    randomString: false,
    randomStringLength: 0,
    searchMode: false,
    searchPrompt: '',
    enableThinking: true,
    maxThinking: true,
    maxThinkingModelLevels: {},
    localVersion: '',
    remoteVersion: '',
    hasUpdate: false,
    concurrentRequests: 0,
    increaseConcurrentOnFailure: 0,
    maxConcurrentRequests: 0,
    maxRetryNum: 0,
    maxEmptyResponses: 0,
    pvpMode: false,
    pvpKey: '',
    pvpMaxRetries: 50,
    responsesDefaultModel: '',
    responsesModelAliases: {},
    claudeDefaultModel: '',
    claudeModelAliases: {},
    enableVertex: false,
    enableVertexExpress: false,
    vertexExpressApiKey: false,
    googleCredentialsJson: false,
    keyRotationStrategy: 'fill',
  })

  const defaultStatus = JSON.parse(JSON.stringify(status.value))
  const defaultConfig = JSON.parse(JSON.stringify(config.value))

  const apiKeyStats = ref([])
  const logs = ref([])
  const availableModels = ref([])
  const selectedModel = ref('all')

  const isRefreshing = ref(false)
  const isConfigLoaded = ref(false)
  const lastError = ref('')
  // 最近一次成功拉取数据的时间（用于头部「最后更新」指示器）
  const lastUpdated = ref(null)

  // ---------- Dark mode ----------
  const isDarkMode = ref(localStorage.getItem('darkMode') === 'true')

  function applyDarkMode(isDark) {
    if (isDark) {
      document.documentElement.classList.add('dark-mode')
      document.documentElement.classList.remove('light-mode')
    } else {
      document.documentElement.classList.remove('dark-mode')
      document.documentElement.classList.add('light-mode')
    }
  }

  watch(isDarkMode, (v) => {
    localStorage.setItem('darkMode', v)
    applyDarkMode(v)
  })
  applyDarkMode(isDarkMode.value)

  // ---------- Auth helper ----------
  // 凭证只通过 Authorization 头传递。曾有一个 authQuery() 把密码拼进
  // ?password=... 查询串（与 Header 双重发送）——查询串会完整落入
  // uvicorn access log 与反向代理日志，已删除；后端两种方式都接受。
  function authHeaders(extra = {}) {
    const h = { ...extra }
    if (sessionPassword.value) {
      h['Authorization'] = `Bearer ${sessionPassword.value}`
    }
    return h
  }

  // ---------- Data fetching ----------
  async function fetchDashboardData() {
    if (!sessionPassword.value) {
      lastError.value = 'PASSWORD_REQUIRED'
      return
    }
    if (activeFetch) return activeFetch.promise
    isRefreshing.value = true
    lastError.value = ''
    const version = sessionVersion
    const dataRevision = dataVersion
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 15000)
    const operation = { controller, promise: null }
    activeFetch = operation
    operation.promise = (async () => { try {
      const response = await fetch('/api/dashboard-data', {
        headers: authHeaders(),
        signal: controller.signal,
      })
      if (version !== sessionVersion) return
      if (response.status === 401) {
        // Session password is wrong/expired — drop it so the lock
        // screen reappears.
        setSessionPassword('')
        lastError.value = 'AUTH_FAILED'
        return
      }
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`)
      }
      const data = await response.json()
      if (version !== sessionVersion) return
      if (dataRevision !== dataVersion) return
      updateDashboardData(data)
      isAuthenticated.value = true
      lastUpdated.value = new Date()
    } catch (err) {
      if (version !== sessionVersion || dataRevision !== dataVersion) return
      console.error('fetch dashboard-data:', err)
      lastError.value = err.name === 'AbortError' ? '请求超时，请重试' : err.message || 'NETWORK'
    } finally {
      clearTimeout(timer)
      if (activeFetch === operation) {
        activeFetch = null
        isRefreshing.value = false
      }
    } })()
    return operation.promise
  }

  function updateDashboardData(data) {
    status.value = {
      keyCount: data.key_count || 0,
      modelCount: data.model_count || 0,
      retryCount: data.retry_count || 0,
      last24hCalls: data.last_24h_calls || 0,
      hourlyCalls: data.hourly_calls || 0,
      minuteCalls: data.minute_calls || 0,
      last24hTokens: data.last_24h_tokens || 0,
      hourlyTokens: data.hourly_tokens || 0,
      minuteTokens: data.minute_tokens || 0,
      enableVertex: data.enable_vertex || false,
    }

    if (data.calls_time_series) timeSeriesData.value.calls = data.calls_time_series
    if (data.tokens_time_series) timeSeriesData.value.tokens = data.tokens_time_series

    config.value = {
      ...config.value,
      maxRequestsPerMinute: data.max_requests_per_minute || 0,
      maxRequestsPerDayPerIp: data.max_requests_per_day_per_ip || 0,
      currentTime: data.current_time || '',
      fakeStreaming: data.fake_streaming || false,
      fakeStreamingInterval: data.fake_streaming_interval || 0,
      randomString: data.random_string || false,
      randomStringLength: data.random_string_length || 0,
      searchMode: data.search_mode || false,
      searchPrompt: data.search_prompt || '',
      enableThinking: data.enable_thinking || false,
      maxThinking: data.max_thinking || false,
      maxThinkingModelLevels: data.max_thinking_model_levels || {},
      localVersion: data.local_version || '',
      remoteVersion: data.remote_version || '',
      hasUpdate: data.has_update || false,
      concurrentRequests: data.concurrent_requests || 0,
      increaseConcurrentOnFailure: data.increase_concurrent_on_failure || 0,
      maxConcurrentRequests: data.max_concurrent_requests || 0,
      enableVertex: data.enable_vertex || false,
      enableVertexExpress: data.enable_vertex_express || false,
      vertexExpressApiKey: data.vertex_express_api_key || false,
      googleCredentialsJson: data.google_credentials_json || false,
      maxRetryNum: data.max_retry_num || 0,
      maxEmptyResponses: data.max_empty_responses || 0,
      pvpMode: data.pvp_mode || false,
      pvpKey: data.pvp_key || '',
      pvpMaxRetries: data.pvp_max_retries || 50,
      responsesDefaultModel: data.responses_default_model || '',
      responsesModelAliases: data.responses_model_aliases || {},
      claudeDefaultModel: data.claude_default_model || '',
      claudeModelAliases: data.claude_model_aliases || {},
      keyRotationStrategy: data.key_rotation_strategy || 'fill',
    }

    if (data.api_key_stats) {
      apiKeyStats.value = data.api_key_stats.map((stat) => ({
        ...stat,
        model_stats: Object.entries(stat.model_stats || {}).reduce(
          (acc, [model, d]) => {
            acc[model] = {
              calls: typeof d === 'object' ? d.calls : d,
              tokens: typeof d === 'object' ? d.tokens : 0,
            }
            return acc
          },
          {}
        ),
      }))

      if (data.available_models && Array.isArray(data.available_models)) {
        availableModels.value = data.available_models
      } else {
        const models = new Set(['all'])
        data.api_key_stats.forEach((stat) => {
          if (stat.model_stats) {
            Object.keys(stat.model_stats).forEach((m) => models.add(m))
          }
        })
        availableModels.value = Array.from(models)
      }

      if (!availableModels.value.includes(selectedModel.value)) {
        selectedModel.value = 'all'
      }
    }

    if (data.logs) logs.value = data.logs
    isConfigLoaded.value = true
  }

  function setSelectedModel(model) {
    selectedModel.value = model
  }

  function toggleDarkMode() {
    isDarkMode.value = !isDarkMode.value
  }

  // ---------- Config update ----------
  function invalidateDashboardFetch() {
    dataVersion += 1
    activeFetch?.controller.abort()
    activeFetch = null
    isRefreshing.value = false
  }

  function captureSession() {
    const version = sessionVersion
    return () => {
      if (version !== sessionVersion) throw new Error('会话已变更，请重新登录')
    }
  }

  async function postConfig(endpoint, payload, password) {
    const version = sessionVersion
    invalidateDashboardFetch()
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 15000)
    try {
      const response = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
        body: JSON.stringify({ ...payload, password: password || sessionPassword.value }),
      })
      const data = await response.json().catch(() => ({}))
      if (version !== sessionVersion) throw new Error('会话已变更，请重新登录')
      if (!response.ok) throw new Error(errorMessage(data, `HTTP ${response.status}`))
      return data
    } catch (err) {
      if (err.name === 'AbortError') throw new Error('保存超时，请刷新确认服务器状态')
      throw err
    } finally {
      clearTimeout(timer)
      if (version === sessionVersion) invalidateDashboardFetch()
    }
  }

  function updateConfig(key, value, password) {
    const snakeCaseKey = key.replace(/[A-Z]/g, (l) => `_${l.toLowerCase()}`)
    return postConfig('/api/update-config', { key: snakeCaseKey, value }, password)
  }

  return {
    // state
    status,
    config,
    apiKeyStats,
    logs,
    timeSeriesData,
    availableModels,
    selectedModel,
    isRefreshing,
    isConfigLoaded,
    isDarkMode,
    isUnlocked,
    sessionPassword,
    lastError,
    lastUpdated,
    // actions
    setSessionPassword,
    fetchDashboardData,
    setSelectedModel,
    toggleDarkMode,
    updateConfig,
    postConfig,
    captureSession,
    authHeaders,
  }
})
