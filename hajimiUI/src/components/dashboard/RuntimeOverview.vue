<script setup>
import { computed } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import { exact, dateTime } from '../../services/format'
const store = useDashboardStore()
const data = computed(() => store.monitor)
const metrics = computed(() => [
  ['运行中请求', data.value.active_count, '尚未结束的客户端请求'],
  ['等待上游', data.value.waiting_count, '已调用上游，等待有效输出'],
  ['正在输出', data.value.streaming_count, '已开始返回有效内容'],
  ['重试等待', data.value.retrying_count, '等待退避或下一次尝试'],
  ['上游在途', data.value.upstream_active, '并发与重试产生的实际调用'],
  ['合并等待', data.value.coalesced_count, '正在复用其他请求的结果'],
])
</script>
<template>
  <section class="section runtime-overview" aria-label="实时运行状态">
    <div class="section__header"><div><h2 class="section__title">实时运行</h2><p class="section-description">{{ data.scope || '当前进程' }} · 每 2 秒同步 · {{ data.instance_id || '等待同步' }}</p></div><span class="pill" :class="store.liveError ? 'pill--warning' : 'pill--success'">{{ store.liveError ? '同步异常' : '实时监控' }}</span></div>
    <div v-if="store.liveError" role="alert" class="banner">{{ store.liveError }}，当前保留上次结果。<button class="btn btn--ghost btn--sm" @click="store.fetchLiveData">重试</button></div>
    <div class="runtime-grid"><div v-for="[label, value, hint] in metrics" :key="label" class="card runtime-metric" :title="hint"><span>{{ label }}</span><strong>{{ value == null ? '—' : exact(value) }}</strong><small>{{ hint }}</small></div></div>
    <div class="runtime-totals card"><span>本周期收到 <b>{{ exact(data.counts?.received || 0) }}</b></span><span>成功 <b>{{ exact(data.counts?.['成功'] || 0) }}</b></span><span>失败 <b>{{ exact(data.counts?.['失败'] || 0) }}</b></span><span>中断 <b>{{ exact(data.counts?.['中断'] || 0) }}</b></span><span>缓存命中 <b>{{ exact(data.counts?.cache_hits || 0) }}</b></span><span>已知结果 Token <b>{{ exact(data.tokens?.total_tokens || 0) }}</b></span><small>密钥池 {{ exact(store.status.keyCount) }} · Vertex 凭证 {{ exact(store.status.credentialsCount || 0) }} · 缓存条目 {{ exact(store.status.cacheEntries || 0) }}</small><small>周期开始 {{ dateTime(data.period_started_at) }} · Token 仅汇总上游返回结果，未知用量与缓存不计入；输出包含思考。</small></div>
  </section>
</template>
<style scoped>
.runtime-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.runtime-metric{padding:18px;display:flex;flex-direction:column;gap:8px}.runtime-metric span{font-size:12px;color:var(--text-muted)}.runtime-metric strong{font-size:30px;font-variant-numeric:tabular-nums}.runtime-metric small{color:var(--text-subtle);font-size:11px}.runtime-totals{display:flex;flex-wrap:wrap;gap:14px;padding:16px;margin-top:12px;font-size:12px}.runtime-totals small{flex-basis:100%;color:var(--text-muted)}@media(max-width:640px){.runtime-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.runtime-metric{padding:14px}.runtime-metric strong{font-size:26px}}
</style>
