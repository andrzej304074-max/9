/* Backtester GBP/USD — logika frontu: konfiguracja, wywołania API i render wyników. */
'use strict';

const STORAGE_KEY = 'gbpusd-backtester-config-v2';
const WEEKDAY_SHORT = ['Pn', 'Wt', 'Śr', 'Cz', 'Pt', 'So', 'Nd'];
const STRATEGIES = ['candle_direction', 'range_breakout'];
const STRATEGY_LABELS = {
  candle_direction: 'Kierunek świecy',
  range_breakout: 'Wybicie zakresu',
};

const state = {
  defaults: {},
  options: {},
  datasetId: null,
  result: null,
  sort: { key: 'date', dir: 'asc' },
  chart: null,
  page: 0,
  pageSize: 250,
  dukascopyJob: null,
  strategy: 'candle_direction',
  // każda strategia trzyma własny komplet ustawień, żeby przełączanie nic nie gubiło
  saved: {},
  compare: null,
};

const $ = (id) => document.getElementById(id);

const nf = (min, max) => new Intl.NumberFormat('pl-PL', {
  minimumFractionDigits: min,
  maximumFractionDigits: max === undefined ? min : max,
});
const fmtMoney = nf(2);
const fmtPct2 = nf(2);
const fmtNum1 = nf(1);

// liczba miejsc po przecinku zależy od instrumentu: 1,27890 dla pary walutowej,
// ale 95 000,00 dla bitcoina — pięć miejsc byłoby tam tylko szumem
let fmtPrice = nf(5);

function setPriceFormat(medianPrice) {
  const decimals = !medianPrice || medianPrice < 20 ? 5 : (medianPrice < 1000 ? 3 : 2);
  fmtPrice = nf(decimals);
}

function signed(value, formatter, suffix = '') {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const sign = value > 0 ? '+' : (value < 0 ? '−' : '');
  return sign + formatter.format(Math.abs(value)) + suffix;
}

function plainOrDash(value, formatter, suffix = '') {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return formatter.format(value) + suffix;
}

function toneClass(value) {
  if (value === null || value === undefined || Number.isNaN(value) || value === 0) return '';
  return value > 0 ? 'value-good' : 'value-bad';
}

function setStatus(message, kind = '') {
  const el = $('status');
  el.textContent = message;
  el.dataset.kind = kind;
}

/* ---------------- inicjalizacja ---------------- */

async function init() {
  buildWeekdayToggles();
  try {
    const res = await fetch('api/options');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    state.defaults = data.defaults;
    state.options = data.options;
    fillSelect($('timezone'), Object.fromEntries(data.timezones.map((t) => [t, t])));
    Object.entries(data.options).forEach(([key, values]) => fillSelect($(key), values));
    fillSelect($('duka_instrument'), data.dukascopy_instruments || {});
    state.saved = loadStoredConfigs();
    state.strategy = state.saved.__active || 'candle_direction';
    applyConfig(configFor(state.strategy));
    setDefaultDukascopyRange();
  } catch (err) {
    setStatus('Nie udało się pobrać ustawień z serwera: ' + err.message, 'error');
    return;
  }
  wireEvents();
  syncConditionalFields();
}

function fillSelect(select, values) {
  if (!select) return;
  select.innerHTML = '';
  for (const [value, label] of Object.entries(values)) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    select.append(option);
  }
}

function buildWeekdayToggles() {
  const host = $('weekdays');
  host.innerHTML = '';
  WEEKDAY_SHORT.forEach((name, index) => {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.value = String(index);
    input.className = 'weekday-toggle';
    label.append(input, document.createTextNode(name));
    host.append(label);
  });
}

function wireEvents() {
  const form = $('config-form');
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    runBacktest();
  });
  // gdyby natywna walidacja zablokowała wysłanie, powiedz o tym wprost zamiast milczeć
  form.addEventListener('invalid', (event) => {
    const field = event.target;
    const name = form.querySelector(`label[for="${field.id}"]`);
    setStatus(`Popraw pole „${name ? name.textContent : field.id}”: ${field.validationMessage}`, 'error');
  }, true);
  $('btn-reset').addEventListener('click', () => {
    delete state.saved[state.strategy];   // reset dotyczy tylko bieżącej strategii
    applyConfig(configFor(state.strategy));
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state.saved));
    syncConditionalFields();
    setStatus(`Przywrócono ustawienia domyślne strategii „${STRATEGY_LABELS[state.strategy]}”.`, 'ok');
  });
  $('btn-sample').addEventListener('click', loadSample);
  $('btn-fetch').addEventListener('click', fetchFromNetwork);
  $('btn-duka').addEventListener('click', startDukascopy);
  $('btn-duka-cancel').addEventListener('click', cancelDukascopy);
  $('file-input').addEventListener('change', uploadFile);
  $('btn-export').addEventListener('click', exportCsv);
  $('only-trades').addEventListener('change', () => { state.page = 0; renderTradesTable(); });

  $('page-prev').addEventListener('click', () => { state.page -= 1; renderTradesTable(); });
  $('page-next').addEventListener('click', () => { state.page += 1; renderTradesTable(); });
  $('page-size').addEventListener('change', (event) => {
    state.pageSize = Number(event.target.value);
    state.page = 0;
    renderTradesTable();
  });

  ['sl_method', 'position_mode', 'sizing_mode', 'direction_mode',
   'breakout_window_mode', 'breakout_retry_mode', 'breakout_trigger'].forEach((id) => {
    $(id).addEventListener('change', syncConditionalFields);
  });

  document.querySelectorAll('.strategy-tab').forEach((tab) => {
    tab.addEventListener('click', () => switchStrategy(tab.dataset.strategy));
  });
  $('btn-compare').addEventListener('click', runCompare);
  $('btn-compare-close').addEventListener('click', () => {
    state.compare = null;
    $('compare-card').hidden = true;
  });

  document.querySelectorAll('#trades-table th[data-sort]').forEach((th) => {
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      state.sort = {
        key,
        dir: state.sort.key === key && state.sort.dir === 'asc' ? 'desc' : 'asc',
      };
      state.page = 0;
      renderTradesTable();
    });
  });

  window.addEventListener('resize', () => {
    if (state.chart) drawChart();
    if (state.compare) drawCompareChart();
  });
}

