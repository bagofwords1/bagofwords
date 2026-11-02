// Text size for charts and grids shown inside a conversation (a tool's
// widget preview). The same components render dashboards, where they keep
// their dense defaults; a preview provides the chat's body size and they
// scale from it, so a chart in the chat follows the text-size setting
// instead of sitting at 11px next to 15px prose.
import type { InjectionKey, Ref } from 'vue'

/** The chat body size in px, provided by the conversation surfaces. */
export const CHAT_VIZ_TEXT_PX: InjectionKey<Ref<number>> = Symbol('chatVizTextPx')

// Ratios of the chat body size: about 12px legends and 13px axis labels and
// table cells at the 15px default.
export const chatLegendPx = (chatPx: number) => Math.round(chatPx * 0.8)
export const chatAxisPx = (chatPx: number) => Math.round(chatPx * 0.8667)
export const chatGridPx = chatAxisPx

const asList = (v: any): any[] => (Array.isArray(v) ? v : v && typeof v === 'object' ? [v] : [])

/** Apply chat sizes to an ECharts option in place: legends and axis labels.
 *  A size the chart author set explicitly on an axis is kept. */
export function applyChatChartText<T extends Record<string, any>>(option: T, chatPx: number): T {
  const legend = chatLegendPx(chatPx)
  const axis = chatAxisPx(chatPx)
  for (const l of asList(option.legend)) l.textStyle = { ...(l.textStyle || {}), fontSize: legend }
  for (const key of ['xAxis', 'yAxis', 'radiusAxis', 'angleAxis']) {
    for (const a of asList(option[key])) {
      if (a.axisLabel?.fontSize != null) continue
      a.axisLabel = { ...(a.axisLabel || {}), fontSize: axis }
    }
  }
  return option
}
