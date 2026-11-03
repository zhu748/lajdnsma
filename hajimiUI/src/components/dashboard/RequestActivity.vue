<script setup>
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
import { exact, duration, dateTime } from '../../services/format'
const store = useDashboardStore()
const query = ref(''), outcome = ref('all'), protocol = ref('all'), backend = ref('all'), timeRange = ref('all'), slow = ref(false), expanded = ref(null), page = ref(1)
const tick = ref(Date.now())
let timer
onMounted(() => timer = setInterval(() => tick.value = Date.now(), 1000))
onUnmounted(() => clearInterval(timer))
const elapsed = r => r.ended_at ? r.duration_ms : Math.max(r.duration_ms, tick.value - Date.parse(r.received_at))
const protocolName = r => r.path.includes('/responses') ? 'Responses' : r.path.includes('/messages') ? 'Claude' : r.path.includes('/models/') ? 'Gemini' : r.path.includes('embedding') || r.path.includes('/vector/') ? 'Embedding' : 'Chat'
const filtered = computed(() => (store.monitor.requests || []).filter(r =>
  (!query.value || [r.request_id, r.model, r.requested_model, r.path].join(' ').toLowerCase().includes(query.value.toLowerCase())) &&
  (outcome.value === 'all' || (outcome.value === 'active' ? !r.ended_at : r.outcome === outcome.value)) &&
  (protocol.value === 'all' || protocolName(r) === protocol.value) && (backend.value === 'all' || r.backend === backend.value) &&
  (timeRange.value === 'all' || tick.value - Date.parse(r.received_at) < Number(timeRange.value) * 60000) && (!slow.value || elapsed(r) >= 30000)))