/* ---------------- konfiguracja ---------------- */

function applyConfig(cfg) {
  const set = (id, value) => { const el = $(id); if (el) el.value = value ?? ''; };

  set('timezone', cfg.timezone);
  set('signal_time', pad2(cfg.signal_hour) + ':' + pad2(cfg.signal_minute));
  set('close_time', pad2(cfg.close_time_hour) + ':' + pad2(cfg.close_time_minute));
  set('breakout_until', pad2(cfg.breakout_until_hour) + ':' + pad2(cfg.breakout_until_minute));

  ['candle_minutes', 'direction_mode', 'doji_mode', 'entry_mode', 'rr_ratio', 'sl_method',
   'sl_pips', 'sl_percent', 'sl_multiplier', 'pip_size', 'spread_pips', 'tie_break',
   'position_mode', 'close_after_days', 'initial_capital', 'leverage', 'sizing_mode',
   'risk_percent', 'lookback_days', 'date_from', 'date_to',
   'breakout_window_mode', 'breakout_hours', 'breakout_trigger', 'breakout_buffer_pips',
   'breakout_retry_mode', 'breakout_max_per_day', 'breakout_both_sides'].forEach((id) => set(id, cfg[id]));

  const active = new Set(cfg.weekdays || [0, 1, 2, 3, 4]);
  document.querySelectorAll('.weekday-toggle').forEach((input) => {
    input.checked = active.has(Number(input.value));
  });
}

function pad2(value) {
  return String(value ?? 0).padStart(2, '0');
}

function num(id, fallback) {
  const value = parseFloat($(id).value);
  return Number.isFinite(value) ? value : fallback;
}

function collectConfig() {
  const [signalHour, signalMinute] = ($('signal_time').value || '08:00').split(':').map(Number);
  const [closeHour, closeMinute] = ($('close_time').value || '22:00').split(':').map(Number);
  const [untilHour, untilMinute] = ($('breakout_until').value || '17:00').split(':').map(Number);

  return {
    strategy: state.strategy,
    breakout_window_mode: $('breakout_window_mode').value,
    breakout_until_hour: untilHour,
    breakout_until_minute: untilMinute,
    breakout_hours: num('breakout_hours', 6),
    breakout_trigger: $('breakout_trigger').value,
    breakout_buffer_pips: num('breakout_buffer_pips', 0),
    breakout_retry_mode: $('breakout_retry_mode').value,
    breakout_max_per_day: num('breakout_max_per_day', 5),
    breakout_both_sides: $('breakout_both_sides').value,
    timezone: $('timezone').value,
    signal_hour: signalHour,
    signal_minute: signalMinute,
    candle_minutes: num('candle_minutes', 15),
    direction_mode: $('direction_mode').value,
    doji_mode: $('doji_mode').value,
    entry_mode: $('entry_mode').value,
    rr_ratio: num('rr_ratio', 4),
    sl_method: $('sl_method').value,
    sl_pips: num('sl_pips', 20),
    sl_percent: num('sl_percent', 0.1),
    sl_multiplier: num('sl_multiplier', 1),
    pip_size: num('pip_size', 0.0001),
    spread_pips: num('spread_pips', 0),
    tie_break: $('tie_break').value,
    position_mode: $('position_mode').value,
    close_time_hour: closeHour,
    close_time_minute: closeMinute,
    close_after_days: num('close_after_days', 0),
    initial_capital: num('initial_capital', 10000),
    leverage: num('leverage', 30),
    sizing_mode: $('sizing_mode').value,
    risk_percent: num('risk_percent', 1),
    lookback_days: num('lookback_days', 0),
    date_from: $('date_from').value || null,
    date_to: $('date_to').value || null,
    weekdays: [...document.querySelectorAll('.weekday-toggle')]
      .filter((input) => input.checked)
      .map((input) => Number(input.value)),
  };
}

function loadStoredConfigs() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {};
  } catch {
    return {};
  }
}

function configFor(strategy) {
  return { ...state.defaults, ...(state.saved[strategy] || {}), strategy };
}

function persistCurrent() {
  state.saved[state.strategy] = collectConfig();
  state.saved.__active = state.strategy;
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state.saved));
}

function switchStrategy(strategy) {
  if (strategy === state.strategy) return;
  persistCurrent();                       // zapamiętaj ustawienia opuszczanej strategii
  state.strategy = strategy;
  applyConfig(configFor(strategy));
  syncConditionalFields();
  if (state.datasetId) runBacktest();
}

function paintStrategyTabs() {
  document.querySelectorAll('.strategy-tab').forEach((tab) => {
    const active = tab.dataset.strategy === state.strategy;
    tab.classList.toggle('is-active', active);
    tab.setAttribute('aria-selected', String(active));
  });
  $('strategy-note').textContent = {
    candle_direction: 'Kierunek świecy 8:00–8:15 decyduje o pozycji: zielona → long, czerwona → short. '
      + 'Wejście zaraz po jej zamknięciu.',
    range_breakout: 'Zapamiętujemy zakres świecy 8:00–8:15 i czekamy, aż cena go przebije. '
      + 'Gramy w stronę wybicia, stop po przeciwnej stronie zakresu.',
  }[state.strategy] || '';
  $('trades-table').dataset.strategy = state.strategy;
}

