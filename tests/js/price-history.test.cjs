/* Uses Node's built-in runner only; the application has no Node dependency. */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const {runInNewContext} = require('node:vm');

const source = readFileSync('static/js/price-history.js', 'utf8');

function render(language, fail = false) {
  let configuration;
  const canvas = {dataset: {errorMessage: language === 'ru' ? 'График недоступен.' : 'The chart is unavailable.'}};
  const status = {textContent: ''};
  const errors = [];
  const payload = {timezone: 'Asia/Yekaterinburg', currency: 'USD', series: [{
    label: 'Кофемашина <Demo>', sampled: false,
    points: [{at: '2026-10-21T10:00:00Z', price: '1299.9500000001'},
             {at: '2026-10-21T11:00:00Z', price: null}],
  }]};
  runInNewContext(source, {
    document: {documentElement: {lang: language}, getElementById(id) {
      return {'price-chart': canvas, 'chart-status': status,
        'price-chart-data': {textContent: JSON.stringify(payload)}}[id];
    }},
    Intl, Date,
    Chart: function(_, config) { if (fail) throw new Error('Controlled chart failure'); configuration = config; },
    console: {error: (...args) => errors.push(args)},
  });
  return {configuration, canvas, status, errors};
}

for (const [language, expected] of [['en', '1,299.9500000001'], ['ru', '1\u00a0299,9500000001']]) {
  test(`${language}: exact tooltip, dates, locale and missing-price gaps`, () => {
    const {configuration, canvas, errors} = render(language);
    assert.equal(configuration.options.locale, language);
    const dataset = configuration.data.datasets[0];
    assert.equal(dataset.label, 'Кофемашина <Demo>');
    assert.equal(dataset.data[0].exactPrice, '1299.9500000001');
    assert.equal(dataset.data[1].y, null);
    assert.equal(dataset.spanGaps, false);
    assert.equal(configuration.options.plugins.tooltip.callbacks.label({dataset, raw: dataset.data[0]}),
      `Кофемашина <Demo>: ${expected} USD`);
    assert.equal(configuration.options.plugins.tooltip.callbacks.title([{parsed: {x: dataset.data[0].x}}]),
      new Intl.DateTimeFormat(language, {timeZone: 'Asia/Yekaterinburg', dateStyle:'short', timeStyle:'medium'}).format(new Date(dataset.data[0].x)));
    assert.equal(canvas.dataset.rendered, 'true');
    assert.equal(errors.length, 0);
  });

  test(`${language}: chart failure uses the translated HTML message`, () => {
    const result = render(language, true);
    assert.equal(result.status.textContent, result.canvas.dataset.errorMessage);
    assert.equal(result.errors.length, 1);
    assert.equal(result.canvas.dataset.rendered, undefined);
  });
}

test('pages without a chart do not need Chart.js', () => {
  assert.doesNotThrow(() => runInNewContext(source, {document: {getElementById: () => null}}));
});
