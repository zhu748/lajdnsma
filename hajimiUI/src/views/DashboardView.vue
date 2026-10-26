<script setup>
import { ref, onMounted, onUnmounted, computed, watch } from 'vue'
import StatusSection from '../components/dashboard/StatusSection.vue'
import ConfigSection from '../components/dashboard/ConfigSection.vue'
import ProtocolEndpoints from '../components/dashboard/ProtocolEndpoints.vue'
import LogSection from '../components/dashboard/LogSection.vue'
import { useDashboardStore } from '../stores/dashboard'

const dashboardStore = useDashboardStore()
const refreshTimer = ref(null)
const pw = ref('')
const pwError = ref('')
const pwLoading = ref(false)
const modeError = ref('')
const modeSaving = ref(false)
const navigation = [
  { id: 'overview', number: '01', label: '运行概览' },
  { id: 'connections', number: '02', label: '协议接入' },
  { id: 'configuration', number: '03', label: '服务配置' },
  { id: 'activity', number: '04', label: '请求日志' },
]

const isUnlocked = computed(() => dashboardStore.isUnlocked)
const isDarkMode = computed(() => dashboardStore.isDarkMode)
const enableVertex = computed(() => dashboardStore.config.enableVertex)
const lastError = computed(() => dashboardStore.lastError)
const isRefreshing = computed(() => dashboardStore.isRefreshing)

onMounted(() => {
  if (dashboardStore.sessionPassword) dashboardStore.fetchDashboardData()
  // Perf(round4): 标签页隐藏时暂停 5s 轮询（后台标签里的仪表盘刷新
  // 没有意义，只会持续消耗服务端 /api/dashboard-data 与日志聚合开销；
  // 回到前台时立即拉一次最新数据再恢复轮询。
  document.addEventListener('visibilitychange', handleVisibilityChange)
})

onUnmounted(() => {
  stopPolling()
  document.removeEventListener('visibilitychange', handleVisibilityChange)
})

function handleVisibilityChange() {
  if (document.hidden) {
    stopPolling()
  } else if (isUnlocked.value) {
    startPolling() // startPolling 内部有防重入 + 立即拉一次
  }
}

// 会话失效（401 时 store 清空密码回到锁屏）也要停掉轮询——
// 此前只有手动 Lock / 卸载会停，401 后定时器继续空转打 401。
watch(isUnlocked, (unlocked) => {
  if (!unlocked) stopPolling()
  else startPolling()
})

function startPolling() {
  if (refreshTimer.value || document.hidden || !isUnlocked.value) return
  refreshTimer.value = setInterval(() => {
    dashboardStore.fetchDashboardData()
  }, 5000) // 5s — was 1s, but with auth in the loop the lower frequency is enough
  dashboardStore.fetchDashboardData()
}

function stopPolling() {
  if (refreshTimer.value) {
    clearInterval(refreshTimer.value)
    refreshTimer.value = null
  }
}

async function unlock() {
  if (!pw.value) {
    pwError.value = '请输入密码'
    return
  }
  pwLoading.value = true
  pwError.value = ''
  dashboardStore.setSessionPassword(pw.value)
  try {
    await dashboardStore.fetchDashboardData()
    if (dashboardStore.lastError) {
      pwError.value = dashboardStore.lastError === 'AUTH_FAILED' ? '密码验证失败' : dashboardStore.lastError
      dashboardStore.setSessionPassword('')
    } else {
      startPolling()
    }
  } catch (e) {
    pwError.value = e.message || '密码验证失败'
    dashboardStore.setSessionPassword('')
  } finally {
    pwLoading.value = false
  }
}

function lock() {
  stopPolling()
  dashboardStore.setSessionPassword('')
  pw.value = ''
}

function handleRefresh() {
  dashboardStore.fetchDashboardData()
}

function toggleDarkMode() {
  dashboardStore.toggleDarkMode()
}