function syncConditionalFields() {
  paintStrategyTabs();

  // sekcje należące tylko do jednej ze strategii
  document.querySelectorAll('[data-strategy-only]').forEach((el) => {
    el.hidden = el.dataset.strategyOnly !== state.strategy;
  });
  document.querySelectorAll('[data-when-window]').forEach((el) => {
    el.hidden = state.strategy !== 'range_breakout'
      || el.dataset.whenWindow !== $('breakout_window_mode').value;
  });
  document.querySelectorAll('[data-when-retry]').forEach((el) => {
    el.hidden = state.strategy !== 'range_breakout'
      || el.dataset.whenRetry !== $('breakout_retry_mode').value;
  });

  $('trigger-hint').textContent = {
    touch: 'Wystarczy, że cena sięgnie granicy zakresu — wejście po cenie tego poziomu.',
    close_beyond: 'Świeca musi zamknąć się poza zakresem — wejście po jej cenie zamknięcia.',
  }[$('breakout_trigger').value] || '';

  $('retry-hint').textContent = {
    single: 'Każdy dzień daje najwyżej jedną pozycję.',
    opposite: 'Po zamknięciu pierwszej pozycji łapiemy jeszcze wybicie przeciwnej granicy.',
    unlimited: 'Każde kolejne wybicie otwiera nową pozycję, aż do limitu dziennego.',
  }[$('breakout_retry_mode').value] || '';

  $('sl-method-hint').textContent = $('sl_method').value === 'candle_range'
    ? (state.strategy === 'range_breakout'
      ? 'Stop ląduje na przeciwnej granicy zakresu — tej, od której cena się odbiła.'
      : 'Stop ląduje na dołku świecy (dla longa) albo na jej szczycie (dla shorta).')
    : '';

  const slMethod = $('sl_method').value;
  document.querySelectorAll('[data-when-sl]').forEach((el) => {
    el.hidden = el.dataset.whenSl !== slMethod;
  });
  const positionMode = $('position_mode').value;
  document.querySelectorAll('[data-when-position]').forEach((el) => {
    el.hidden = el.dataset.whenPosition !== positionMode;
  });
  const sizingMode = $('sizing_mode').value;
  document.querySelectorAll('[data-when-sizing]').forEach((el) => {
    el.hidden = el.dataset.whenSizing !== sizingMode;
  });
  $('position-hint').textContent = {
    parallel: 'Każdy dzień dostaje własną pozycję — kilka może być otwartych naraz.',
    close_at_time: 'Pozycja zamykana o wskazanej godzinie, o ile wcześniej nie trafi TP ani SL.',
    replace: 'Otwarcie nowej pozycji zamyka poprzednią po tej samej cenie.',
    skip: 'Nowy sygnał jest ignorowany, dopóki poprzednia pozycja żyje.',
  }[positionMode] || '';

  $('direction-hint').textContent = {
    follow: 'Świeca zielona → long, czerwona → short. To pierwotna logika strategii.',
    invert: 'Świeca zielona → short, czerwona → long.',
    long_only: 'Grane są tylko sygnały długie; krótkie są pomijane.',
    short_only: 'Grane są tylko sygnały krótkie; długie są pomijane.',
  }[$('direction_mode').value] || '';

  $('sizing-hint').textContent = {
    compound: 'Nominał = bieżący kapitał × dźwignia. Zyski powiększają kolejne pozycje.',
    fixed_notional: 'Nominał = kapitał początkowy × dźwignia. Każda pozycja tej samej wielkości.',
    risk_percent: 'Wielkość liczona wstecz z odległości stop lossa. Dźwignia działa jak limit.',
  }[sizingMode] || '';
}

/* ---------------- wczytywanie danych ---------------- */

async function callApi(url, options) {
  const res = await fetch(url, options);
  let payload = null;
  try {
    payload = await res.json();
  } catch {
    /* odpowiedź bez JSON-a */
  }
  if (!res.ok) {
    const detail = payload && payload.detail;
    throw new Error(typeof detail === 'string' ? detail : `Błąd serwera (HTTP ${res.status}).`);
  }
  return payload;
}

function onDatasetLoaded(data) {
  state.datasetId = data.dataset_id;
  const label = $('dataset-label');
  label.textContent = data.source;
  label.className = 'pill pill-ready';
  $('dataset-detail').textContent =
    `${data.bars.toLocaleString('pl-PL')} świec · ${data.first_date} → ${data.last_date}` +
    (data.interval_minutes ? ` · interwał ${data.interval_minutes} min` : '');

  setPriceFormat(data.median_price);

  // rozmiar pipsa zależy od instrumentu — dopasuj go, jeśli podpowiedź się nie zgadza
  const warnings = [...(data.warnings || [])];
  const suggested = data.suggested_pip_size;
  if (suggested && Math.abs(num('pip_size', 0.0001) - suggested) > suggested * 1e-6) {
    $('pip_size').value = suggested;
    warnings.push(`Rozmiar pipsa ustawiono na ${suggested} — dopasowany do poziomu cen tego ` +
      'instrumentu. Możesz go zmienić; wpływa tylko na metodę „stałe pipsy” i spread.');
  }

  setStatus(
    warnings.length ? warnings.join('\n') : 'Dane wczytane. Możesz uruchomić backtest.',
    warnings.length ? '' : 'ok',
  );
  runBacktest();
}

async function uploadFile(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) return;
  const body = new FormData();
  body.append('file', file);
  await withBusy('btn-run', 'Wczytuję plik…', async () => {
    const tz = encodeURIComponent($('timezone').value);
    onDatasetLoaded(await callApi(`api/upload?timezone=${tz}`, { method: 'POST', body }));
  });
}

