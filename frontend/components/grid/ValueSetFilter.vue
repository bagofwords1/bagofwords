<!-- ValueSetFilter.vue — the column filter behind every header's funnel.
     A searchable list of the column's values (tick what to keep), or From / To
     for a numeric column with too many distinct values to list. ag-grid's own
     value list (the "set filter") is Enterprise-only, so this is a custom
     filter component: ag-grid calls isFilterActive / doesFilterPass /
     getModel / setModel, and filterChangedCallback() re-runs the filter.

     Inside a report (chat / side panel) a bridge is passed in filterParams:
     the selection is written to the report's shared filters instead — the
     chart and the Filters popover follow — and the grid's own filter stays
     inert (its rows arrive already filtered). See utils/valueFilter.ts. -->
<template>
  <div class="vsf" data-testid="grid-value-filter">
    <template v-if="rangeMode">
      <div class="vsf__range">
        <input
          v-model="fromText"
          type="number"
          class="vsf__input"
          :placeholder="t('dataGrid.inRangeStart')"
          :aria-label="t('dataGrid.inRangeStart')"
          data-testid="grid-value-filter-from"
          @input="onRangeInput">
        <span class="vsf__dash" aria-hidden="true">–</span>
        <input
          v-model="toText"
          type="number"
          class="vsf__input"
          :placeholder="t('dataGrid.inRangeEnd')"
          :aria-label="t('dataGrid.inRangeEnd')"
          data-testid="grid-value-filter-to"
          @input="onRangeInput">
      </div>
    </template>
    <template v-else>
      <input
        ref="search"
        v-model="query"
        type="search"
        class="vsf__input vsf__search"
        :placeholder="t('dataGrid.search')"
        :aria-label="t('dataGrid.search')"
        data-testid="grid-value-filter-search">
      <div class="vsf__list">
        <label v-if="visible.length" class="vsf__item vsf__item--all">
          <input
            type="checkbox"
            :checked="allVisibleChecked"
            :indeterminate.prop="someVisibleChecked && !allVisibleChecked"
            data-testid="grid-value-filter-all"
            @change="toggleAllVisible">
          <span class="vsf__label">{{ t('dataGrid.selectAll') }}</span>
        </label>
        <label v-for="o in shown" :key="o.key" class="vsf__item" data-testid="grid-value-filter-item">
          <input type="checkbox" :checked="selected.has(o.key)" @change="toggle(o.key)">
          <span class="vsf__label" dir="auto" :title="labelOf(o)">{{ labelOf(o) }}</span>
          <span class="vsf__count">{{ o.count }}</span>
        </label>
        <div v-if="!visible.length" class="vsf__hint">{{ t('dataGrid.noMatches') }}</div>
        <div v-else-if="visible.length > maxRows" class="vsf__hint">{{ t('dataGrid.moreValues', { n: maxRows }) }}</div>
      </div>
    </template>
    <div v-if="otherFilters" class="vsf__hint vsf__hint--note" data-testid="grid-value-filter-other">
      {{ t('dataGrid.alsoFilteredAbove') }}
    </div>
    <div class="vsf__footer">
      <button
        type="button"
        class="vsf__reset"
        :disabled="!active"
        data-testid="grid-value-filter-reset"
        @click="reset">{{ t('dataGrid.resetFilter') }}</button>
    </div>
  </div>
</template>

<script>
import {
  BLANK_KEY, MAX_LIST_FOR_NUMBERS, MAX_LIST_ROWS,
  collectValues, selectionModel, selectedKeys, isActiveModel, valuePasses, matchesSearch,
} from '~/utils/valueFilter';

