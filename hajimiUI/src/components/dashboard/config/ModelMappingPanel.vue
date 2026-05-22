<script setup>
const props = defineProps({
  title: { type: String, required: true },
  help: { type: String, required: true },
  aliases: { type: Array, required: true },
  defaultModel: { type: String, default: '' },
  availableModels: { type: Array, default: () => [] },
  aliasPlaceholder: { type: String, default: '????? gpt-*' },
  emptyHint: { type: String, default: '????????' }
})

const emit = defineEmits(['update:defaultModel', 'add-alias', 'remove-alias'])

function targetModels() {
  return props.availableModels.filter(model => model && model !== 'all')
}
</script>

<template>
  <div class="model-mapping-section">
    <h4 class="subsection-title">{{ title }}</h4>
    <p class="config-hint">{{ help }}</p>

    <div class="config-row">
      <div class="config-group full-width">
        <label class="config-label">??????</label>
        <select
          class="config-input"
          :value="defaultModel"
          @change="emit('update:defaultModel', $event.target.value)"
        >
          <option value="">???????????</option>
          <option v-for="model in targetModels()" :key="model" :value="model">{{ model }}</option>
        </select>
      </div>
    </div>

    <div class="mapping-header">
      <span>?????????? * ????</span>
      <button type="button" class="add-alias-button" @click="emit('add-alias')">????</button>
    </div>

    <div v-if="aliases.length === 0" class="config-hint">{{ emptyHint }}</div>
    <div v-for="(item, index) in aliases" :key="index" class="alias-row">
      <input v-model="item.alias" class="config-input" :placeholder="aliasPlaceholder">
      <select v-model="item.model" class="config-input">
        <option value="">???????</option>
        <option v-for="model in targetModels()" :key="model" :value="model">{{ model }}</option>
      </select>
      <button type="button" class="remove-alias-button" @click="emit('remove-alias', index)">??</button>
    </div>
  </div>
</template>

<style scoped>
.model-mapping-section {
  margin-top: 18px;
  padding: 16px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--stats-item-bg);
}

.subsection-title {
  margin: 0 0 10px;
  color: var(--color-heading);
}

.mapping-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 12px 0;
  flex-wrap: wrap;
}

.alias-row {
  display: grid;
  grid-template-columns: minmax(160px, 1fr) minmax(220px, 1fr) auto;
  gap: 10px;
  margin-top: 10px;
}

.add-alias-button,
.remove-alias-button {
  padding: 8px 12px;
  border: none;
  border-radius: var(--radius-md);
  color: white;
  cursor: pointer;
}

.add-alias-button {
  background: var(--button-primary);
}

.remove-alias-button {
  background: #dc3545;
}

.config-hint {
  color: var(--color-text);
  opacity: 0.75;
  font-size: 13px;
}

@media (max-width: 768px) {
  .alias-row {
    grid-template-columns: 1fr;
  }
}
</style>