async function loadSample() {
  await withBusy('btn-sample', 'Ładuję dane demo…', async () => {
    const tz = encodeURIComponent($('timezone').value);
    onDatasetLoaded(await callApi(`api/sample?timezone=${tz}`));
  });
}

async function fetchFromNetwork() {
  await withBusy('btn-fetch', 'Pobieram dane z sieci…', async () => {
    onDatasetLoaded(await callApi('api/fetch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        timezone: $('timezone').value,
        symbol: $('fetch_symbol').value.trim() || 'GBPUSD=X',
      }),
    }));
  });
}

/* ---------------- Dukascopy: pełne archiwum, pobierane w tle ---------------- */

function setDefaultDukascopyRange() {
  const end = new Date();
  end.setDate(end.getDate() - 1);
  const start = new Date(end);
  start.setMonth(start.getMonth() - 3);
  const iso = (d) => d.toISOString().slice(0, 10);
  $('duka_to').value = iso(end);
  $('duka_to').max = iso(end);
  $('duka_from').value = iso(start);
  $('duka_from').max = iso(end);
}

function showDukascopyProgress(visible) {
  $('duka-progress').hidden = !visible;
  $('btn-duka').disabled = visible;
}

async function startDukascopy() {
  const body = {
    instrument: $('duka_instrument').value,
    date_from: $('duka_from').value,
    date_to: $('duka_to').value,
    interval_minutes: Number($('duka_interval').value),
    timezone: $('timezone').value,
  };
  if (!body.date_from || !body.date_to) {
    setStatus('Podaj zakres dat do pobrania z Dukascopy.', 'error');
    return;
  }

  showDukascopyProgress(true);
  $('duka-fill').style.width = '0%';
  $('duka-text').textContent = 'Nawiązuję połączenie…';
  setStatus('Pobieram dane z Dukascopy…');

  try {
    const { job_id: jobId } = await callApi('api/dukascopy/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    state.dukascopyJob = jobId;
    pollDukascopy(jobId);
  } catch (err) {
    showDukascopyProgress(false);
    setStatus(err.message, 'error');
  }
}

async function pollDukascopy(jobId) {
  while (state.dukascopyJob === jobId) {
    await new Promise((resolve) => setTimeout(resolve, 800));
    let job;
    try {
      job = await callApi(`api/dukascopy/status/${jobId}`);
    } catch (err) {
      showDukascopyProgress(false);
      setStatus(err.message, 'error');
      return;
    }

    if (job.state === 'running') {
      const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
      $('duka-fill').style.width = `${pct}%`;
      $('duka-text').textContent = job.total
        ? `${pct}% · ${job.done.toLocaleString('pl-PL')} z ${job.total.toLocaleString('pl-PL')} godzin`
        : 'Przygotowuję listę plików…';
      continue;
    }

    state.dukascopyJob = null;
    showDukascopyProgress(false);
    if (job.state === 'done') {
      onDatasetLoaded(job.dataset);
    } else {
      setStatus(job.error || 'Pobieranie nie powiodło się.', 'error');
    }
    return;
  }
}

async function cancelDukascopy() {
  const jobId = state.dukascopyJob;
  if (!jobId) return;
  state.dukascopyJob = null;
  showDukascopyProgress(false);
  setStatus('Pobieranie przerwane.');
  try {
    await callApi(`api/dukascopy/cancel/${jobId}`, { method: 'POST' });
  } catch {
    /* zadanie i tak zostanie porzucone */
  }
}

async function withBusy(buttonId, message, task) {
  const button = $(buttonId);
  button.disabled = true;
  setStatus(message);
  try {
    await task();
  } catch (err) {
    setStatus(err.message, 'error');
  } finally {
    button.disabled = false;
  }
}

/* ---------------- backtest ---------------- */

async function runBacktest() {
  if (!state.datasetId) {
    setStatus('Najpierw wczytaj dane — plik CSV z TradingView albo dane demo.', 'error');
    return;
  }
  const config = collectConfig();
  persistCurrent();

  await withBusy('btn-run', 'Liczę…', async () => {
    state.result = await callApi('api/backtest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dataset_id: state.datasetId, config }),
    });
    render();
    const { trades_closed: closed, signal_days: days } = state.result.summary;
    setStatus(`Gotowe — ${closed} zagranych pozycji na ${days} dni sygnałowych.`, 'ok');
  });
}

/* ---------------- porównanie obu strategii ---------------- */

async function runCompare() {
  if (!state.datasetId) {
    setStatus('Najpierw wczytaj dane.', 'error');
    return;
  }
  persistCurrent();

  const configs = {};
  STRATEGIES.forEach((name) => { configs[name] = configFor(name); });
  configs[state.strategy] = collectConfig();   // bieżąca strategia bierze stan formularza

  await withBusy('btn-compare', 'Liczę obie strategie…', async () => {
    const data = await callApi('api/compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dataset_id: state.datasetId, configs }),
    });
    state.compare = data.results;
    $('results').hidden = false;
    renderCompare();
    $('compare-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
    setStatus('Obie strategie policzone na tych samych danych.', 'ok');
  });
}

function compareSeriesColor(index) {
  const css = getComputedStyle(document.body);
  return css.getPropertyValue(index === 0 ? '--series' : '--series-2').trim();
}

