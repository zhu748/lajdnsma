<script setup>
import { ref, onMounted, onUnmounted, computed, watch } from 'vue'
import StatusSection from '../components/dashboard/StatusSection.vue'
import ConfigSection from '../components/dashboard/ConfigSection.vue'
import LogSection from '../components/dashboard/LogSection.vue'
import { useDashboardStore } from '../stores/dashboard'

const dashboardStore = useDashboardStore()
const refreshTimer = ref(null)
const pw = ref('')
const pwError = ref('')
const pwLoading = ref(false)

const isUnlocked = computed(() => dashboardStore.isUnlocked)
const isDarkMode = computed(() => dashboardStore.isDarkMode)
const enableVertex = computed(() => dashboardStore.config.enableVertex)
const lastError = computed(() => dashboardStore.lastError)

onMounted(() => {
  if (isUnlocked.value) startPolling()
})

onUnmounted(() => stopPolling())

// 会话失效（401 时 store 清空密码回到锁屏）也要停掉轮询——
// 此前只有手动 Lock / 卸载会停，401 后定时器继续空转打 401。
watch(isUnlocked, (unlocked) => {
  if (!unlocked) stopPolling()
})

function startPolling() {
  if (refreshTimer.value) return
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
    if (dashboardStore.lastError === 'AUTH_FAILED' || dashboardStore.lastError === 'PASSWORD_REQUIRED') {
      pwError.value = '密码验证失败'
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
  // Vertex toggle uses the session password.
  try {
    const newValue = !enableVertex.value
    await dashboardStore.updateConfig('enableVertex', newValue)
    // The next poll will pick up the new value.
    await dashboardStore.fetchDashboardData()
  } catch (e) {
    console.error('toggle Vertex:', e)
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
        <span class="app-header__brand-text">网关控制台</span>
      </div>

      <div class="app-header__spacer" />

      <div class="app-header__actions">
        <span v-if="isUnlocked" class="pill pill--success hidden-sm">
          <span class="pill__dot pill__dot--pulse" />
          在线
        </span>
        <button
          v-if="isUnlocked"
          class="btn btn--secondary btn--sm"
          @click="toggleVertex"
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
          :title="isDarkMode ? '切换浅色模式' : '切换深色模式'"
        >
          <span v-if="isDarkMode">☀</span>
          <span v-else>☾</span>
        </button>
        <button
          v-if="isUnlocked"
          class="btn btn--ghost btn--sm btn--icon"
          @click="handleRefresh"
          title="刷新数据"
        >
          ↻
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
    <main class="app-main">
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
            <div class="lock-card__title">需要身份验证</div>
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
                <div v-if="pwError" class="field__hint" style="color:var(--danger-strong);">
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
        <div v-if="lastError === 'AUTH_FAILED'" class="banner banner--warning">
          <strong>会话已过期。</strong>
          保存的密码已失效，请锁定后重新解锁。
        </div>

        <StatusSection />
        <ConfigSection />
        <LogSection />
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
</style>
