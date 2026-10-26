<script setup>
import { computed, ref } from 'vue'
import { useDashboardStore } from '../../stores/dashboard'
const store = useDashboardStore()
const feedback = ref('')
const backend = computed(() => store.config.enableVertex ? 'Vertex AI' : 'AI Studio')
const protocols = [
  { name: 'OpenAI Chat', path: '/v1/chat/completions', label: '对话与工具调用', symbol: '01' },
  { name: 'Responses', path: '/v1/responses', label: '响应与函数工具', symbol: '02' },
  { name: 'Claude Messages', path: '/v1/messages', label: '消息与内容块', symbol: '03' },
  { name: 'Gemini 原生', path: '/v1beta/models/{model}:generateContent', label: '原生生成请求', symbol: '04', native: true },
]
async function copy(protocol, base = false) {
  try {
    await navigator.clipboard.writeText(`${window.location.origin}${base ? protocol.native ? '/v1beta' : '/v1' : protocol.path}`)
    feedback.value = `${protocol.name} ${base ? 'Base URL' : '接口地址'}已复制`
  } catch {
    feedback.value = '复制失败，请选择下方地址手动复制'
  }
}
</script>

<template>
  <section class="section" aria-labelledby="protocol-title">
    <div class="section__header">
      <div><h2 id="protocol-title" class="section__title">协议接入</h2><p class="section-description">为现有客户端选择对应入口，继续使用管理端配置的访问密码。</p></div>
      <span class="pill">4 个入口</span>
    </div>
    <div class="protocol-grid">
      <article v-for="protocol in protocols" :key="protocol.path" class="protocol-card">
        <div class="row row--between"><span class="protocol-number mono">{{ protocol.symbol }}</span><span class="protocol-backend">{{ protocol.native ? 'AI Studio' : backend }}</span></div>
        <h3>{{ protocol.name }}</h3>
        <p>{{ protocol.label }}</p>
        <code>{{ protocol.path }}</code>
        <button class="btn btn--ghost btn--sm" @click="copy(protocol)" :aria-label="`复制 ${protocol.name} 接口地址`">复制接口地址 <span aria-hidden="true">↗</span></button>
        <button class="btn btn--ghost btn--sm" @click="copy(protocol, true)" :aria-label="`复制 ${protocol.name} Base URL`">复制 Base URL</button>
      </article>
    </div>
    <p v-if="feedback" class="section-description" role="status">{{ feedback }}</p>
    <p class="section-description protocol-note">客户端要求 Base URL 时使用基础地址；Gemini 完整接口中的 {model} 需替换为实际模型。Responses 需传完整会话历史；Gemini 原生入口始终使用 AI Studio。具体能力以项目文档为准。</p>
  </section>
</template>

<style scoped>
.protocol-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.protocol-card { background: var(--bg-elevated); border: 1px solid var(--border); border-radius: var(--r-xl); padding: 20px; min-width: 0; }
.protocol-number { color: var(--accent-strong); font-size: 12px; }
.protocol-backend { color: var(--text-muted); font-size: 11px; }
.protocol-card h3 { margin: 16px 0 3px; font-size: 15px; font-weight: 600; }
.protocol-card p { font-size: 12px; color: var(--text-muted); margin: 0 0 14px; }
.protocol-card code { display: block; background: var(--bg-subtle); border: 1px solid var(--border-subtle); padding: 10px; border-radius: 6px; font-size: 11px; overflow-wrap: anywhere; user-select: all; }
.protocol-card button { margin-top: 12px; padding-left: 0; color: var(--accent-strong); }
.protocol-note { margin-top: 12px; }
@media (max-width: 540px) { .protocol-grid { grid-template-columns: 1fr; } }
</style>