export default {
  props: ['params'],
  data() {
    return {
      model: null,          // standalone grid: the active filter
      options: [],          // [{ key, raw, count }]
      selected: new Set(),
      query: '',
      fromText: '',
      toText: '',
      otherFilters: false,
      maxRows: MAX_LIST_ROWS,
    };
  },
  computed: {
    field() { return this.params.colDef?.field || this.params.column?.getColId?.() || ''; },
    // Only while the host can actually filter (a report and a visualization
    // id); otherwise filter this grid's own rows.
    bridge() { const b = this.params.bridge; return b && b.enabled() ? b : null; },
    rangeMode() {
      return !!this.params.numeric
        && this.options.filter((o) => o.key !== BLANK_KEY).length > MAX_LIST_FOR_NUMBERS;
    },
    visible() {
      const blank = this.t('dataGrid.blankValue');
      return this.options.filter((o) => matchesSearch(o, this.query, blank));
    },
    shown() { return this.visible.slice(0, MAX_LIST_ROWS); },
    allVisibleChecked() { return this.visible.length > 0 && this.visible.every((o) => this.selected.has(o.key)); },
    someVisibleChecked() { return this.visible.some((o) => this.selected.has(o.key)); },
    active() { return isActiveModel(this.currentModel()); },
  },
  mounted() {
    this.refresh();
  },
  methods: {
    t(key, args) { return this.params.t ? this.params.t(key, args) : key; },
    labelOf(o) { return o.key === BLANK_KEY ? this.t('dataGrid.blankValue') : String(o.raw); },

    // ---- ag-grid filter contract ----
    isFilterActive() { return !this.bridge && isActiveModel(this.model); },
    doesFilterPass(p) { return valuePasses(this.model, p.data?.[this.field]); },
    getModel() { return this.bridge || !isActiveModel(this.model) ? null : this.model; },
    setModel(model) {
      if (this.bridge) return;
      this.model = model || null;
      this.syncFromModel();
    },
    afterGuiAttached() {
      this.refresh();
      this.$nextTick(() => this.$refs.search?.focus?.());
    },

    // ---- state ----
    currentModel() { return this.bridge ? this.bridge.getModel(this.field) : this.model; },
    rows() {
      if (this.bridge) return this.bridge.rows() || [];
      const out = [];
      this.params.api?.forEachNode?.((node) => { if (node.data) out.push(node.data); });
      return out;
    },
    refresh() {
      const field = this.field;
      this.options = collectValues(this.rows().map((r) => r?.[field]));
      this.otherFilters = !!this.bridge?.hasOtherFilters(field);
      this.syncFromModel();
    },
    syncFromModel() {
      const model = this.currentModel();
      this.selected = selectedKeys(model, this.options);
      this.fromText = model?.mode === 'range' && model.from != null ? String(model.from) : '';
      this.toText = model?.mode === 'range' && model.to != null ? String(model.to) : '';
    },
    commit(model) {
      if (this.bridge) {
        this.bridge.setModel(this.field, model, this.options);
      } else {
        this.model = model;
        this.params.filterChangedCallback?.();
      }
    },

    // ---- list ----
    toggle(key) {
      const next = new Set(this.selected);
      if (next.has(key)) next.delete(key); else next.add(key);
      this.selected = next;
      this.commit(selectionModel(next, this.options));
    },
    // With a search typed, "Select all" ticks / unticks just the matches.
    toggleAllVisible() {
      const next = new Set(this.selected);
      const on = !this.allVisibleChecked;
      for (const o of this.visible) { if (on) next.add(o.key); else next.delete(o.key); }
      this.selected = next;
      this.commit(selectionModel(next, this.options));
    },

    // ---- range ----
    onRangeInput() {
      clearTimeout(this._rangeTimer);
      this._rangeTimer = setTimeout(() => {
        const num = (s) => (String(s).trim() === '' || !Number.isFinite(Number(s)) ? null : Number(s));
        const from = num(this.fromText);
        const to = num(this.toText);
        this.commit(from == null && to == null ? null : { mode: 'range', from, to });
      }, 300);
    },

    reset() {
      this.query = '';
      this.commit(null);
      this.syncFromModel();
    },
  },
  beforeUnmount() {
    clearTimeout(this._rangeTimer);
  },
};
</script>

<style>
.vsf {
  width: 232px;
  padding: 8px 10px 6px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  color: var(--ag-foreground-color);
  font-size: 12px;
}
.vsf__input {
  width: 100%;
  min-height: 28px;
  border: 1px solid var(--ag-border-color);
  border-radius: 6px;
  background-color: var(--ag-background-color);
  color: var(--ag-foreground-color);
  padding-inline: 8px;
  font-size: 12px;
  outline: none;
}
.vsf__input:focus {
  border-color: rgb(96 165 250);
  box-shadow: 0 0 0 2px rgb(96 165 250 / 0.18);
}
.vsf__range {
  display: flex;
  align-items: center;
  gap: 6px;
}
.vsf__dash { opacity: 0.5; }
.vsf__list {
  max-height: 232px;
  overflow-y: auto;
  margin-inline: -4px;
}
.vsf__item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 4px;
  border-radius: 4px;
  cursor: pointer;
  line-height: 18px;
}
.vsf__item:hover {
  background-color: color-mix(in srgb, var(--ag-foreground-color) 6%, transparent);
}
.vsf__item input {
  flex: none;
  margin: 0;
  accent-color: rgb(37 99 235);
}
.vsf__item--all {
  border-bottom: 1px solid color-mix(in srgb, var(--ag-border-color) 70%, transparent);
  border-radius: 0;
  margin-bottom: 2px;
  padding-bottom: 5px;
}
.vsf__label {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.vsf__count {
  flex: none;
  font-size: 11px;
  opacity: 0.45;
  font-variant-numeric: tabular-nums;
}
.vsf__hint {
  padding: 4px;
  font-size: 11px;
  opacity: 0.55;
}
.vsf__hint--note { padding: 0; }
.vsf__footer {
  display: flex;
  justify-content: flex-end;
}
.vsf__reset {
  padding: 2px 4px;
  font-size: 11px;
  font-weight: 500;
  color: inherit;
  opacity: 0.55;
  background: transparent;
}
.vsf__reset:hover:not(:disabled) { opacity: 0.9; }
.vsf__reset:disabled { opacity: 0.25; cursor: default; }
</style>