async function toggleVertex() {
  if (modeSaving.value) return
  modeSaving.value = true
  modeError.value = ''
  try {
    const newValue = !enableVertex.value
    await dashboardStore.updateConfig('enableVertex', newValue)
    // The next poll will pick up the new value.
    await dashboardStore.fetchDashboardData()
  } catch (e) {
    modeError.value = e.message || '切换 Vertex 模式失败'
  } finally {
    modeSaving.value = false
  }
}

function formatTime(t) {
  if (!t) return ''
  try {
    return new Date(t).toLocaleString()
  } catch {
    return t
  }
}
</script>

<template>
  <div class="app-shell">
    <!-- ============ Header ============ -->
    <header class="app-header">
      <div class="app-header__brand">
        <span class="app-header__brand-icon">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="4 17 10 11 4 5" />
            <line x1="12" y1="19" x2="20" y2="19" />
          </svg>
        </span>
        <span class="app-header__brand-text">Hajimi <span class="brand-divider">/</span> <span class="brand-caption">网关控制台</span></span>
      </div>

      <div class="app-header__spacer" />

      <div class="app-header__actions">
        <span v-if="isUnlocked" class="pill hidden-sm" :class="lastError ? 'pill--warning' : 'pill--success'">
          <span class="pill__dot pill__dot--pulse" />
          {{ lastError ? '连接异常' : '在线' }}
        </span>
        <button
          v-if="isUnlocked"
          class="btn btn--secondary btn--sm"
          @click="toggleVertex"
          :disabled="modeSaving"
          :title="enableVertex ? '关闭 Vertex 模式' : '开启 Vertex 模式'"
        >
          Vertex
          <span
            class="pill"
            :class="enableVertex ? 'pill--accent' : ''"
            style="height:18px;padding:0 6px;font-size:10px;"
          >
            {{ enableVertex ? '开' : '关' }}
          </span>
        </button>
        <button
          class="btn btn--ghost btn--sm btn--icon"
          @click="toggleDarkMode"
          :aria-label="isDarkMode ? '切换浅色模式' : '切换深色模式'"
          :title="isDarkMode ? '切换浅色模式' : '切换深色模式'"
        >
          <span v-if="isDarkMode">☀</span>
          <span v-else>☾</span>
        </button>
        <button
          v-if="isUnlocked"
          class="btn btn--ghost btn--sm btn--icon"
          :class="{ 'btn--spinning': isRefreshing }"
          @click="handleRefresh"
          title="刷新数据"
          aria-label="刷新数据"
          :disabled="isRefreshing"
        >
          <span class="refresh-icon">↻</span>
        </button>
        <button
          v-if="isUnlocked"
          class="btn btn--ghost btn--sm"
          @click="lock"
          title="锁定会话"
        >
          锁定
        </button>
      </div>
    </header>

    <!-- ============ Main ============ -->
    <main class="app-main" :class="{ 'app-main--console': isUnlocked }">
      <!-- Password gate -->
      <div v-if="!isUnlocked" class="lock-card-wrap">
        <div class="card lock-card">
          <div class="lock-card__header">
            <div class="lock-card__icon">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
                <path d="M7 11V7a5 5 0 0 1 10 0v4" />
              </svg>
            </div>
            <div class="lock-card__eyebrow">WELCOME TO HAJIMI</div>
            <h1 class="lock-card__title">欢迎回到控制台</h1>
            <div class="lock-card__subtitle">输入管理密码以访问控制台</div>
          </div>
          <div class="lock-card__body">
            <form @submit.prevent="unlock" class="col" style="gap:14px;">
              <div class="field">
                <label class="field__label" for="lock-pw">密码</label>
                <input
                  id="lock-pw"
                  v-model="pw"
                  type="password"
                  class="input"
                  placeholder="请输入密码"
                  autofocus
                  autocomplete="current-password"
                >
                <div v-if="pwError" role="alert" class="field__hint" style="color:var(--danger-strong);">
                  {{ pwError }}
                </div>
              </div>
              <button type="submit" class="btn btn--primary btn--lg" :disabled="pwLoading">
                {{ pwLoading ? '验证中…' : '解锁' }}
              </button>
            </form>
          </div>
        </div>
      </div>

      <!-- Console -->
      <template v-else>
        <div v-if="modeError" role="alert" class="banner banner--warning">{{ modeError }}</div>
        <div class="console-layout">
          <aside class="console-sidebar">
            <div class="sidebar-caption">工作空间</div>
            <nav aria-label="控制台导航" class="console-nav">
              <a v-for="item in navigation" :key="item.id" :href="`#${item.id}`">
                <span class="nav-number mono">{{ item.number }}</span>{{ item.label }}<span class="nav-arrow" aria-hidden="true">↗</span>
              </a>
            </nav>
            <div class="sidebar-note"><span class="pill__dot" :class="lastError ? 'connection-warning' : 'connection-ok'" /><span>{{ lastError ? '连接异常' : '每 5 秒同步状态' }}</span></div>
          </aside>
          <div class="console-content">
            <div class="console-heading">
              <div><p class="console-eyebrow">HAJIMI / WORKSPACE</p><h1>服务总览</h1><p class="console-description">从一次请求到每一枚令牌，让服务运行一目了然。</p></div>
              <div class="backend-indicator"><span class="backend-indicator__icon" aria-hidden="true">↗</span><div><span>当前后端</span><strong>{{ enableVertex ? 'Vertex AI' : 'AI Studio' }}</strong></div></div>
            </div>
            <div v-if="lastError" class="banner banner--warning" role="alert">数据暂时无法更新：{{ lastError }}。当前显示上次成功同步的结果。<button class="btn btn--ghost btn--sm" @click="handleRefresh">重试</button></div>
            <div id="overview" class="console-section"><StatusSection /></div>
            <div id="connections" class="console-section"><ProtocolEndpoints /></div>
            <div id="configuration" class="console-section"><ConfigSection /></div>
            <div id="activity" class="console-section"><LogSection /></div>
            <footer class="console-footer"><span>Hajimi Gateway</span><span>{{ dashboardStore.lastUpdated ? `最后同步 ${formatTime(dashboardStore.lastUpdated)}` : '等待数据同步' }}</span></footer>
          </div>
        </div>
      </template>
    </main>
  </div>
