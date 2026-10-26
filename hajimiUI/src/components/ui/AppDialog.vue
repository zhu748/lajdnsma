<script>
let scrollLocks = 0
let originalOverflow = ''
</script>
<script setup>
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps({ modelValue: Boolean, title: { type: String, required: true }, busy: Boolean, width: { type: String, default: '480px' } })
const emit = defineEmits(['update:modelValue'])
const panel = ref(null)
let previousFocus
let listening = false
const focusable = 'button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex="0"]'

function close() { if (!props.busy) emit('update:modelValue', false) }
function onKey(event) {
  if (!panel.value?.contains(document.activeElement)) return
  if (event.key === 'Escape') { event.preventDefault(); close() }
  if (event.key !== 'Tab') return
  const elements = [...panel.value.querySelectorAll(focusable)].filter(el => !el.hidden && el.getAttribute('aria-hidden') !== 'true')
  const first = elements[0] || panel.value
  const last = elements.at(-1) || panel.value
  if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.value)) {
    event.preventDefault(); last.focus()
  } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === panel.value)) {
    event.preventDefault(); first.focus()
  }
}
function cleanup() {
  if (!listening) return
  document.removeEventListener('keydown', onKey)
  scrollLocks -= 1
  if (scrollLocks === 0) document.body.style.overflow = originalOverflow
  listening = false
  if (previousFocus?.isConnected) previousFocus.focus()
}
watch(() => props.modelValue, async open => {
  if (!open) { cleanup(); return }
  previousFocus = document.activeElement
  if (scrollLocks === 0) originalOverflow = document.body.style.overflow
  scrollLocks += 1
  document.body.style.overflow = 'hidden'
  listening = true
  document.addEventListener('keydown', onKey)
  await nextTick()
  if (props.modelValue) (panel.value?.querySelector('input:not([disabled]), textarea:not([disabled]), select:not([disabled])') || panel.value)?.focus()
}, { immediate: true })
onBeforeUnmount(cleanup)
</script>

<template>
  <div v-if="modelValue" class="modal-overlay" @click.self="close">
    <div ref="panel" class="modal" :style="{ maxWidth: width }" role="dialog" aria-modal="true" :aria-label="title" :aria-busy="busy" tabindex="-1">
      <slot :close="close" />
    </div>
  </div>
</template>