const rows = computed(() => filtered.value.slice((page.value - 1) * 20, page.value * 20))
const pages = computed(() => Math.max(1, Math.ceil(filtered.value.length / 20)))
import { watch } from 'vue'
watch([query, outcome, protocol, backend, timeRange, slow], () => page.value = 1)
watch(pages, n => page.value = Math.min(page.value, n))
function download() {
  const blob = new Blob([JSON.stringify(filtered.value, null, 2)], {type:'application/json'})
  const url = URL.createObjectURL(blob), a = document.createElement('a')
  a.href = url; a.download = 'gateway-requests.json'; a.click(); URL.revokeObjectURL(url)
}
</script>
<template>
<section class="section" aria-label="请求记录">
  <div class="section__header"><div><h2 class="section__title">请求记录 <span class="text-muted">{{ filtered.length }}</span></h2><p class="section-description">保留当前进程最近 {{ store.monitor.history_capacity || 300 }} 条结束记录和全部运行中请求；每日 15:00 随统计重置整体清空。</p></div><button class="btn btn--secondary btn--sm" @click="download" :disabled="!filtered.length">导出筛选结果</button></div>
  <div class="request-toolbar"><div class="request-toolbar__group"><input class="input request-toolbar__search" v-model="query" placeholder="搜索请求编号、模型或路径" aria-label="搜索请求"></div><div class="request-toolbar__group"><select class="input request-toolbar__select" v-model="outcome" aria-label="请求状态"><option value="all">全部状态</option><option value="active">运行中</option><option>成功</option><option>失败</option><option>中断</option></select><select class="input request-toolbar__select" v-model="protocol" aria-label="请求协议"><option value="all">全部协议</option><option v-for="p in ['Chat','Responses','Claude','Gemini','Embedding']" :key="p">{{ p }}</option></select><select class="input request-toolbar__select" v-model="backend" aria-label="请求后端"><option value="all">全部后端</option><option>AI Studio</option><option>Vertex AI</option></select><select class="input request-toolbar__select" v-model="timeRange" aria-label="请求时间范围"><option value="all">保留的全部时间</option><option value="5">最近 5 分钟</option><option value="30">最近 30 分钟</option><option value="60">最近 1 小时</option></select><button class="filter-chip" :class="{ 'filter-chip--active': slow }" :aria-pressed="slow" @click="slow = !slow" title="只看耗时不少于 30 秒的请求">耗时 ≥ 30 秒</button></div></div>
  <p v-if="store.monitor.gap" role="status" class="text-muted">同步期间部分历史已被淘汰，请结合服务端日志排查。</p>
  <div class="card request-table" role="region" aria-label="请求列表，可左右滚动" tabindex="0"><table class="table"><thead><tr><th>请求 / 接收时间</th><th>模型 / 后端</th><th>阶段</th><th class="col-num">耗时</th><th class="col-num">上游首输出 / 首正文</th><th class="col-num">结果 Token</th><th class="col-num">尝试</th></tr></thead><tbody><template v-for="r in rows" :key="r.request_id"><tr :class="{'request-failed': r.outcome === '失败'}"><td><button class="request-id" @click="expanded = expanded === r.request_id ? null : r.request_id" :aria-expanded="expanded === r.request_id">{{ r.request_id }} {{ expanded === r.request_id ? '▴' : '▾' }}</button><small>{{ dateTime(r.received_at) }} · {{ protocolName(r) }}</small></td><td>{{ r.model || r.requested_model || '待识别' }}<small>{{ r.backend }} · {{ r.source }} · {{ r.request_type === 'fake-stream' ? '伪流式' : r.request_type === 'stream' ? '流式' : '非流式' }}</small></td><td><span class="pill" :class="r.outcome === '失败' ? 'pill--warning' : !r.ended_at ? 'pill--accent' : ''">{{ r.stage }}</span></td><td class="col-num mono">{{ duration(elapsed(r)) }}</td><td class="col-num mono">{{ duration(r.upstream_first_output_ms) }} / {{ duration(r.upstream_first_text_ms) }}</td><td class="col-num mono" :title="exact(r.usage?.total_tokens)">{{ exact(r.usage?.total_tokens) }}<small>{{ r.usage_source }}</small></td><td class="col-num mono">{{ r.attempt_count }}</td></tr>
  <tr v-if="expanded === r.request_id"><td colspan="7"><div class="request-detail"><p class="mono">{{ r.path }} · {{ r.requested_model || '原生模型路径' }} → {{ r.model }} · HTTP {{ r.status_code || '等待响应' }} · {{ r.finish_reason || '等待结束原因' }}</p><div class="usage-grid"><span>输入 <b>{{ exact(r.usage?.input_tokens) }}</b></span><span>输出（含思考） <b>{{ exact(r.usage?.output_tokens) }}</b></span><span>思考 <b>{{ exact(r.usage?.reasoning_tokens) }}</b></span><span>合计 <b>{{ exact(r.usage?.total_tokens) }}</b></span></div><p>网关首输出 {{ duration(r.first_output_ms) }} · 网关首正文 {{ duration(r.first_text_ms) }}。非流式上游无法观测首字；空分片和心跳不算有效输出。</p><p>所有已记录尝试的已知上游 Token：{{ exact(r.upstream_usage?.total_tokens) }}；{{ r.unknown_usage_attempts }} 次用量未知。缓存与复用不会产生本请求的新上游用量。{{ r.attempts_truncated ? '尝试明细仅保留最近记录。' : '' }}</p><ol class="timeline"><li v-for="(event, i) in r.timeline" :key="i"><span>{{ duration(event.elapsed_ms) }}</span> {{ event.label }} <small>{{ event.attempt_id }} {{ event.outcome }}<template v-if="event.status_code"> · HTTP {{ event.status_code }}</template><template v-if="event.retry_delay != null"> · 计划等待 {{ duration(event.retry_delay * 1000) }}</template><template v-if="event.next_attempt_number"> · 下一次尝试 #{{ event.next_attempt_number }}</template><template v-if="event.previous_key || event.next_key"> · {{ event.previous_key || '未提供' }} → {{ event.next_key || '未提供' }}</template><template v-if="event.previous_model || event.next_model"> · {{ event.previous_model || '未提供' }} → {{ event.next_model || '未提供' }}</template></small><div v-if="event.reason" class="timeline-reason">{{ event.reason }}</div></li></ol><div v-for="a in r.attempts" :key="a.attempt_id" class="attempt"><b>{{ a.attempt_id }}</b> · {{ a.backend }} · {{ a.key }} · {{ a.model }} · {{ a.outcome }} {{ a.error }} {{ a.status_code }}<span v-if="a.observation_incomplete"> · 观测信息不完整</span><span v-if="a.reason"> · {{ a.reason }}</span> · {{ duration(a.ended_ms == null ? null : a.ended_ms - a.started_ms) }} · Token {{ exact(a.usage?.total_tokens) }}</div><details v-if="r.response_preview || r.reasoning_preview"><summary>返回与思考摘要</summary><pre v-if="r.response_preview">{{ r.response_preview }}</pre><pre v-if="r.reasoning_preview">{{ r.reasoning_preview }}</pre></details></div></td></tr></template></tbody></table><div v-if="!rows.length" class="empty-state"><div class="empty-state__title">暂无匹配请求</div><div class="empty-state__desc">请求到达后将在此显示阶段与用量。</div></div></div>
  <div v-if="pages > 1" class="row pagination"><button class="btn btn--secondary btn--sm" :disabled="page === 1" @click="page--">上一页</button><span>{{ page }} / {{ pages }}</span><button class="btn btn--secondary btn--sm" :disabled="page === pages" @click="page++">下一页</button></div>