</template>

<style scoped>
.lock-card-wrap {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: calc(100vh - var(--header-h) - 48px);
  min-height: calc(100dvh - var(--header-h) - 48px);
  padding: var(--sp-6) 0;
}

/* 锁屏卡片：居中布局 + 顶部品牌化图标，比原来的左侧标题卡片更
   有「登录页」的仪式感 */
.lock-card {
  max-width: 400px;
  width: 100%;
  box-shadow: var(--shadow-lg);
}
.lock-card__header {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: var(--sp-8) var(--sp-6) var(--sp-4);
}
.lock-card__icon {
  width: 48px;
  height: 48px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--r-full);
  background: var(--accent-subtle);
  color: var(--accent-strong);
  margin-bottom: var(--sp-3);
}
.lock-card__title {
  font-size: var(--fs-xl);
  font-weight: var(--fw-semibold);
  color: var(--text-strong);
}
.lock-card__subtitle {
  font-size: var(--fs-sm);
  color: var(--text-muted);
  margin-top: var(--sp-1);
}
.lock-card__body {
  padding: 0 var(--sp-6) var(--sp-6);
}

.banner {
  padding: var(--sp-3) var(--sp-4);
  border-radius: var(--r-md);
  font-size: var(--fs-sm);
  margin-bottom: var(--sp-4);
  border: 1px solid var(--border);
  background: var(--bg-elevated);
}

.banner--warning {
  background: var(--warning-subtle);
  border-color: var(--warning);
  color: var(--warning-strong);
}

/* 刷新按钮：拉取数据时图标旋转，给用户「正在刷新」的即时反馈 */
.btn--spinning .refresh-icon {
  display: inline-block;
  animation: refresh-spin 0.9s linear infinite;
}
@keyframes refresh-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
</style>