function renderCompare() {
  const results = state.compare;
  if (!results) return;
  $('compare-card').hidden = false;

  $('compare-grid').innerHTML = STRATEGIES.filter((name) => results[name]).map((name, index) => {
    const s = results[name].summary;
    const rows = [
      ['Kumulatywna stopa zwrotu', signed(s.return_pct, fmtPct2, '%'), toneClass(s.return_pct)],
      ['Wynik w pieniądzu', signed(s.net_profit, fmtMoney), toneClass(s.net_profit)],
      ['Kapitał końcowy', fmtMoney.format(s.final_equity), ''],
      ['Skuteczność', plainOrDash(s.win_rate, fmtPct2, '%'), ''],
      ['Transakcji', String(s.trades_closed), ''],
      ['Dni pominiętych', String(s.trades_skipped), ''],
      ['Max obsunięcie', '−' + fmtPct2.format(s.max_drawdown_pct) + '%', s.max_drawdown_pct > 0 ? 'value-bad' : ''],
      ['Profit factor', plainOrDash(s.profit_factor, fmtPct2), ''],
    ];
    return `
      <div class="compare-col" style="--swatch: ${compareSeriesColor(index)}">
        <h3>${escapeHtml(STRATEGY_LABELS[name] || name)}</h3>
        <dl class="compare-rows">
          ${rows.map(([label, value, tone]) =>
            `<dt>${escapeHtml(label)}</dt><dd class="${tone}">${escapeHtml(value)}</dd>`).join('')}
        </dl>
      </div>`;
  }).join('');

  // dwie serie na wykresie wymagają legendy — kolor nigdy nie niesie znaczenia sam
  $('compare-legend').innerHTML = STRATEGIES.filter((name) => results[name]).map((name, index) =>
    `<span class="legend-item">
       <span class="legend-swatch" style="background:${compareSeriesColor(index)}"></span>
       ${escapeHtml(STRATEGY_LABELS[name] || name)}
     </span>`).join('');

  drawCompareChart();
}

function drawCompareChart() {
  const results = state.compare;
  const canvas = $('compare-chart');
  if (!results || !canvas) return;

  const series = STRATEGIES.filter((name) => results[name]).map((name, index) => ({
    label: STRATEGY_LABELS[name] || name,
    color: compareSeriesColor(index),
    points: results[name].equity_curve.map((p) => ({
      t: new Date(p.time.replace(' ', 'T')).getTime(),
      value: p.cumulative_return_pct,
    })),
  })).filter((s) => s.points.length >= 2);

  const { ctx, width, height, pad } = chartGeometry(canvas);
  const css = getComputedStyle(document.body);
  const grid = css.getPropertyValue('--grid').trim();
  const axis = css.getPropertyValue('--axis').trim();
  const muted = css.getPropertyValue('--muted').trim();

  ctx.clearRect(0, 0, width, height);
  if (!series.length) {
    ctx.fillStyle = muted;
    ctx.font = '13px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('Za mało zamkniętych pozycji, aby narysować porównanie.', width / 2, height / 2);
    return;
  }

  const all = series.flatMap((s) => s.points);
  const tMin = Math.min(...all.map((p) => p.t));
  const tMax = Math.max(...all.map((p) => p.t)) || tMin + 1;
  const values = all.map((p) => p.value).concat([0]);
  const padding = (Math.max(...values) - Math.min(...values)) * 0.08 || 1;
  const vMin = Math.min(...values) - padding;
  const vMax = Math.max(...values) + padding;

  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const x = (t) => pad.left + ((t - tMin) / (tMax - tMin || 1)) * plotW;
  const y = (v) => pad.top + (1 - (v - vMin) / (vMax - vMin || 1)) * plotH;

  ctx.font = '11px system-ui, sans-serif';
  ctx.textBaseline = 'middle';
  ctx.lineWidth = 1;
  for (const tick of niceTicks(vMin, vMax, 5)) {
    const py = Math.round(y(tick)) + 0.5;
    ctx.strokeStyle = Math.abs(tick) < 1e-9 ? axis : grid;
    ctx.beginPath();
    ctx.moveTo(pad.left, py);
    ctx.lineTo(width - pad.right, py);
    ctx.stroke();
    ctx.fillStyle = muted;
    ctx.textAlign = 'right';
    ctx.fillText(fmtNum1.format(tick) + '%', pad.left - 8, py);
  }

  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  const labels = Math.max(2, Math.min(6, Math.floor(plotW / 90)));
  for (let i = 0; i < labels; i += 1) {
    const t = tMin + ((tMax - tMin) * i) / (labels - 1);
    ctx.fillStyle = muted;
    ctx.fillText(
      new Date(t).toLocaleDateString('pl-PL', { day: '2-digit', month: 'short' }),
      Math.min(width - pad.right, Math.max(pad.left, x(t))), height - pad.bottom + 8,
    );
  }

  for (const s of series) {
    ctx.beginPath();
    s.points.forEach((p, i) => (i ? ctx.lineTo(x(p.t), y(p.value)) : ctx.moveTo(x(p.t), y(p.value))));
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    ctx.stroke();
  }

  canvas.setAttribute('aria-label', 'Porównanie kumulatywnej stopy zwrotu: '
    + series.map((s) => `${s.label} ${signed(s.points[s.points.length - 1].value, fmtPct2, '%')}`).join(', ')
    + '. Dokładne liczby w tabeli powyżej.');
}

function render() {
  $('summary-empty').hidden = true;
  $('tiles').hidden = false;
  $('results').hidden = false;
  renderTiles();
  renderNotes();
  prepareChart();
  drawChart();
  renderWeekdayTable();
  renderTradesTable();
}

