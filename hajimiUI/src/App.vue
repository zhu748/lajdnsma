<script setup>
import { RouterView } from 'vue-router'
</script>

<template>
  <RouterView />
</template>

<style>
/* App-level reset only — main.css already imports base.css. */
html,
body,
#app {
  height: 100%;
}
#app {
  display: flex;
  flex-direction: column;
}
</style>
