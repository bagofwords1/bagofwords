// A create_data result read as a figure: what create_data inferred decides
// the shape; a single value is never a figure.
//   cd frontend && node tests/unit/figureModel.mjs
import assert from 'node:assert/strict'
import { figureModel, formatFigureValue } from '../../composables/figureModel.ts'

const te = (type, rows, view = {}, extra = {}) => ({
  status: 'success',
  created_step: { data: { rows, columns: Object.keys(rows[0] || {}).map((f) => ({ field: f, headerName: f })) }, data_model: { type } },
  created_visualizations: [{ view: { view: { type, ...view } } }],
  ...extra,
})
const countries = [{ country: 'USA', revenue: 523.06 }, { country: 'Canada', revenue: 303.96 }, { country: 'France', revenue: 195.1 }]

// Categorical single series -> horizontal bars, in the order the query returned.
{
  const m = figureModel(te('bar_chart', countries, { x: 'country', y: 'revenue', axisY: { format: 'currency', currency: 'USD' } }))
  assert.equal(m.kind, 'bars')
  assert.deepEqual(m.categories, ['USA', 'Canada', 'France'])
  assert.deepEqual(m.series[0].values, [523.06, 303.96, 195.1])
  assert.equal(m.format, 'currency')
}
// Periods read left to right -> columns; a line stays a line; area marks area.
{
  const months = ['2024-01', '2024-02', '2024-03'].map((month, i) => ({ month, orders: 10 + i }))
  assert.equal(figureModel(te('bar_chart', months, { x: 'month', y: 'orders' })).kind, 'columns')
  assert.equal(figureModel(te('line_chart', months, { x: 'month', y: 'orders' })).kind, 'line')
  assert.equal(figureModel(te('area_chart', months, { x: 'month', y: 'orders' })).area, true)
  assert.equal(figureModel(te('bar_chart', countries, { x: 'country', y: 'revenue', horizontal: true })).kind, 'bars')
}
// A breakdown (groupBy) pivots into one series per group, gaps as null.
{
  const rows = [
    { year: '2023', region: 'EU', sales: 5 }, { year: '2023', region: 'US', sales: 7 },
    { year: '2024', region: 'EU', sales: 6 },
  ]
  const m = figureModel(te('bar_chart', rows, { x: 'year', y: 'sales', groupBy: 'region' }))
  assert.equal(m.kind, 'columns')
  assert.deepEqual(m.series.map((s) => [s.name, s.values]), [['EU', [5, 6]], ['US', [7, null]]])
}
// A "breakdown" whose groups share no category (each month in one year) is one line.
{
  const rows = ['2021-01', '2021-02', '2022-01'].map((month) => ({ month, year: month.slice(0, 4), revenue: 1 }))
  const m = figureModel(te('line_chart', rows, { x: 'month', y: 'revenue', groupBy: 'year' }))
  assert.equal(m.series.length, 1)
  assert.deepEqual(m.series[0].values, [1, 1, 1])
}
// Several measures without a breakdown: one series per measure, labelled.
{
  const rows = [{ m: 'Jan', a: 1, b: 2 }, { m: 'Feb', a: 3, b: 4 }]
  const m = figureModel(te('line_chart', rows, { x: 'm', y: ['a', 'b'], seriesStyles: [{ value: 'b', label: 'Bravo' }] }))
  assert.deepEqual(m.series.map((s) => s.name), ['a', 'Bravo'])
}
// Never a figure: a count card, a one-cell result.
assert.equal(figureModel(te('count', [{ total: 412 }])).kind, 'number')
assert.equal(figureModel(te('table', [{ total: 412 }])).kind, 'number')
// Tables stay tables; unknown chart types go to the existing renderer.
assert.equal(figureModel(te('table', countries)).kind, 'table')
assert.equal(figureModel(te('pie_chart', countries, { category: 'country', value: 'revenue' })).kind, 'other')
// A preview says so until the full rows are loaded.
{
  const t = te('table', countries)
  t.created_step.data.total_rows = 59
  assert.equal(figureModel(t).partial, true)
  assert.equal(figureModel(t, { rows: countries }).partial, false)
}
// Units: currency, percent from either 0-1 or 0-100, compact for big numbers.
assert.equal(formatFigureValue(523.06, 'currency', 'USD', 'en'), '$523.06')
assert.equal(formatFigureValue(60, 'currency', 'USD', 'en', true), '$60')
assert.equal(formatFigureValue(0.224, 'percent'), '22.4%')
assert.equal(formatFigureValue(22.4, 'percent'), '22.4%')
assert.equal(formatFigureValue(1250000, 'number', 'USD', 'en', true), '1.25M')
console.log('PASS: figures follow create_data inference; single values are never figures')