function renderTiles() {
  const s = state.result.summary;
  const tiles = [
    {
      label: 'Kumulatywna stopa zwrotu',
      value: signed(s.return_pct, fmtPct2, '%'),
      tone: toneClass(s.return_pct),
      note: `z kapitału ${fmtMoney.format(s.initial_capital)}`,
      hero: true,
    },
    {
      label: 'Wynik w pieniądzu',
      value: signed(s.net_profit, fmtMoney),
      tone: toneClass(s.net_profit),
      note: `kapitał końcowy ${fmtMoney.format(s.final_equity)}`,
      hero: true,
    },
    {
      label: 'Skuteczność',
      value: plainOrDash(s.win_rate, fmtPct2, '%'),
      note: `${s.wins} trafionych / ${s.losses} stratnych`,
    },
    {
      label: 'Dni na plus / na minus',
      value: `${s.days_positive} / ${s.days_negative}`,
      note: `${s.trades_closed} zamkniętych pozycji`,
    },
    {
      label: 'Max obsunięcie',
      value: '−' + fmtPct2.format(s.max_drawdown_pct) + '%',
      tone: s.max_drawdown_pct > 0 ? 'value-bad' : '',
      note: fmtMoney.format(s.max_drawdown_money),
    },
    {
      label: 'Profit factor',
      value: plainOrDash(s.profit_factor, fmtPct2),
      note: `TP: ${s.exit_tp} · SL: ${s.exit_sl}` +
        (s.exit_time ? ` · czas: ${s.exit_time}` : '') +
        (s.exit_replaced ? ` · zastąpione: ${s.exit_replaced}` : ''),
    },
    {
      label: 'Średnia transakcja',
      value: signed(s.avg_trade, fmtMoney),
      tone: toneClass(s.avg_trade),
      note: s.avg_hold_hours !== null ? `trzymana śr. ${fmtNum1.format(s.avg_hold_hours)} h` : '—',
    },
    {
      label: 'Max pozycji naraz',
      value: String(s.max_concurrent),
      tone: s.peak_leverage > 100 ? 'value-bad' : '',
      note: `szczytowa dźwignia ${fmtNum1.format(s.peak_leverage)}×`,
    },
  ];

  if (s.trades_open > 0) {
    tiles.push({
      label: 'Pozycje wciąż otwarte',
      value: String(s.trades_open),
      note: `wycena rynkowa ${signed(s.unrealised_pnl, fmtMoney)}`,
    });
  }
  if (s.trades_skipped > 0) {
    tiles.push({
      label: 'Dni pominięte',
      value: String(s.trades_skipped),
      note: 'powód w tabeli poniżej',
    });
  }

  $('tiles').innerHTML = tiles.map((tile) => `
    <div class="tile${tile.hero ? ' tile-hero' : ''}">
      <span class="tile-label">${escapeHtml(tile.label)}</span>
      <span class="tile-value ${tile.tone || ''}">${escapeHtml(tile.value)}</span>
      <span class="tile-note">${escapeHtml(tile.note || '')}</span>
    </div>`).join('');
}

function renderNotes() {
  const s = state.result.summary;
  const notes = [...(s.notes || [])];
  if (s.liquidated) {
    notes.unshift(`Kapitał wyzerował się ${s.liquidated_at.slice(0, 10)} — dalsze sygnały nie zostały zagrane.`);
  }
  if (s.peak_leverage > 100) {
    notes.push(`Szczytowa dźwignia sięgnęła ${fmtNum1.format(s.peak_leverage)}× — przy nakładających się ` +
      'pozycjach ekspozycja się sumuje. Realny broker zamknąłby część pozycji wezwaniem do uzupełnienia depozytu.');
  }
  const box = $('notes');
  box.hidden = notes.length === 0;
  box.textContent = notes.join('\n');
}

/* ---------------- wykres krzywej kapitału ---------------- */

function prepareChart() {
  const points = state.result.equity_curve.map((p) => ({
    t: new Date(p.time.replace(' ', 'T')).getTime(),
    value: p.cumulative_return_pct,
    equity: p.equity,
    label: p.time,
  }));
  state.chart = points.length >= 2 ? { points, hover: -1 } : null;

  const canvas = $('equity-chart');
  if (!state.chart) {
    canvas.setAttribute('aria-label', 'Za mało zamkniętych pozycji, aby narysować wykres.');
    return;
  }
  const last = points[points.length - 1];
  canvas.setAttribute('aria-label',
    `Kumulatywna stopa zwrotu od ${points[0].label} do ${last.label}, ` +
    `końcowo ${signed(last.value, fmtPct2, '%')}. Pełne dane w tabeli poniżej.`);

  canvas.onmousemove = onChartHover;
  canvas.onmouseleave = () => { state.chart.hover = -1; $('chart-tooltip').hidden = true; drawChart(); };
}

function chartGeometry(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, width, height, pad: { left: 54, right: 14, top: 12, bottom: 28 } };
}

function niceTicks(min, max, count) {
  const span = (max - min) || 1;
  const rough = span / count;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= rough) || magnitude * 10;
  const ticks = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 0.001; v += step) {
    ticks.push(Number(v.toFixed(10)));
  }
  return ticks;
}

