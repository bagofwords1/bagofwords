<template>
  <div class="custom-loading-renderer">
    <div v-for="i in 3" :key="i" class="skeleton-row">
      <div class="skeleton-cell" v-for="j in columns" :key="j"></div>
    </div>
  </div>
</template>

<script setup>
const props = defineProps({
  columns: {
    type: Number,
    required: true,
  },
});
</script>

<style scoped>
.custom-loading-renderer {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.skeleton-row {
  display: flex;
  gap: 8px;
}

.skeleton-cell {
  flex: 1;
  height: 20px;
  background: #ccc;
  border-radius: 4px;
  animation: pulse 1.5s infinite ease-in-out;
}

@keyframes pulse {
  0% {
    opacity: 1;
  }
  50% {
    opacity: 0.4;
  }
  100% {
    opacity: 1;
  }
}
</style>