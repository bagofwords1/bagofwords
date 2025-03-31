<!-- CustomHeader.vue -->
<template>
  <div class="custom-header">
    <div class="flex items-center gap-1">
      {{ params.displayName || params.column?.colDef?.headerName }}
      <span v-if="headerStats" 
         class="text-gray-500 cursor-help relative"
         @mouseenter="showTooltip"
         @mouseleave="hideTooltip">
         ...
      </span>
    </div>
    <div v-if="tooltipVisible" :style="tooltipStyles" class="tooltip2">
      <pre class="whitespace-pre-line text-gray-700">{{ headerStats }}</pre>
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
  computed: {
    headerStats() {
      // Try different possible locations of the stats text in the params
      return this.params.statsText || 
             this.params.column?.colDef?.headerComponentParams?.statsText ||
             this.params.column?.colDef?.headerTooltip;
    }
  },
  methods: {
    showTooltip(event) {
      const iconContainer = event.currentTarget;
      const rect = iconContainer.getBoundingClientRect();
      
      this.tooltipStyles = {
        top: `${rect.bottom + 5}px`,
        left: `${rect.left + (rect.width / 2)}px`,
        position: 'fixed',
        transform: 'translateX(-50%)',
        zIndex: 9999,
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