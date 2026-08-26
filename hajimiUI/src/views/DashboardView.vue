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
    pwError.value = 'Please enter a password'
    return
  }
  pwLoading.value = true
  pwError.value = ''
  dashboardStore.setSessionPassword(pw.value)
  try {
    await dashboardStore.fetchDashboardData()
    if (dashboardStore.lastError === 'AUTH_FAILED' || dashboardStore.lastError === 'PASSWORD_REQUIRED') {
      pwError.value = 'Authentication failed'
      dashboardStore.setSessionPassword('')
    } else {
      startPolling()
    }
  } catch (e) {
    pwError.value = e.message || 'Authentication failed'
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
        <span class="app-header__brand-icon">G</span>
        <span class="app-header__brand-text">Gateway Console</span>
      </div>

      <div class="app-header__spacer" />

      <div class="app-header__actions">
        <span v-if="isUnlocked" class="pill pill--success hidden-sm">
          <span class="pill__dot pill__dot--pulse" />
          Online
        </span>
        <button
          v-if="isUnlocked"
          class="btn btn--secondary btn--sm"
          @click="toggleVertex"
          :title="enableVertex ? 'Disable Vertex mode' : 'Enable Vertex mode'"
        >
          Vertex
          <span
            class="pill"
            :class="enableVertex ? 'pill--accent' : ''"
            style="height:18px;padding:0 6px;font-size:10px;"
          >
            {{ enableVertex ? 'ON' : 'OFF' }}
          </span>
        </button>
        <button
          class="btn btn--ghost btn--sm btn--icon"
          @click="toggleDarkMode"
          :title="isDarkMode ? 'Switch to light' : 'Switch to dark'"
        >
          <span v-if="isDarkMode">☀</span>
          <span v-else>☾</span>
        </button>
        <button
          v-if="isUnlocked"
          class="btn btn--ghost btn--sm btn--icon"
          @click="handleRefresh"
          title="Refresh"
        >
          ↻
        </button>
        <button
          v-if="isUnlocked"
          class="btn btn--ghost btn--sm"
          @click="lock"
          title="Lock session"
        >
          Lock
        </button>
      </div>
    </header>

    <!-- ============ Main ============ -->
    <main class="app-main">
      <!-- Password gate -->
      <div v-if="!isUnlocked" class="lock-card-wrap">
        <div class="card" style="max-width:400px;width:100%;">
          <div class="card__header">
            <div>
              <div class="card__title">Authentication required</div>
              <div class="card__subtitle">
                Enter the operator password to access the console.
              </div>
            </div>
          </div>
          <div class="card__body">
            <form @submit.prevent="unlock" class="col" style="gap:12px;">
              <div class="field">
                <label class="field__label">Password</label>
                <input
                  v-model="pw"
                  type="password"
                  class="input"
                  placeholder="Enter password"
                  autofocus
                  autocomplete="current-password"
                >
                <div v-if="pwError" class="field__hint" style="color:var(--danger-strong);">
                  {{ pwError }}
                </div>
              </div>
              <button type="submit" class="btn btn--primary btn--lg" :disabled="pwLoading">
                {{ pwLoading ? 'Verifying…' : 'Unlock' }}
              </button>
            </form>
          </div>
        </div>
      </div>

      <!-- Console -->
      <template v-else>
        <div v-if="lastError === 'AUTH_FAILED'" class="banner banner--warning">
          <strong>Session expired.</strong>
          The saved password is no longer valid. Please lock and unlock again.
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
  padding: var(--sp-6) 0;
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
