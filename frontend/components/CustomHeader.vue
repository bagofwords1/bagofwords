<!-- CustomHeader.vue -->
<template>
  <div class="custom-header" :style="headerStyles">
    <div class="flex items-center gap-1">
      {{ params.displayName || params.column?.colDef?.headerName }}
      <span v-if="headerStats" 
         :style="iconStyles"
         class="cursor-help relative"
         @mouseenter="showTooltip"
         @mouseleave="hideTooltip">
         ...
      </span>
    </div>
    <div v-if="tooltipVisible" :style="tooltipStyles" class="tooltip2">
      <pre class="whitespace-pre-line" :style="tooltipTextStyles">{{ headerStats }}</pre>
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
    },
    themeTokens() {
      return this.params.themeTokens || 
             this.params.column?.colDef?.headerComponentParams?.themeTokens || 
             {};
    },
    headerStyles() {
      return {
        color: this.themeTokens.textColor || '#0f172a',
        fontFamily: this.themeTokens.fontFamily || 'Inter, ui-sans-serif, system-ui'
      };
    },
    iconStyles() {
      return {
        color: this.themeTokens.axis?.xLabelColor || this.themeTokens.textColor || '#6b7280',
        opacity: 0.7
      };
    },
    tooltipTextStyles() {
      return {
        color: this.themeTokens.tooltip?.textStyle?.color || this.themeTokens.textColor || '#374151'
      };
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
        backgroundColor: this.themeTokens.tooltip?.backgroundColor || this.themeTokens.cardBackground || '#ffffff',
        borderColor: this.themeTokens.cardBorder || '#e5e7eb',
        color: this.themeTokens.tooltip?.textStyle?.color || this.themeTokens.textColor || '#374151'
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
  z-index: 9999;
  max-width: 170px;
  word-wrap: break-word;
  white-space: normal; /* Allow text to wrap */
  text-align: left;
  padding: 0.5rem;
  font-weight: normal;
  border: solid 2px;
  border-radius: 0.25rem;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
  /* Colors are now applied dynamically via inline styles */
}
</style>