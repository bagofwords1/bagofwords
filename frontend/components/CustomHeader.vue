<!-- CustomHeader.vue -->
<template>
  <div class="custom-header" @mouseenter="showTooltip" @mouseleave="hideTooltip">
    <div>
      {{ params.displayName }}

    </div>
    <div v-if="tooltipVisible" :style="tooltipStyles" class="tooltip2">
      {{ params.description || "No description"}} 
      <a v-if="params.description" class='text-gray-200 block hover:underline text-xs mt-3' href="#">&times; Not relevant</a>
    </div>
  </div>
</template>

<script>
export default {
  props: ['params'],
  data() {
    return {
      tooltipVisible: false,
      tooltipStyles: {},
    };
  },
  methods: {
    showTooltip(event) {
      const rect = event.target.getBoundingClientRect();
      this.tooltipStyles = {
        top: `${rect.bottom}px`,
        left: `${rect.left + rect.width / 2}px`,
        position: 'fixed',
        transform: 'translateX(-50%)',
      };
      this.tooltipVisible = true;
    },
    hideTooltip() {
      this.tooltipVisible = false;
    },
  },
};
</script>

<style>
.custom-header {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.tooltip2 {
  background-color: #fff; 
  z-index: 9999;
  max-width: 170px;
  word-wrap: break-word;
  white-space: normal; /* Allow text to wrap */
  text-align: left;
  color:#232323;
  padding: 0.5rem;
  font-weight: normal;
  border:solid 2px #eee;
  border-radius: 0.25rem;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
}
</style>