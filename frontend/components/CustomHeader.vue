<!-- CustomHeader.vue
     ag-grid hands a custom header the whole header cell: its built-in sort
     arrow and column-menu (filter) button are NOT drawn for us. This header
     wires them itself through the params ag-grid passes in — progressSort()
     for a click on the name, showColumnMenu() for the filter button — and
     follows the column's sort / filter state through its events (and, inside
     a report, the report's shared filters through the filter bridge). No refresh()
     method: on a column-definition change ag-grid builds a fresh header. -->
<template>
  <!-- Every header starts at the same edge (left in LTR, right in RTL),
       numeric columns too: an end-aligned header floated next to the hidden
       filter button, and a truncated one filled the cell anyway. -->
  <div
    class="custom-header"
    :style="headerStyles">
    <div
      class="custom-header__label"
      :class="{ 'cursor-pointer': sortable }"
      data-testid="grid-header-label"
      @click="onLabelClick"
      @mouseenter="showTooltip"
      @mouseleave="hideTooltip">
      <!-- dir="auto": an English name in a Hebrew header (and vice versa) is
           cut at its own end, not its start. -->
      <span ref="nameEl" class="custom-header__name" dir="auto">{{ name }}</span>
      <span
        v-if="sort"
        class="custom-header__sort"
        :data-sort="sort"
        aria-hidden="true">{{ sort === 'asc' ? '▲' : '▼' }}</span>
    </div>
    <button
      v-if="hasMenu"
      ref="menuBtn"
      type="button"
      class="custom-header__menu"
      :class="{ 'custom-header__menu--active': filterOn }"
      data-testid="grid-header-menu"
      :aria-label="name"
      @click.stop="openMenu">
      <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
        <path fill="currentColor" d="M1.5 2h13a.5.5 0 0 1 .38.82L10 8.6V13a.5.5 0 0 1-.28.45l-3 1.5A.5.5 0 0 1 6 14.5V8.6L1.12 2.82A.5.5 0 0 1 1.5 2Z"/>
      </svg>
    </button>
    <div v-if="tooltipVisible" :style="tooltipStyles" class="tooltip2" role="tooltip">
      <div class="font-semibold break-words">{{ name }}</div>
      <pre v-if="headerStats" class="whitespace-pre-line mt-1" :style="tooltipTextStyles">{{ headerStats }}</pre>
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
      sort: null,
      filterActive: false,
    };
  },
  computed: {
    colDef() {
      return this.params.column?.getColDef?.() || {};
    },
    name() {
      return this.params.displayName || this.colDef.headerName || '';
    },
    sortable() {
      return !!this.params.enableSorting;
    },
    hasMenu() {
      return !!this.params.enableMenu;
    },
    // Inside a report the column filter lives in the report's shared filters
    // (the grid's own filter stays off), so ask the bridge too — it also
    // lights up for a condition on this column set in the Filters popover.
    filterOn() {
      const bridge = this.params.filterBridge || this.colDef.headerComponentParams?.filterBridge;
      const field = this.colDef.field || this.params.column?.getColId?.();
      return this.filterActive || !!(bridge && field && bridge.enabled() && bridge.isActive(field));
    },
    headerStats() {
      return this.params.statsText || this.colDef.headerComponentParams?.statsText || '';
    },
    themeTokens() {
      return this.params.themeTokens || this.colDef.headerComponentParams?.themeTokens || {};
    },
    headerStyles() {
      // Color intentionally omitted: it inherits from .ag-header-cell, whose
      // color tracks the theme CSS vars reactively (a live color-mode flip
      // updates it; header component params captured in columnDefs would not).
      return {
        fontFamily: this.themeTokens.fontFamily || 'Inter, ui-sans-serif, system-ui'
      };
    },
    tooltipTextStyles() {
      return {
        color: this.themeTokens.tooltip?.textStyle?.color || this.themeTokens.textColor || '#374151'
      };
    }
  },
  mounted() {
    const column = this.params.column;
    this.syncSort();
    this.syncFilter();
    column?.addEventListener?.('sortChanged', this.syncSort);
    column?.addEventListener?.('filterActiveChanged', this.syncFilter);
  },
  beforeUnmount() {
    const column = this.params.column;
    column?.removeEventListener?.('sortChanged', this.syncSort);
    column?.removeEventListener?.('filterActiveChanged', this.syncFilter);
  },
  methods: {
    syncSort() {
      this.sort = this.params.column?.getSort?.() || null;
    },
    syncFilter() {
      this.filterActive = !!this.params.column?.isFilterActive?.();
    },
    onLabelClick(event) {
      if (!this.sortable) return;
      // asc → desc → none; shift-click adds the column to a multi-sort.
      this.params.progressSort(event.shiftKey);
    },
    openMenu() {
      this.hideTooltip();
      this.params.showColumnMenu(this.$refs.menuBtn);
    },
    showTooltip(event) {
      const el = this.$refs.nameEl;
      const truncated = el && el.scrollWidth > el.clientWidth;
      if (!truncated && !this.headerStats) return;
      const rect = event.currentTarget.getBoundingClientRect();
      // Centered under the label, clamped into the viewport so a header at
      // either edge (the first column in RTL sits on the right) stays visible.
      const half = 95;
      const center = Math.min(Math.max(rect.left + rect.width / 2, half + 4), window.innerWidth - half - 4);
      this.tooltipStyles = {
        top: `${rect.bottom + 5}px`,
        left: `${center}px`,
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
  align-items: center;
  gap: 4px;
  width: 100%;
  min-width: 0;
}

.custom-header__label {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: 1 1 auto;
  min-width: 0;
}

.custom-header__name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.custom-header__sort {
  flex: none;
  font-size: 0.65em;
  opacity: 0.8;
}

.custom-header__menu {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 3px;
  opacity: 0;
  color: inherit;
  transition: opacity 0.1s;
}

.ag-header-cell:hover .custom-header__menu,
.custom-header__menu:focus-visible,
.custom-header__menu--active {
  opacity: 0.75;
}

.custom-header__menu:hover {
  opacity: 1;
  background-color: color-mix(in srgb, currentColor 12%, transparent);
}

.custom-header__menu--active {
  color: rgb(37 99 235);
}

/* Touch screens have no hover: keep the filter button reachable. */
@media (hover: none) {
  .custom-header__menu { opacity: 0.75; }
}

.tooltip2 {
  z-index: 9999;
  max-width: 190px;
  word-wrap: break-word;
  white-space: normal; /* Allow text to wrap */
  text-align: start;
  padding: 0.5rem;
  font-weight: normal;
  border: solid 2px;
  border-radius: 0.25rem;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
  /* Display only: it sits over the first rows, and must not catch the
     pointer (or the header label would never get its mouseleave). */
  pointer-events: none;
  /* Colors are applied dynamically via inline styles */
}
</style>