</section>
</template>
<style scoped>
/* 筛选工具栏：卡片化一行，搜索与下拉分组排布，慢请求改为 chip 开关 */
.request-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2) var(--sp-3);
  padding: var(--sp-2) var(--sp-3);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  margin-bottom: 12px;
}
.request-toolbar__group { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); min-width: 0; }
.request-toolbar__group:first-child { flex: 1 1 280px; }
.request-toolbar__search { flex: 1 1 200px; min-width: 180px; }
.request-toolbar__select { width: auto; min-width: 108px; }
.filter-chip {
  display: inline-flex;
  align-items: center;
  height: 32px;
  padding: 0 12px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--r-full);
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  transition: all var(--dur-fast) var(--ease-out);
  cursor: pointer;
  white-space: nowrap;
}
.filter-chip:hover { background: var(--bg-subtle); color: var(--text); }
.filter-chip--active { background: var(--accent); border-color: var(--accent); color: white; }

.request-table { overflow-x: auto; }
.request-table table { min-width: 960px; }
.request-table td { vertical-align: top; }
.request-table small { display: block; color: var(--text-muted); font-size: 11px; margin-top: 4px; }
/* 数值列右对齐 + 等宽数字：耗时/Token/尝试等量化指标纵向可比较 */
.request-table .col-num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.request-table thead th.col-num { text-align: right; }
.request-id { background: none; border: 0; color: var(--accent-strong); font: inherit; cursor: pointer; white-space: nowrap; }
.request-failed { background: var(--danger-subtle); }
.request-detail { padding: 16px; max-width: 1100px; font-size: 12px; white-space: normal; overflow-wrap: anywhere; }
.request-detail pre { white-space: pre-wrap; max-height: 240px; overflow: auto; }
.usage-grid { display: flex; flex-wrap: wrap; gap: 20px; padding: 12px 0; }
.timeline { padding-left: 20px; }
.timeline li { padding: 4px; }
.timeline-reason { margin-top: 4px; color: var(--text-muted); }
.timeline span { color: var(--accent-strong); }
.attempt { padding: 8px 0; border-top: 1px solid var(--border); }
.pagination { justify-content: flex-end; margin-top: 12px; }
@media (max-width: 768px) {
  .request-toolbar__search { flex: 1 1 100%; }
  .request-toolbar__select { flex: 1 1 calc(50% - 12px); min-width: 130px; }
  .request-toolbar__group:first-child { flex-basis: 100%; }
  .filter-chip { flex: 1 1 100%; justify-content: center; }
}
</style>