<style scoped>
.brand-divider { color: var(--border-strong); margin: 0 10px; font-weight: 400; }
.brand-caption { color: var(--text-muted); font-size: 12px; font-weight: 400; }
.console-layout { display: grid; grid-template-columns: 176px minmax(0, 1fr); gap: 40px; }
.console-sidebar { position: sticky; top: 96px; align-self: start; padding-top: 12px; }
.sidebar-caption { font-size: 11px; letter-spacing: .1em; color: var(--text-subtle); padding: 0 12px; margin-bottom: 16px; }
.console-nav { display: flex; flex-direction: column; gap: 7px; }
.console-nav a { display: flex; align-items: center; gap: 12px; color: var(--text-muted); padding: 12px; border-radius: 9px; font-size: 13px; text-decoration: none; transition: background .15s, color .15s; }
.console-nav a:hover, .console-nav a:focus-visible { background: var(--accent-subtle); color: var(--accent-strong); }
.nav-number { font-size: 10px; opacity: .6; }
.nav-arrow { margin-left: auto; opacity: .5; }
.sidebar-note { margin: 36px 12px 0; padding-top: 20px; border-top: 1px solid var(--border); display: flex; gap: 8px; align-items: center; font-size: 11px; color: var(--text-muted); }
.connection-ok { background: var(--success); }
.connection-warning { background: var(--warning); }
.console-content { min-width: 0; }
.console-heading { display: flex; align-items: center; justify-content: space-between; gap: 24px; padding-bottom: 32px; }
.console-eyebrow { font: 10px var(--font-mono); letter-spacing: .16em; color: var(--accent-strong); margin: 0 0 14px; }
.console-heading h1 { font-size: clamp(24px, 3vw, 32px); letter-spacing: -.04em; font-weight: 600; line-height: 1.2; }
.console-description { font-size: 12px; color: var(--text-muted); margin: 12px 0 0; }
.backend-indicator { display: flex; gap: 12px; align-items: center; flex-shrink: 0; padding: 14px 18px; border: 1px solid var(--border); border-radius: 12px; background: var(--bg-elevated); }
.backend-indicator__icon { display: grid; place-items: center; width: 32px; height: 32px; border-radius: 50%; color: var(--accent-strong); background: var(--accent-subtle); font-size: 19px; }
.backend-indicator span:not(.backend-indicator__icon) { display: block; color: var(--text-muted); font-size: 10px; margin-bottom: 4px; }
.backend-indicator strong { font-size: 13px; font-weight: 600; }
.console-section { scroll-margin-top: 90px; }
.console-footer { display: flex; gap: 16px; justify-content: space-between; padding-top: 24px; border-top: 1px solid var(--border); color: var(--text-subtle); font-size: 11px; }
.lock-card__eyebrow { margin: 4px 0 12px; font: 10px var(--font-mono); letter-spacing: .14em; color: var(--accent-strong); }
@media (max-width: 1050px) {
 .console-layout { grid-template-columns: 1fr; gap: 24px; }
 .console-sidebar { position: static; padding: 0; }
 .console-nav { flex-direction: row; gap: 4px; overflow-x: auto; padding-bottom: 3px; }
 .console-nav a { white-space: nowrap; flex: 1; justify-content: center; padding: 10px; border: 1px solid var(--border); background: var(--bg-elevated); }
 .sidebar-caption, .sidebar-note, .nav-arrow { display: none; }
}
@media (max-width: 600px) {
 .console-heading { align-items: start; gap: 12px; padding-bottom: 24px; }
 .console-description { max-width: 220px; line-height: 1.7; }
 .backend-indicator { padding: 10px; gap: 8px; }
 .backend-indicator__icon { display: none; }
 .console-nav a { padding: 9px 8px; font-size: 12px; }
 .nav-number { display: none; }
 .console-footer { flex-direction: column; gap: 6px; }
 .brand-caption, .brand-divider { display: none; }
}
</style>
