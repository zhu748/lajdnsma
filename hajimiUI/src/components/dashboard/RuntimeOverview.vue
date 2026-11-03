<script setup>
import { computed } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import { exact, dateTime } from '../../services/format'
const store = useDashboardStore()
const data = computed(() => store.monitor)
const metrics = computed(() => [
  { label: '运行中请求', value: data.value.active_count, hint: '尚未结束的客户端请求', tone: 'accent', icon: 'pulse' },
  { label: '等待上游', value: data.value.waiting_count, hint: '已调用上游，等待有效输出', tone: 'info', icon: 'hourglass' },
  { label: '正在输出', value: data.value.streaming_count, hint: '已开始返回有效内容', tone: 'success', icon: 'stream' },
  { label: '重试等待', value: data.value.retrying_count, hint: '等待退避或下一次尝试', tone: 'warning', icon: 'retry' },
  { label: '上游在途', value: data.value.upstream_active, hint: '并发与重试产生的实际调用', tone: 'info', icon: 'upstream' },
  { label: '合并等待', value: data.value.coalesced_count, hint: '正在复用其他请求的结果', tone: 'muted', icon: 'merge' },
])
</script>
<template>
  <section class="section runtime-overview" aria-label="实时运行状态">
    <div class="section__header"><div><h2 class="section__title">实时运行</h2><p class="section-description">{{ data.scope || '当前进程' }} · 每 2 秒同步 · {{ data.instance_id || '等待同步' }}</p></div><span class="pill" :class="store.liveError ? 'pill--warning' : 'pill--success'">{{ store.liveError ? '同步异常' : '实时监控' }}</span></div>
    <div v-if="store.liveError" role="alert" class="banner">{{ store.liveError }}，当前保留上次结果。<button class="btn btn--ghost btn--sm" @click="store.fetchLiveData">重试</button></div>
    <div class="runtime-grid">
      <div v-for="metric in metrics" :key="metric.label" class="card runtime-metric" :class="`runtime-metric--${metric.tone}`" :title="metric.hint">
        <span class="runtime-metric__accent" aria-hidden="true" />
        <div class="runtime-metric__head">
          <span class="runtime-metric__icon" :class="`runtime-metric__icon--${metric.tone}`" aria-hidden="true">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <template v-if="metric.icon === 'pulse'"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12" /></template>
              <template v-else-if="metric.icon === 'hourglass'"><path d="M5 22h14M5 2h14M17 22v-4.172a2 2 0 0 0-.586-1.414L12 12l-4.414 4.414A2 2 0 0 0 7 17.828V22M7 2v4.172a2 2 0 0 0 .586 1.414L12 12l4.414-4.414A2 2 0 0 0 17 6.172V2" /></template>
              <template v-else-if="metric.icon === 'stream'"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /></template>
              <template v-else-if="metric.icon === 'retry'"><polyline points="23 4 23 10 17 10" /><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" /></template>
              <template v-else-if="metric.icon === 'upstream'"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" /><polyline points="15 3 21 3 21 9" /><line x1="10" y1="14" x2="21" y2="3" /></template>
              <template v-else><path d="M18 8h1a4 4 0 0 1 0 8h-1M2 8h16v9a4 4 0 0 1-4 4H6a4 4 0 0 1-4-4V8z" /><line x1="6" y1="1" x2="6" y2="4" /><line x1="10" y1="1" x2="10" y2="4" /><line x1="14" y1="1" x2="14" y2="4" /></template>
            </svg>
          </span>
          <span class="runtime-metric__label">{{ metric.label }}</span>
        </div>
        <strong class="runtime-metric__value">{{ metric.value == null ? '—' : exact(metric.value) }}</strong>
        <small class="runtime-metric__hint">{{ metric.hint }}</small>
      </div>
    </div>
    <div class="runtime-totals card"><span>本周期收到 <b>{{ exact(data.counts?.received || 0) }}</b></span><span class="runtime-totals__sep" aria-hidden="true" /><span>成功 <b>{{ exact(data.counts?.['成功'] || 0) }}</b></span><span>失败 <b>{{ exact(data.counts?.['失败'] || 0) }}</b></span><span>中断 <b>{{ exact(data.counts?.['中断'] || 0) }}</b></span><span>缓存命中 <b>{{ exact(data.counts?.cache_hits || 0) }}</b></span><span>已知结果 Token <b>{{ exact(data.tokens?.total_tokens || 0) }}</b></span><small>密钥池 {{ exact(store.status.keyCount) }} · Vertex 凭证 {{ exact(store.status.credentialsCount || 0) }} · 缓存条目 {{ exact(store.status.cacheEntries || 0) }}</small><small>周期开始 {{ dateTime(data.period_started_at) }} · Token 仅汇总上游返回结果，未知用量与缓存不计入；输出包含思考。</small></div>
  </section>
</template>
<style scoped>
.runtime-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.runtime-metric {
  position: relative;
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow: hidden;
  transition: transform var(--dur-base) var(--ease-out), box-shadow var(--dur-base) var(--ease-out), border-color var(--dur-base) var(--ease-out);
}
/* 悬停微抬升：顶部概览卡片是面板第一眼，轻交互提升「活」感 */
.runtime-metric:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-md);
  border-color: var(--border-strong);
}
.runtime-metric__accent {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 2px;
  opacity: 0.4;
  transition: opacity var(--dur-base) var(--ease-out);
}
.runtime-metric:hover .runtime-metric__accent { opacity: 1; }
.runtime-metric__accent { background: var(--accent); }
.runtime-metric--info .runtime-metric__accent { background: var(--info); }
.runtime-metric--success .runtime-metric__accent { background: var(--success); }
.runtime-metric--warning .runtime-metric__accent { background: var(--warning); }
.runtime-metric--muted .runtime-metric__accent { background: var(--border-strong); }

.runtime-metric__head { display: flex; align-items: center; gap: 8px; }
.runtime-metric__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border-radius: var(--r-md);
  flex-shrink: 0;
  background: var(--accent-subtle);
  color: var(--accent-strong);
}
.runtime-metric--info .runtime-metric__icon { background: var(--info-subtle); color: var(--info-strong); }
.runtime-metric--success .runtime-metric__icon { background: var(--success-subtle); color: var(--success-strong); }
.runtime-metric--warning .runtime-metric__icon { background: var(--warning-subtle); color: var(--warning-strong); }
.runtime-metric--muted .runtime-metric__icon { background: var(--bg-subtle); color: var(--text-muted); }

.runtime-metric__label { font-size: 12px; color: var(--text-muted); }
.runtime-metric__value {
  font-size: 30px;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
  line-height: 1.1;
}
.runtime-metric__hint { color: var(--text-subtle); font-size: 11px; }

.runtime-totals {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 10px 14px;
  padding: 16px;
  margin-top: 12px;
  font-size: 12px;
}
.runtime-totals b { font-variant-numeric: tabular-nums; font-weight: var(--fw-semibold); }
.runtime-totals__sep { flex: 1 1 auto; }
.runtime-totals small { flex-basis: 100%; color: var(--text-muted); }
@media (max-width: 640px) {
  .runtime-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .runtime-metric { padding: 14px; }
  .runtime-metric__value { font-size: 26px; }
  .runtime-metric__hint { display: none; }
}
</style>
