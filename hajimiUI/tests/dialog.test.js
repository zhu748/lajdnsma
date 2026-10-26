import { afterEach, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import AppDialog from '../src/components/ui/AppDialog.vue'

const wrappers = []
function dialog(props = {}) {
  const wrapper = mount(AppDialog, {
    attachTo: document.body,
    props: { modelValue: true, title: '操作确认', ...props },
    slots: { default: '<input aria-label="内容"><button>取消</button><button>确定</button>' },
  })
  wrappers.push(wrapper)
  return wrapper
}
afterEach(() => {
  wrappers.splice(0).reverse().forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
  document.body.style.overflow = ''
})

it('traps keyboard focus, closes with Escape, and restores the opener', async () => {
  const opener = document.createElement('button')
  document.body.append(opener)
  opener.focus()
  const wrapper = dialog()
  await nextTick()
  expect(document.activeElement).toBe(wrapper.get('input').element)
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, cancelable: true }))
  expect(document.activeElement).toBe(wrapper.findAll('button').at(-1).element)
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', cancelable: true }))
  expect(document.activeElement).toBe(wrapper.get('input').element)
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', cancelable: true }))
  expect(wrapper.emitted('update:modelValue')).toEqual([[false]])
  await wrapper.setProps({ modelValue: false })
  expect(document.activeElement).toBe(opener)
  expect(document.body.style.overflow).toBe('')
})

it('prevents backdrop and Escape dismissal while busy', async () => {
  const wrapper = dialog({ busy: true })
  await nextTick()
  await wrapper.get('.modal-overlay').trigger('click')
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
  expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  await wrapper.setProps({ busy: false })
  await wrapper.get('.modal-overlay').trigger('click')
  expect(wrapper.emitted('update:modelValue')).toEqual([[false]])
})

it('keeps scrolling locked until the last dialog closes', async () => {
  document.body.style.overflow = 'auto'
  const first = dialog()
  const second = dialog()
  await nextTick()
  expect(document.body.style.overflow).toBe('hidden')
  await second.setProps({ modelValue: false })
  expect(document.body.style.overflow).toBe('hidden')
  await first.setProps({ modelValue: false })
  expect(document.body.style.overflow).toBe('auto')
})