function drawChart() {
  const canvas = $('equity-chart');
  const { ctx, width, height, pad } = chartGeometry(canvas);
  const css = getComputedStyle(document.body);
  const colors = {
    grid: css.getPropertyValue('--grid').trim(),
    axis: css.getPropertyValue('--axis').trim(),
    muted: css.getPropertyValue('--muted').trim(),
    series: css.getPropertyValue('--series').trim(),
    soft: css.getPropertyValue('--series-soft').trim(),
    surface: css.getPropertyValue('--surface').trim(),
  };

  ctx.clearRect(0, 0, width, height);
  if (!state.chart) {
    ctx.fillStyle = colors.muted;
    ctx.font = '13px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('Za mało zamkniętych pozycji, aby narysować wykres.', width / 2, height / 2);
    return;
  }

  const { points } = state.chart;
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  const tMin = points[0].t;
  const tMax = points[points.length - 1].t || tMin + 1;
  const values = points.map((p) => p.value).concat([0]);
  let vMin = Math.min(...values);
  let vMax = Math.max(...values);
  const padding = (vMax - vMin) * 0.08 || 1;
  vMin -= padding;
  vMax += padding;

  const x = (t) => pad.left + ((t - tMin) / (tMax - tMin || 1)) * plotW;
  const y = (v) => pad.top + (1 - (v - vMin) / (vMax - vMin || 1)) * plotH;

  // siatka pozioma — celowo wycofana wizualnie
  ctx.font = '11px system-ui, sans-serif';
  ctx.textBaseline = 'middle';
  ctx.lineWidth = 1;
  for (const tick of niceTicks(vMin, vMax, 5)) {
    const py = Math.round(y(tick)) + 0.5;
    ctx.strokeStyle = Math.abs(tick) < 1e-9 ? colors.axis : colors.grid;
    ctx.beginPath();
    ctx.moveTo(pad.left, py);
    ctx.lineTo(width - pad.right, py);
    ctx.stroke();
    ctx.fillStyle = colors.muted;
    ctx.textAlign = 'right';
    ctx.fillText(fmtNum1.format(tick) + '%', pad.left - 8, py);
  }

  // etykiety osi czasu
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  const labelCount = Math.max(2, Math.min(6, Math.floor(plotW / 90)));
  for (let i = 0; i < labelCount; i += 1) {
    const t = tMin + ((tMax - tMin) * i) / (labelCount - 1);
    const date = new Date(t);
    ctx.fillStyle = colors.muted;
    ctx.fillText(
      date.toLocaleDateString('pl-PL', { day: '2-digit', month: 'short' }),
      Math.min(width - pad.right, Math.max(pad.left, x(t))),
      height - pad.bottom + 8,
    );
  }

  // wypełnienie pod linią
  ctx.beginPath();
  ctx.moveTo(x(points[0].t), y(0));
  points.forEach((p) => ctx.lineTo(x(p.t), y(p.value)));
  ctx.lineTo(x(points[points.length - 1].t), y(0));
  ctx.closePath();
  ctx.fillStyle = colors.soft;
  ctx.fill();

  // seria — cienka linia, bez znaczników na każdym punkcie
  ctx.beginPath();
  points.forEach((p, i) => (i ? ctx.lineTo(x(p.t), y(p.value)) : ctx.moveTo(x(p.t), y(p.value))));
  ctx.strokeStyle = colors.series;
  ctx.lineWidth = 2;
  ctx.lineJoin = 'round';
  ctx.stroke();

  // warstwa hover: krzyżyk + znacznik
  const hover = state.chart.hover;
  if (hover >= 0 && hover < points.length) {
    const p = points[hover];
    ctx.beginPath();
    ctx.moveTo(Math.round(x(p.t)) + 0.5, pad.top);
    ctx.lineTo(Math.round(x(p.t)) + 0.5, height - pad.bottom);
    ctx.strokeStyle = colors.axis;
    ctx.lineWidth = 1;
    ctx.stroke();

    ctx.beginPath();
    ctx.arc(x(p.t), y(p.value), 5, 0, Math.PI * 2);
    ctx.fillStyle = colors.series;
    ctx.fill();
    ctx.strokeStyle = colors.surface;
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  state.chart.scale = { x, y, pad, width, height };
}

function onChartHover(event) {
  if (!state.chart || !state.chart.scale) return;
  const canvas = $('equity-chart');
  const rect = canvas.getBoundingClientRect();
  const mouseX = event.clientX - rect.left;
  const { points, scale } = state.chart;

  let nearest = 0;
  let best = Infinity;
  points.forEach((p, i) => {
    const distance = Math.abs(scale.x(p.t) - mouseX);
    if (distance < best) { best = distance; nearest = i; }
  });

  state.chart.hover = nearest;
  drawChart();

  const point = points[nearest];
  const tooltip = $('chart-tooltip');
  tooltip.innerHTML =
    `${escapeHtml(point.label)}<br>` +
    `Narastająco: <b class="${toneClass(point.value)}">${escapeHtml(signed(point.value, fmtPct2, '%'))}</b><br>` +
    `Kapitał: <b>${escapeHtml(fmtMoney.format(point.equity))}</b>`;
  tooltip.hidden = false;

  const px = scale.x(point.t);
  const py = scale.y(point.value);
  const flip = px > scale.width - tooltip.offsetWidth - 24;
  tooltip.style.left = `${flip ? px - tooltip.offsetWidth - 12 : px + 12}px`;
  tooltip.style.top = `${Math.max(4, Math.min(py - 12, scale.height - tooltip.offsetHeight - 4))}px`;
}

/* ---------------- tabele ---------------- */

function renderWeekdayTable() {
  const rows = state.result.weekday_breakdown;
  $('weekday-table').querySelector('tbody').innerHTML = rows.map((row) => `
    <tr>
      <td>${escapeHtml(row.weekday_name)}</td>
      <td class="num">${row.signals}</td>
      <td class="num">${row.trades}</td>
      <td class="num">${row.wins}</td>
      <td class="num">${escapeHtml(plainOrDash(row.win_rate, fmtPct2, '%'))}</td>
      <td class="num ${toneClass(row.pnl_money)}">${escapeHtml(signed(row.pnl_money, fmtMoney))}</td>
      <td class="num ${toneClass(row.pnl_pct_of_capital)}">${escapeHtml(signed(row.pnl_pct_of_capital, fmtPct2, '%'))}</td>
    </tr>`).join('');
}

function visibleTrades() {
  let rows = [...state.result.trades];
  if ($('only-trades').checked) {
    rows = rows.filter((t) => t.status === 'closed' || t.status === 'open');
  }
  const { key, dir } = state.sort;
  const factor = dir === 'asc' ? 1 : -1;
  rows.sort((a, b) => {
    const av = a[key];
    const bv = b[key];
    if (av === null || av === undefined) return 1;
    if (bv === null || bv === undefined) return -1;
    if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * factor;
    return String(av).localeCompare(String(bv), 'pl') * factor;
  });
  return rows;
}

function renderTradesTable() {
  document.querySelectorAll('#trades-table th[data-sort]').forEach((th) => {
    if (th.dataset.sort === state.sort.key) {
      th.setAttribute('aria-sort', state.sort.dir === 'asc' ? 'ascending' : 'descending');
    } else {
      th.removeAttribute('aria-sort');
    }
  });

  const allRows = visibleTrades();
  $('trades-empty').hidden = allRows.length > 0;

  // Przy wieloletniej historii tabela ma tysiące wierszy — rysowanie ich wszystkich naraz
  // zamraża przeglądarkę na kilka sekund przy każdym sortowaniu, więc dzielimy je na strony.
  const size = state.pageSize > 0 ? state.pageSize : allRows.length;
  const pageCount = Math.max(1, Math.ceil(allRows.length / (size || 1)));
  state.page = Math.min(Math.max(0, state.page), pageCount - 1);
  const start = state.page * size;
  const rows = allRows.slice(start, start + size);

  const pager = $('pager');
  pager.hidden = allRows.length === 0;
  $('page-prev').disabled = state.page === 0;
  $('page-next').disabled = state.page >= pageCount - 1;
  // przy powtórkach w ciągu dnia jeden dzień daje więcej niż jeden wiersz
  const days = new Set(allRows.map((t) => t.date)).size;
  const scope = days === allRows.length
    ? `${allRows.length.toLocaleString('pl-PL')} dni`
    : `${allRows.length.toLocaleString('pl-PL')} wierszy z ${days.toLocaleString('pl-PL')} dni`;
  $('page-info').textContent = allRows.length
    ? `${start + 1}–${start + rows.length} z ${scope}` +
      (pageCount > 1 ? ` · strona ${state.page + 1} z ${pageCount}` : '')
    : '';

  $('trades-table').querySelector('tbody').innerHTML = rows.map((t) => {
    const played = t.status === 'closed' || t.status === 'open';
    if (!played) {
      return `<tr class="row-skipped">
        <td>${escapeHtml(t.date)}</td>
        <td>${escapeHtml(t.weekday_name)}</td>
        <td colspan="11">${escapeHtml(t.skip_reason || 'dzień pominięty')}</td>
      </tr>`;
    }
    const dirClass = t.direction === 1 ? 'tag-long' : 'tag-short';
    const outcomeClass = t.exit_reason === 'TP' ? 'tag-tp' : (t.exit_reason === 'SL' ? 'tag-sl' : 'tag-neutral');
    const attempt = t.attempt > 1 ? ` <span class="attempt">próba ${t.attempt}</span>` : '';
    return `<tr>
      <td>${escapeHtml(t.date)}</td>
      <td>${escapeHtml(t.weekday_name)}${attempt}</td>
      <td data-strategy-col="range_breakout">${escapeHtml(t.breakout_label)}</td>
      <td><span class="tag ${dirClass}">${escapeHtml(t.direction_label)}</span></td>
      <td class="num">${escapeHtml(fmtPrice.format(t.entry_price))}</td>
      <td class="num">${escapeHtml(fmtPrice.format(t.stop_loss))}</td>
      <td class="num">${escapeHtml(fmtPrice.format(t.take_profit))}</td>
      <td>${escapeHtml(t.exit_time || '—')}</td>
      <td><span class="tag ${outcomeClass}">${escapeHtml(t.exit_reason || '—')}</span></td>
      <td class="num ${toneClass(t.pnl_money)}">${escapeHtml(signed(t.pnl_money, fmtMoney))}</td>
      <td class="num ${toneClass(t.pnl_pct)}">${escapeHtml(signed(t.pnl_pct, fmtPct2, '%'))}</td>
      <td class="num ${toneClass(t.cumulative_return_pct)}">${escapeHtml(signed(t.cumulative_return_pct, fmtPct2, '%'))}</td>
      <td class="num">${t.hold_hours === null ? '—' : escapeHtml(fmtNum1.format(t.hold_hours) + ' h')}</td>
    </tr>`;
  }).join('');
}

function exportCsv() {
  const header = ['data', 'dzien_tygodnia', 'wybicie', 'proba', 'kierunek', 'wejscie', 'stop_loss',
    'take_profit', 'wyjscie', 'wynik', 'zysk_strata', 'procent', 'narastajaco_procent',
    'godziny', 'status', 'powod'];
  const lines = [header.join(',')];

  for (const t of visibleTrades()) {
    const played = t.status === 'closed' || t.status === 'open';
    lines.push([
      t.date,
      t.weekday_name,
      t.breakout_side || '',
      t.attempt,
      played ? t.direction_label : '',
      played ? t.entry_price.toFixed(5) : '',
      played ? t.stop_loss.toFixed(5) : '',
      played ? t.take_profit.toFixed(5) : '',
      t.exit_time || '',
      t.exit_reason || '',
      played ? t.pnl_money.toFixed(2) : '',
      played ? t.pnl_pct.toFixed(4) : '',
      played ? t.cumulative_return_pct.toFixed(4) : '',
      t.hold_hours === null ? '' : t.hold_hours.toFixed(2),
      t.status,
      (t.skip_reason || '').replace(/,/g, ';'),
    ].join(','));
  }

  const blob = new Blob(['﻿' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'backtest-gbpusd.csv';
  link.click();
  URL.revokeObjectURL(url);
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

init();
