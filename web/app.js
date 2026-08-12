/* Backtester GBP/USD — logika frontu: konfiguracja, wywołania API i render wyników. */
'use strict';

const STORAGE_KEY = 'gbpusd-backtester-config-v2';
const ARCHIVE_KEY = 'gbpusd-backtester-archiwum-v1';
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
  library: [],
  fetchIntervals: {},
  fetchHistoryDays: {},
  intervalChoice: { yahoo: '15m', dukascopy: '15' },
  // skąd wzięły się bieżące dane — pozwala odtworzyć je po utracie stanu na serwerze
  source: null,
  runtime: { serverless: false },
  strategy: 'candle_direction',
  // każda strategia trzyma własny komplet ustawień, żeby przełączanie nic nie gubiło
  saved: {},
  compare: null,
  archiveRunning: false,      // czy pętla kroków właśnie się kręci
  // Zamiar prowadzenia pobierania. Osobno od `archiveRunning`, bo między założeniem planu
  // a pierwszym krokiem jest chwila, w której pętla jeszcze nie ruszyła — a bez tego panel
  // zdążył w niej mrugnąć napisem „wstrzymane" i podstawić przycisk wznawiania.
  archiveDriving: false,
  archivePlan: null,          // ostatni stan planu — przyciski wiedzą z niego, co zrobią
  archiveProgress: {},
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

// Polska odmiana rzeczownika po liczbie: 1 doba, 2 doby, 5 dób, 12 dób, 22 doby.
function odmiana(ile, jeden, kilka, wiele) {
  if (ile === 1) return jeden;
  const dziesiatki = ile % 100;
  return (ile % 10 >= 2 && ile % 10 <= 4 && !(dziesiatki >= 12 && dziesiatki <= 14)) ? kilka : wiele;
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
    fillSelect($('duka_price'), (data.options || {}).tick_price || {});
    fillSelect($('archive_price'), (data.options || {}).tick_price || {});
    buildArchiveInstruments(data.dukascopy_instruments || {});
    state.fetchIntervals = data.fetch_intervals || {};
    state.fetchHistoryDays = data.fetch_history_days || {};
    state.intervalChoice = { yahoo: '15m', dukascopy: '15' };
    syncFetchSource();
    state.runtime = data.runtime || state.runtime;
    applyRuntimeLimits();
    state.saved = loadStoredConfigs();
    state.strategy = state.saved.__active || 'candle_direction';
    applyConfig(configFor(state.strategy));
    refreshLibrary();
    resumeArchive();      // pobieranie mogło zostać wstrzymane zamknięciem karty
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
    refreshLibrary();
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state.saved));
    syncConditionalFields();
    setStatus(`Przywrócono ustawienia domyślne strategii „${STRATEGY_LABELS[state.strategy]}”.`, 'ok');
  });
  $('btn-sample').addEventListener('click', loadSample);
  $('btn-fetch').addEventListener('click', fetchFromNetwork);
  $('fetch_source').addEventListener('change', syncFetchSource);
  $('btn-duka-cancel').addEventListener('click', cancelDukascopy);
  $('btn-duka-probe').addEventListener('click', probeDukascopy);
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

  ['sl_method', 'position_mode', 'sizing_mode', 'direction_mode', 'direction_source',
   'breakout_window_mode', 'breakout_retry_mode', 'breakout_trigger',
   'breakout_levels', 'breakout_stop_levels'].forEach((id) => {
    $(id).addEventListener('change', syncConditionalFields);
  });

  document.querySelectorAll('.strategy-tab').forEach((tab) => {
    tab.addEventListener('click', () => switchStrategy(tab.dataset.strategy));
  });
  ['fetch_interval', 'fetch_days', 'candle_minutes'].forEach((id) => {
    $(id).addEventListener('change', syncFetchHint);
    $(id).addEventListener('input', syncFetchHint);
  });
  $('library-body').addEventListener('click', handleLibraryAction);
  $('btn-library-refresh').addEventListener('click', refreshLibrary);
  $('btn-storage-probe').addEventListener('click', probeStorage);
  $('trades-table').addEventListener('click', (event) => {
    const wiersz = event.target.closest('tr.trade-row');
    if (wiersz) togglePodgladSwiec(wiersz);
  });
  $('trades-table').addEventListener('keydown', (event) => {
    const wiersz = event.target.closest('tr.trade-row');
    if (wiersz && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      togglePodgladSwiec(wiersz);
    }
  });
  $('btn-archive-start').addEventListener('click', startArchive);
  $('btn-archive-cancel').addEventListener('click', cancelArchive);
  $('btn-archive-retry').addEventListener('click', retryArchive);
  $('btn-archive-resume').addEventListener('click', resumeArchiveLoop);
  $('archive_price').addEventListener('change', persistArchiveChoice);
  ['archive_years', 'archive_interval'].forEach((id) => {
    $(id).addEventListener('change', () => { persistArchiveChoice(); scheduleArchiveEstimate(); });
    $(id).addEventListener('input', scheduleArchiveEstimate);
  });
  $('archive-panel').addEventListener('toggle', () => {
    if ($('archive-panel').open) refreshArchiveEstimate();
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

  ['candle_minutes', 'direction_source', 'direction_mode', 'doji_mode', 'entry_mode', 'rr_ratio', 'sl_method',
   'sl_pips', 'sl_percent', 'sl_multiplier', 'pip_size', 'spread_pips', 'tie_break',
   'position_mode', 'close_after_days', 'initial_capital', 'leverage', 'sizing_mode',
   'risk_percent', 'lookback_days', 'date_from', 'date_to',
   'breakout_window_mode', 'breakout_hours', 'breakout_trigger', 'breakout_buffer_pips',
   'breakout_retry_mode', 'breakout_max_per_day', 'breakout_both_sides',
   'breakout_levels', 'breakout_stop_levels'].forEach((id) => set(id, cfg[id]));

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
    breakout_levels: $('breakout_levels').value,
    breakout_stop_levels: $('breakout_stop_levels').value,
    timezone: $('timezone').value,
    signal_hour: signalHour,
    signal_minute: signalMinute,
    candle_minutes: num('candle_minutes', 15),
    direction_source: $('direction_source').value,
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

  $('direction-source-hint').textContent = {
    body: 'Liczy się tylko kolorowy korpus — otwarcie kontra zamknięcie. Knoty są pomijane.',
    swing: 'Liczy się, w którą stronę cena zaszła dalej od otwarcia: górne wychylenie '
      + '(szczyt − otwarcie) kontra dolne (otwarcie − dołek). Zamknięcie nie ma znaczenia.',
    range: 'Liczy się położenie zamknięcia względem środka między szczytem a dołkiem. '
      + 'Świeca z długim górnym knotem i zamknięciem przy dole bywa formalnie zielona, '
      + 'ale tutaj wyjdzie spadkowa — cena została odrzucona od góry.',
  }[$('direction_source').value] || '';

  $('breakout-stop-hint').textContent = {
    same: 'Stop ląduje na tej samej granicy co wejście, tylko po przeciwnej stronie.',
    range: 'Stop za pełnym wychyleniem świecy — dalej od ceny, więc luźniejszy, '
      + 'ale przy stałym RR take profit też odsuwa się dalej.',
    body: 'Stop na krańcu korpusu — bliżej ceny, więc ciaśniejszy. Można wejść na wybiciu '
      + 'pełnego wychylenia, a stop trzymać tuż przy korpusie.',
  }[$('breakout_stop_levels').value] || '';

  $('breakout-levels-hint').textContent = {
    range: 'Cena musi wyjść poza szczyt albo dołek świecy, czyli poza jej knoty.',
    body: 'Cena musi wyjść poza otwarcie albo zamknięcie. Te poziomy leżą bliżej, '
      + 'więc wybicia padają częściej i wcześniej, a stop jest ciaśniejszy.',
  }[$('breakout_levels').value] || '';

  $('sl-method-hint').textContent = {
    candle_range: state.strategy === 'range_breakout'
      ? 'Stop ląduje na przeciwnej granicy zakresu — tej, od której cena się odbiła.'
      : 'Stop ląduje na dołku świecy (dla longa) albo na jej szczycie (dla shorta).',
    candle_body: 'Stop ląduje na krańcu korpusu zamiast na końcu knota — bliżej ceny, '
      + 'więc ciaśniejszy, a przy stałym RR take profit odpowiednio bliżej.',
    candle_span: 'Dystans stopa równa się szerokości świecy (szczyt − dołek) i jest odmierzany '
      + 'od ceny wejścia. W odróżnieniu od „wychyleń” luka na otwarciu nie rozciąga ryzyka — '
      + 'stop zawsze ma tyle samo, ile mierzy świeca.',
  }[$('sl_method').value] || '';

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

  // To samo ustawienie znaczy co innego w każdej strategii: w pierwszej sygnałem jest
  // kolor świecy, w drugiej — strona, którą puścił zakres. Opis musi mówić o tym, co
  // faktycznie się liczy, inaczej wygląda na to, że silnik gra w drugą stronę.
  $('direction-hint').textContent = (state.strategy === 'range_breakout' ? {
    follow: 'Wybicie górą → long, wybicie dołem → short. To pierwotna logika strategii.',
    invert: 'Wybicie górą → short, wybicie dołem → long — gra na fałszywe wybicie.',
    long_only: 'Grane są tylko wybicia górą; wybicia dołem są pomijane.',
    short_only: 'Grane są tylko wybicia dołem; wybicia górą są pomijane.',
  } : {
    follow: 'Świeca zielona → long, czerwona → short. To pierwotna logika strategii.',
    invert: 'Świeca zielona → short, czerwona → long.',
    long_only: 'Grane są tylko sygnały długie; krótkie są pomijane.',
    short_only: 'Grane są tylko sygnały krótkie; długie są pomijane.',
  })[$('direction_mode').value] || '';

  $('sizing-hint').textContent = {
    compound: 'Nominał = bieżący kapitał × dźwignia. Zyski powiększają kolejne pozycje.',
    fixed_notional: 'Nominał = kapitał początkowy × dźwignia. Każda pozycja tej samej wielkości.',
    risk_percent: 'Wielkość liczona wstecz z odległości stop lossa. Dźwignia działa jak limit.',
  }[sizingMode] || '';
}

/* ---------------- wczytywanie danych ---------------- */

async function callApi(url, options, { allowRecovery = true } = {}) {
  const res = await fetch(url, options);
  let payload = null;
  try {
    payload = await res.json();
  } catch {
    /* odpowiedź bez JSON-a */
  }

  // 409 = trafiliśmy na instancję, która nie zna naszego zbioru danych. Przy wdrożeniu
  // bezserwerowym to normalne (każde żądanie może obsłużyć inny proces), więc zamiast
  // pokazywać błąd wysyłamy dane jeszcze raz i powtarzamy żądanie.
  if (res.status === 409 && allowRecovery && state.source) {
    const recovered = await resendDataset();
    if (recovered) {
      const retryOptions = options && options.body && typeof options.body === 'string'
        ? { ...options, body: options.body.replace(/"dataset_id":"[^"]*"/, `"dataset_id":"${recovered}"`) }
        : options;
      return callApi(url, retryOptions, { allowRecovery: false });
    }
  }

  if (!res.ok) {
    const detail = payload && payload.detail;
    throw new Error(typeof detail === 'string' ? detail : `Błąd serwera (HTTP ${res.status}).`);
  }
  return payload;
}

/** Odtwarza zbiór danych na serwerze po tym, jak instancja stracila go z pamięci. */
async function resendDataset() {
  const source = state.source;
  if (!source) return null;
  const tz = encodeURIComponent($('timezone').value);

  let data = null;
  if (source.kind === 'upload' && source.file) {
    data = await callApi(`api/upload?timezone=${tz}`, {
      method: 'POST',
      body: await buildUploadBody(source.file),
    }, { allowRecovery: false });
  } else if (source.kind === 'sample') {
    data = await callApi(`api/sample?timezone=${tz}`, undefined, { allowRecovery: false });
  } else {
    // Dane z Yahoo albo Dukascopy trzeba by pobrać od nowa — na to potrzebna jest
    // świadoma decyzja użytkownika, więc zwracamy zwykły błąd.
    return null;
  }

  state.datasetId = data.dataset_id;
  return data.dataset_id;
}

/** Pakuje CSV gzipem, jeśli przeglądarka to potrafi — inaczej nie zmieścimy się w limicie żądania. */
async function buildUploadBody(file) {
  const body = new FormData();
  const packed = await compress(file);
  if (packed) {
    body.append('file', packed, `${file.name || 'dane'}.gz`);
    body.append('encoding', 'gzip');
  } else {
    body.append('file', file);
  }
  return body;
}

/** Rozmiar w jednostce czytelnej dla danej wielkości — „0,0 MB” nikomu nic nie mówi. */
function formatBytes(bytes) {
  if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

/** Zwraca skompresowany plik albo null, gdy przeglądarka nie umie tego zrobić. */
async function compress(file) {
  if (typeof CompressionStream === 'undefined') return null;
  try {
    return await new Response(file.stream().pipeThrough(new CompressionStream('gzip'))).blob();
  } catch {
    return null;
  }
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
  refreshLibrary();

  const warnings = [...(data.warnings || [])];

  // Dane rzadsze niż świeca sygnałowa dają backtest bez ani jednej pozycji. Bez tego
  // ostrzeżenia wygląda to jak awaria, a jest zwykłą niezgodnością rozdzielczości.
  const candle = num('candle_minutes', 15);
  if (data.interval_minutes && data.interval_minutes > candle) {
    warnings.push(`Te dane mają rozdzielczość ${data.interval_minutes} min, a świeca sygnałowa `
      + `trwa ${candle} min — nie da się jej z nich złożyć i backtest nie znajdzie żadnej pozycji. `
      + `Wgraj dane o interwale ${candle} min lub gęstszym, albo ustaw dłuższą świecę w sekcji 2.`);
  }

  // rozmiar pipsa zależy od instrumentu — dopasuj go, jeśli podpowiedź się nie zgadza
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
  await withBusy('btn-run', 'Wczytuję plik…', async () => {
    const tz = encodeURIComponent($('timezone').value);
    // plik zostaje pod ręką — pozwala odtworzyć zbiór, gdy serwer straci go z pamięci
    state.source = { kind: 'upload', file };
    onDatasetLoaded(await callApi(`api/upload?timezone=${tz}`, {
      method: 'POST',
      body: await buildUploadBody(file),
    }));
  });
}

async function loadSample() {
  await withBusy('btn-sample', 'Ładuję dane demo…', async () => {
    const tz = encodeURIComponent($('timezone').value);
    state.source = { kind: 'sample' };
    onDatasetLoaded(await callApi(`api/sample?timezone=${tz}`));
  });
}

// Dukascopy składa świece z ticków, więc interwał podaje się w minutach i nie ma tu
// żadnego sufitu historii poza początkiem archiwum.
const DUKASCOPY_INTERVALS = {
  1: '1 minuta — najdokładniej, największy plik',
  5: '5 minut',
  15: '15 minut',
  30: '30 minut',
  60: '1 godzina',
};
const YAHOO_MINUTES = { '1m': 1, '5m': 5, '15m': 15, '30m': 30, '60m': 60, '1h': 60, '1d': 1440 };

function fetchSource() {
  return $('fetch_source').value;
}

/** Długość świecy wybranego interwału w minutach — wspólna miara dla obu źródeł. */
function selectedIntervalMinutes() {
  const value = $('fetch_interval').value;
  return fetchSource() === 'dukascopy' ? Number(value) : YAHOO_MINUTES[value];
}

/** Przełącza pola i listę interwałów pod wybrane źródło, pamiętając wybór dla każdego. */
function syncFetchSource() {
  const source = fetchSource();
  document.querySelectorAll('[data-source]').forEach((el) => {
    el.hidden = el.dataset.source !== source;
  });

  const select = $('fetch_interval');
  const wanted = state.intervalChoice[source];
  fillSelect(select, source === 'dukascopy' ? DUKASCOPY_INTERVALS : (state.fetchIntervals || {}));
  if (wanted && select.querySelector(`option[value="${wanted}"]`)) select.value = wanted;

  syncFetchHint();
}

async function fetchFromNetwork() {
  if (fetchSource() === 'dukascopy') return startDukascopy();

  const days = Math.max(1, num('fetch_days', 60));
  await withBusy('btn-fetch', `Pobieram ${days} dni oknami wstecz…`, async () => {
    state.source = { kind: 'fetch' };
    onDatasetLoaded(await callApi('api/fetch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        timezone: $('timezone').value,
        symbol: $('fetch_symbol').value.trim() || 'GBPUSD=X',
        interval: $('fetch_interval').value,
        days,
      }),
    }));
  });
}

/** Mówi wprost, czego się spodziewać po wybranym źródle i interwale — zanim ktoś kliknie. */
function syncFetchHint() {
  const hint = $('fetch-hint');
  const source = fetchSource();
  const wanted = num('fetch_days', 60);
  const candle = num('candle_minutes', 15);
  const dataMinutes = selectedIntervalMinutes();
  state.intervalChoice[source] = $('fetch_interval').value;

  // Świeca sygnałowa nie powstanie z danych rzadszych, niż sama trwa — to najczęstsza
  // przyczyna backtestu bez ani jednej transakcji.
  if (dataMinutes && dataMinutes > candle) {
    hint.textContent = `Uwaga: świeca sygnałowa trwa ${candle} min, a te dane mają rozdzielczość `
      + `${dataMinutes} min — nie da się z nich złożyć sygnału i backtest nie znajdzie ani jednej `
      + `pozycji. Wybierz interwał ${candle} min lub gęstszy, albo zmień długość świecy w sekcji 2.`;
    hint.classList.add('hint-limit');
    return;
  }

  if (source === 'dukascopy') {
    hint.textContent = `Archiwum sięga 2003 roku, więc ${wanted} dni pobierze się w całości. `
      + 'Idzie plik po pliku, jeden na godzinę handlu — miesiąc to kilkanaście sekund, '
      + 'rok kilka minut. Powtórka tego samego okresu jest natychmiastowa.';
    hint.classList.remove('hint-limit');
    return;
  }

  const depth = (state.fetchHistoryDays || {})[$('fetch_interval').value];
  if (!depth) { hint.textContent = ''; return; }
  const human = depth >= 10000 ? 'pełną dostępną historię'
    : (depth >= 365 ? `${Math.round(depth / 365)} lat` : `${depth} dni`);

  if (wanted > depth) {
    hint.textContent = `Yahoo trzyma dla tego interwału tylko ${human} historii, a poproszono `
      + `o ${wanted} dni — pobierze się tyle, ile jest. Po głębszą historię przełącz źródło `
      + `na Dukascopy albo wgraj plik z TradingView.`;
    hint.classList.add('hint-limit');
  } else {
    hint.textContent = 'Pobieranie idzie oknami wstecz, aż uzbiera cały okres. '
      + `Dla tego interwału Yahoo udostępnia ${human}.`;
    hint.classList.remove('hint-limit');
  }
}

/** Dostosowuje podpowiedzi do ograniczeń środowiska, w którym aplikacja została wdrożona. */
function applyRuntimeLimits() {
  const { serverless, max_upload_bytes: maxUpload } = state.runtime;
  if (!serverless) return;

  const uploadHint = $('upload-hint');
  if (uploadHint && maxUpload) {
    uploadHint.textContent = `Plik jest kompresowany w przeglądarce przed wysłaniem. `
      + `Limit po kompresji to ${(maxUpload / (1024 * 1024)).toFixed(1)} MB — `
      + `w praktyce starcza na kilkanaście lat świec 15-minutowych.`;
    uploadHint.hidden = false;
  }
}

/* ---------------- Dukascopy: pełne archiwum od 2003 roku ---------------- */

function showDukascopyProgress(visible) {
  $('duka-progress').hidden = !visible;
  $('btn-fetch').disabled = visible;
}

/** Zamienia „ile dni wstecz" na zakres dat, którego oczekuje archiwum. */
function daysBackToRange(days) {
  const iso = (d) => d.toISOString().slice(0, 10);
  const end = new Date();
  end.setUTCDate(end.getUTCDate() - 1);          // wczoraj — dzisiejsze godziny bywają niegotowe
  const start = new Date(end);
  start.setUTCDate(start.getUTCDate() - (days - 1));

  const archiveStart = new Date('2003-01-01T00:00:00Z');
  return { from: iso(start < archiveStart ? archiveStart : start), to: iso(end) };
}

async function startDukascopy() {
  const days = Math.max(1, num('fetch_days', 60));
  const range = daysBackToRange(days);
  const body = {
    instrument: $('duka_instrument').value,
    date_from: range.from,
    date_to: range.to,
    interval_minutes: selectedIntervalMinutes(),
    price: $('duka_price').value,
    timezone: $('timezone').value,
  };

  showDukascopyProgress(true);
  $('duka-fill').style.width = '0%';
  $('duka-text').textContent = 'Nawiązuję połączenie…';
  setStatus('Pobieram dane z Dukascopy…');

  // Gdy środowisko narzuca limit czasu żądania, zakres pobieramy partiami, wznawiając
  // od miejsca, w którym serwer musiał przerwać.
  if (state.runtime.serverless) {
    await downloadInChunks(body);
    return;
  }

  try {
    state.source = { kind: 'dukascopy' };
    const job = await callApi('api/dukascopy/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    state.dukascopyJob = job.job_id;
    pollDukascopy(job.job_id);
  } catch (err) {
    showDukascopyProgress(false);
    setStatus(err.message, 'error');
  }
}

/** Pyta serwer, czy w ogóle widzi archiwum Dukascopy, i pokazuje surowy wynik. */
async function probeDukascopy() {
  const out = $('duka-probe-result');
  out.hidden = false;
  out.classList.remove('hint-limit');
  out.textContent = 'Sprawdzam…';

  await withBusy('btn-duka-probe', 'Sprawdzam połączenie z archiwum…', async () => {
    const r = await callApi(`api/dukascopy/probe?instrument=${encodeURIComponent($('duka_instrument').value)}`);
    if (r.ok) {
      const c = r.candles || {};
      out.textContent = `Połączenie działa: pobrano plik testowy (${r.bytes} B, `
        + `${r.ticks.toLocaleString('pl-PL')} ticków) w ${r.ms} ms. `
        + (c.usable
          ? `Archiwum udostępnia też gotowe świece minutowe (zgodne z tickami na ${c.compared} minutach) `
            + '— pobieranie użyje ich i będzie około 24 razy szybsze.'
          : `Gotowe świece niedostępne (${c.reason || 'brak informacji'}), pobieranie pójdzie z ticków.`);
      setStatus(r.attempts > 1
        ? `Archiwum Dukascopy jest osiągalne — odpowiedziało dopiero za ${r.attempts}. razem, `
          + 'więc bywa chwilowo przeciążone.'
        : 'Archiwum Dukascopy jest osiągalne z serwera.', 'ok');
    } else {
      out.textContent = `${r.error} Adres testowy: ${r.url}`;
      out.classList.add('hint-limit');
      setStatus('Serwer nie może pobrać danych z Dukascopy — szczegóły przy przycisku.', 'error');
    }
  });
}

/** Następny dzień po podanej dacie ISO. */
function nextDay(iso) {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString().slice(0, 10);
}

/** Ile dni dzieli dwie daty ISO (włącznie). */
function daysBetween(fromIso, toIso) {
  const ms = new Date(`${toIso}T00:00:00Z`) - new Date(`${fromIso}T00:00:00Z`);
  return Math.round(ms / 86400000) + 1;
}

/** Pobiera długi zakres kawałek po kawałku i skleja go w przeglądarce.
 *
 * Każdy odcinek to osobne żądanie, więc żadne nie przekracza limitu czasu. Sklejony
 * komplet wraca na serwer jako jeden plik — dokładnie tak, jakby użytkownik wgrał go
 * ręcznie, co przy okazji pozwala odtworzyć dane po zimnym starcie instancji.
 */
async function downloadInChunks(body) {
  const token = `chunks-${Date.now()}`;
  state.dukascopyJob = token;

  const total = daysBetween(body.date_from, body.date_to);
  const parts = [];
  let bars = 0;
  let failed = 0;
  let skipped = 0;      // doby, z których archiwum nie oddało nic
  let round = 0;
  let cursor = body.date_from;
  const humanDate = (s) => s.split('-').reverse().join('.');

  try {
    while (cursor <= body.date_to) {
      if (state.dukascopyJob !== token) return;      // użytkownik przerwał

      const donePct = Math.round((daysBetween(body.date_from, cursor) / total) * 100);
      $('duka-fill').style.width = `${Math.min(99, donePct)}%`;

      // Jedna część potrafi zająć kilkadziesiąt sekund. Bez tykającego licznika
      // wygląda to jak zawieszenie, więc pokazujemy upływ czasu.
      const label = `Pobieram od ${humanDate(cursor)}`
        + (bars ? ` · ${bars.toLocaleString('pl-PL')} świec` : '')
        + (round ? ` · część ${round + 1}` : '');
      const since = Date.now();
      $('duka-text').textContent = label;
      const ticker = setInterval(() => {
        $('duka-text').textContent = `${label} · ${Math.round((Date.now() - since) / 1000)} s`;
      }, 1000);

      let chunk;
      try {
        chunk = await callApi('api/dukascopy/chunk', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          // `resumed` mówi serwerowi, że mamy już świece z wcześniejszych dni: martwa doba
          // na początku odcinka jest wtedy dziurą do zanotowania, a nie powodem do przerwania.
          body: JSON.stringify({ ...body, date_from: cursor, with_header: round === 0, resumed: bars > 0 }),
        });
      } finally {
        clearInterval(ticker);
      }
      parts.push(chunk.csv);
      bars += chunk.bars;
      failed += chunk.failed_hours || 0;
      skipped += chunk.skipped_days || 0;
      round += 1;

      // Serwer pobiera, ile zdąży w swoim limicie czasu, i mówi, dokąd doszedł.
      // Wznawiamy od następnego dnia, aż domkniemy cały zakres.
      if (chunk.complete || !chunk.covered_to) break;
      const resume = nextDay(chunk.covered_to);
      if (resume <= cursor) break;                   // brak postępu — nie zapętlaj się
      cursor = resume;
    }
    if (state.dukascopyJob !== token) return;

    if (!bars) {
      throw new Error('W wybranym zakresie nie ma żadnych notowań. Sprawdź daty — '
        + 'weekendy i święta są w archiwum puste.');
    }

    $('duka-fill').style.width = '100%';
    $('duka-text').textContent = `Scalam ${bars.toLocaleString('pl-PL')} świec i wysyłam…`;
    // Przy dziesiątkach tysięcy plików pojedyncze wywrotki są normalne — ale użytkownik
    // ma prawo wiedzieć, że w danych są dziury. Pominięta doba waży więcej niż godzina:
    // to cały brakujący dzień handlowy, którego w backteście po prostu nie ma.
    if (skipped) {
      setStatus(`Uwaga: ${skipped} ${odmiana(skipped, 'doby', 'dób', 'dób')} archiwum nie oddało `
        + 'w całości — tych dni nie ma w danych i nie wejdą do backtestu. Powtórzenie pobrania '
        + 'je uzupełni (reszta jest już w pamięci podręcznej, więc pójdzie szybko).');
    } else if (failed) {
      setStatus(`Uwaga: ${failed} ${odmiana(failed, 'godziny', 'godzin', 'godzin')} `
        + `nie udało się pobrać mimo ponowień — w danych `
        + 'mogą być drobne luki. Powtórzenie pobrania uzupełni brakujące godziny '
        + '(reszta jest już w pamięci podręcznej, więc pójdzie szybko).');
    }

    const file = new File([parts.join('')], `dukascopy-${body.instrument}.csv`, { type: 'text/csv' });
    const limit = state.runtime.max_upload_bytes;
    const packed = await compress(file);
    if (limit && packed && packed.size > limit) {
      throw new Error(
        `Pobrano ${bars.toLocaleString('pl-PL')} świec, ale scalony plik `
        + `(${formatBytes(packed.size)} po kompresji) przekracza limit `
        + `${formatBytes(limit)} tego wdrożenia. Wybierz rzadszy interwał `
        + `albo krótszy zakres — pobrane godziny są w pamięci podręcznej, więc powtórka będzie szybka.`,
      );
    }

    // od tego miejsca dane zachowują się jak zwykły wgrany plik
    state.source = { kind: 'upload', file };
    const tz = encodeURIComponent($('timezone').value);
    onDatasetLoaded(await callApi(`api/upload?timezone=${tz}`, {
      method: 'POST',
      body: await buildUploadBody(file),
    }));
  } catch (err) {
    setStatus(err.message, 'error');
  } finally {
    state.dukascopyJob = null;
    showDukascopyProgress(false);
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
  state.dukascopyJob = null;   // pętla odcinków sprawdza ten znacznik między żądaniami
  showDukascopyProgress(false);
  setStatus('Pobieranie przerwane.');
  if (jobId.startsWith('chunks-')) return;   // pobieranie odcinkami nie ma zadania na serwerze
  try {
    await callApi(`api/dukascopy/cancel/${jobId}`, { method: 'POST' });
  } catch {
    /* zadanie i tak zostanie porzucone */
  }
}

async function withBusy(target, message, task) {
  // przyjmuje identyfikator albo gotowy element — przyciski biblioteki powstają dynamicznie
  const button = typeof target === 'string' ? $(target) : target;
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

    // Backtest bez ani jednej pozycji wygląda jak awaria — jeśli znamy przyczynę, podajmy ją
    // zamiast suchego zera.
    if (!closed) {
      setStatus(explainNoTrades(days), 'error');
      return;
    }
    setStatus(`Gotowe — ${closed} zagranych pozycji na ${days} dni sygnałowych.`, 'ok');
  });
}

/** Najczęstsze powody pustego wyniku, w kolejności od najbardziej prawdopodobnego. */
function explainNoTrades(days) {
  const candle = num('candle_minutes', 15);
  const interval = state.result.dataset && state.result.dataset.interval_minutes;

  if (interval && interval > candle) {
    return `Zero pozycji: dane mają rozdzielczość ${interval} min, a świeca sygnałowa trwa `
      + `${candle} min — nie da się jej z nich złożyć. Wgraj dane o interwale ${candle} min `
      + `lub gęstszym, albo ustaw dłuższą świecę sygnałową w sekcji 2.`;
  }
  if (!days) {
    return 'Zero pozycji: żaden dzień nie przeszedł filtrów. Sprawdź zakres dat i wybrane '
      + 'dni tygodnia w sekcji 6.';
  }
  const skipped = (state.result.trades || []).find((t) => t.skip_reason);
  return `Zero pozycji na ${days} dni sygnałowych`
    + (skipped ? ` — najczęstszy powód: ${skipped.skip_reason}.` : '.')
    + ' Sprawdź godzinę świecy sygnałowej i tryb kierunku w sekcji 2.';
}

/* ---------------- biblioteka zapisanych zbiorów ---------------- */

async function refreshLibrary() {
  let data;
  try {
    data = await callApi('api/datasets', undefined, { allowRecovery: false });
  } catch {
    return;                       // biblioteka jest dodatkiem, jej awaria nie może psuć reszty
  }
  state.library = data.datasets || [];
  const usage = data.usage || {};
  // Panel jest widoczny zawsze: przy pustej bibliotece mieści ostrzeżenie o ulotnym zapisie
  // i wejście do masowego pobrania archiwum — a to właśnie wtedy jest najbardziej potrzebne.
  $('library-card').hidden = false;

  const gdzie = usage.backend ? ` · magazyn: ${usage.backend}` : '';   // trafia do textContent
  // Skrót commita w widocznym miejscu: przy zgłoszeniu błędu od razu wiadomo, czy serwer
  // ma już poprawkę, czy komunikat pochodzi ze starszego wdrożenia.
  const build = state.runtime.build ? ` · wersja ${state.runtime.build}` : '';
  $('library-sub').textContent = state.library.length
    ? `${state.library.length} ${state.library.length === 1 ? 'zbiór' : 'zbiorów'} · `
      + `${formatBytes(usage.bytes || 0)}${gdzie}${build} · kliknij „Wczytaj”, żeby wrócić do danych bez pobierania`
    : `Biblioteka jest pusta${gdzie}${build}.`;

  const warning = $('library-warning');
  warning.hidden = data.persistent !== false;
  if (!warning.hidden) {
    warning.textContent = 'Zapisane zbiory trafiają na dysk instancji, a przy wdrożeniu '
      + 'bezserwerowym znika on razem z uśpieniem serwera — traktuj tę listę jako wygodę '
      + 'w obrębie sesji, nie archiwum. Trwałą kopię pobierzesz przyciskiem „Pobierz CSV”, '
      + 'a stałe archiwum włączysz, podpinając magazyn Vercel Blob (instrukcja w DEPLOY.md).';
    warning.classList.add('hint-limit');
  }

  // Same nagłówki nad pustką tylko myliłyby — przy pustej bibliotece zostaje sam komunikat.
  $('library-table').hidden = state.library.length === 0;
  $('library-body').innerHTML = state.library.map((e) => {
    const biezacy = e.id === state.datasetId;
    const okres = e.first_date && e.last_date ? `${e.first_date} → ${e.last_date}` : '—';
    const nazwa = e.name || e.source || e.id;
    return `<tr>
      <td class="library-name ${biezacy ? 'library-current' : ''}" title="${escapeHtml(nazwa)}">${escapeHtml(nazwa)}</td>
      <td class="num">${e.bars ? Number(e.bars).toLocaleString('pl-PL') : '—'}</td>
      <td>${escapeHtml(okres)}</td>
      <td class="num">${e.interval_minutes ? `${e.interval_minutes} min` : '—'}</td>
      <td class="num">${formatBytes(e.bytes || 0)}</td>
      <td class="library-tools">
        <div class="library-actions">
          <button type="button" class="btn btn-ghost" data-lib="open" data-id="${e.id}">Wczytaj</button>
          <button type="button" class="btn btn-ghost" data-lib="rename" data-id="${e.id}">Nazwa</button>
          <button type="button" class="btn btn-ghost" data-lib="csv" data-id="${e.id}">Pobierz CSV</button>
          <button type="button" class="btn btn-ghost" data-lib="delete" data-id="${e.id}">Usuń</button>
        </div>
      </td>
    </tr>`;
  }).join('');
}

async function probeStorage() {
  const out = $('storage-probe-result');
  out.hidden = false;
  out.classList.remove('hint-limit');
  out.textContent = 'Sprawdzam…';

  await withBusy('btn-storage-probe', 'Sprawdzam trwałość zapisu…', async () => {
    const r = await callApi('api/storage/probe', undefined, { allowRecovery: false });
    const kroki = (r.steps || [])
      .map((k) => `${k.ok ? '✓' : '✗'} ${k.krok}${k.szczegol ? ` (${k.szczegol})` : ''}`)
      .join(' · ');
    const skad = r.token_env ? ` Token ze zmiennej ${r.token_env}.` : '';
    out.textContent = `${r.backend || 'magazyn'}: ${kroki || 'brak kroków'}.${skad} ${r.hint || ''}`.trim();
    // Zapis może działać i mimo to nie przetrwać — o kolorze decyduje trwałość, nie sam cykl.
    out.classList.toggle('hint-limit', !(r.ok && r.persistent));
  });
  refreshLibrary();
}

async function handleLibraryAction(event) {
  const button = event.target.closest('[data-lib]');
  if (!button) return;
  const { lib: action, id } = button.dataset;
  const entry = (state.library || []).find((e) => e.id === id) || {};

  if (action === 'csv') {
    window.location.href = `api/datasets/${id}/csv`;
    return;
  }

  if (action === 'open') {
    await withBusy(button, 'Wczytuję zapisany zbiór…', async () => {
      const tz = encodeURIComponent($('timezone').value);
      // zbiór jest już na serwerze, więc nie ma czego wysyłać ponownie
      state.source = { kind: 'library', id };
      onDatasetLoaded(await callApi(`api/datasets/${id}/open?timezone=${tz}`, { method: 'POST' }));
      refreshLibrary();
    });
    return;
  }

  if (action === 'rename') {
    const name = window.prompt('Nowa nazwa zbioru:', entry.name || '');
    if (name === null) return;
    await callApi(`api/datasets/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    refreshLibrary();
    return;
  }

  if (action === 'delete') {
    const ile = entry.bars ? `${Number(entry.bars).toLocaleString('pl-PL')} świec` : 'ten zbiór';
    if (!window.confirm(`Usunąć „${entry.name || id}” (${ile})? Tej operacji nie da się cofnąć.`)) return;
    await callApi(`api/datasets/${id}`, { method: 'DELETE' });
    if (id === state.datasetId) state.datasetId = null;
    refreshLibrary();
    setStatus('Zbiór usunięty z biblioteki.', 'ok');
  }
}

/* ---------------- masowe pobranie archiwum ---------------- */

const ARCHIVE_STATES = {
  pending: 'czeka',
  running: 'pobiera…',
  done: 'gotowe',
  empty: 'brak danych',
  error: 'błąd',
};

function buildArchiveInstruments(instruments) {
  // Wybór z poprzedniej wizyty. Bez tego każde odświeżenie strony zaznaczało wszystko
  // od nowa i wyglądało, jakby ustawienia pobierania w ogóle się nie zmieniały.
  const zapamietane = loadArchiveChoice();
  const host = $('archive-instruments');
  host.innerHTML = '';
  Object.entries(instruments).forEach(([code, label]) => {
    const wrap = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.value = code;
    input.className = 'archive-instrument';
    input.checked = zapamietane.instruments ? zapamietane.instruments.includes(code) : true;
    input.addEventListener('change', () => { persistArchiveChoice(); scheduleArchiveEstimate(); });
    wrap.append(input, document.createTextNode(label));
    host.append(wrap);
  });
  if (!document.querySelector('.archive-instrument:checked')) {
    // Pusty wybór nie ma jak ruszyć — zapamiętana lista mogła stracić aktualność.
    const pierwszy = document.querySelector('.archive-instrument');
    if (pierwszy) pierwszy.checked = true;
  }
  if (zapamietane.years) $('archive_years').value = zapamietane.years;
  if (zapamietane.interval_minutes) $('archive_interval').value = zapamietane.interval_minutes;
  if (zapamietane.price) $('archive_price').value = zapamietane.price;
}

function archiveChoice() {
  return {
    instruments: [...document.querySelectorAll('.archive-instrument:checked')].map((i) => i.value),
    years: Number($('archive_years').value) || 10,
    interval_minutes: Number($('archive_interval').value),
    price: $('archive_price').value,
  };
}

function loadArchiveChoice() {
  try {
    return JSON.parse(localStorage.getItem(ARCHIVE_KEY)) || {};
  } catch {
    return {};
  }
}

function persistArchiveChoice() {
  try {
    localStorage.setItem(ARCHIVE_KEY, JSON.stringify(archiveChoice()));
  } catch {
    // Prywatny tryb przeglądarki potrafi odmówić zapisu; to nie powód, żeby cokolwiek psuć.
  }
}

// Numer ostatniego pytania o oszacowanie. Każda zmiana pola wysyła osobne żądanie, a te
// wracają w dowolnej kolejności — bez tego licznika odpowiedź na nieaktualne pytanie
// nadpisywała świeższą i linijka pokazywała coś innego, niż stało w formularzu.
let szacunekNr = 0;
let szacunekTimer = null;

/** Wpisywanie „10" to trzy zdarzenia i trzy żądania — czekamy, aż palce znieruchomieją. */
function scheduleArchiveEstimate() {
  clearTimeout(szacunekTimer);
  szacunekTimer = setTimeout(refreshArchiveEstimate, 250);
}

async function refreshArchiveEstimate() {
  const nr = (szacunekNr += 1);
  const wybor = archiveChoice();
  const out = $('archive-estimate');
  if (!wybor.instruments.length) {
    out.textContent = 'Zaznacz przynajmniej jeden instrument.';
    return;
  }
  const zapytanie = `instruments=${wybor.instruments.join(',')}`
    + `&years=${wybor.years}&interval_minutes=${wybor.interval_minutes}`;
  let e;
  try {
    e = await callApi(`api/archive/estimate?${zapytanie}`, undefined, { allowRecovery: false });
  } catch {
    return;                      // oszacowanie to podpowiedź, jego brak nie blokuje pobierania
  }
  if (nr !== szacunekNr) return;   // formularz zdążył się zmienić — ta odpowiedź jest już nieaktualna
  // Prędkości łącza nie da się zgadnąć, więc czas podajemy widełkami zamiast udawać precyzję.
  out.textContent = `${e.date_from} → ${e.date_to} · `
    + `${e.instruments} ${e.instruments === 1 ? 'instrument' : 'instrumentów'} · `
    + `${Number(e.files_total).toLocaleString('pl-PL')} plików godzinowych · `
    + `~${formatBytes(e.bytes_total)} danych · `
    + `orientacyjnie ${formatDuration(e.seconds_fast)}–${formatDuration(e.seconds_slow)}.`;
}

function formatDuration(sekundy) {
  if (sekundy < 90) return `${Math.round(sekundy)} s`;
  if (sekundy < 5400) return `${Math.round(sekundy / 60)} min`;
  return `${(sekundy / 3600).toFixed(1)} h`;
}

async function startArchive() {
  const wybor = archiveChoice();
  if (!wybor.instruments.length) {
    setStatus('Zaznacz przynajmniej jeden instrument do pobrania.', 'error');
    return;
  }
  persistArchiveChoice();

  // Trwający plan nie może zamieniać przycisku w ślepy zaułek. Pytamy wprost i zastępujemy —
  // inaczej zmiana instrumentów czy liczby lat nie miałaby jak wejść w życie.
  const trwa = state.archivePlan && state.archivePlan.state === 'running';
  const lata = `${wybor.years} ${wybor.years === 1 ? 'rok' : 'lat'}`;
  const pytanie = trwa
    ? `Pobieranie archiwum już trwa (${archiveDone()}). Przerwać je i zacząć nowe: `
      + `${lata} historii dla ${wybor.instruments.length} instrumentów?\n\n`
      + 'Instrumenty domknięte wcześniej zostają w bibliotece, a pobrane godziny w pamięci '
      + 'podręcznej — nic z dotychczasowej pracy nie przepada.'
    : `Pobrać ${lata} historii dla ${wybor.instruments.length} instrumentów?\n\n`
      + 'Może to potrwać od kilkunastu minut do kilku godzin. Postęp zapisuje się na bieżąco, '
      + 'więc przerwanie niczego nie kasuje — kolejne uruchomienie ruszy od tego samego miejsca.';
  if (!window.confirm(pytanie)) return;

  let ruszylo = false;
  state.archiveDriving = true;          // od tej chwili panel wie, że pobieranie ma iść
  await withBusy('btn-archive-start', 'Zakładam plan pobierania…', async () => {
    const dane = await callApi('api/archive/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...wybor, replace: true }),
    }, { allowRecovery: false });
    renderArchive(dane);
    ruszylo = dane.plan && dane.plan.state === 'running';
  });
  state.archiveDriving = ruszylo;
  if (ruszylo) runArchiveLoop();
}

function archiveDone() {
  const p = state.archiveProgress || {};
  return p.days_total
    ? `${Math.round((p.fraction || 0) * 100)}% zrobione`
    : 'w trakcie';
}

async function resumeArchive() {
  let dane;
  try {
    dane = await callApi('api/archive/status', undefined, { allowRecovery: false });
  } catch {
    return;
  }
  if (!dane.plan) return;
  renderArchive(dane);
  if (dane.plan.state !== 'running') return;   // zakończony plan zostaje do wglądu, nie rozwija panelu
  $('archive-panel').open = true;
  setStatus('Wznawiam przerwane pobieranie archiwum.', 'ok');
  runArchiveLoop();
}

async function runArchiveLoop() {
  if (state.archiveRunning) return;         // dwie pętle deptałyby sobie po krokach
  state.archiveRunning = state.archiveDriving = true;
  // Przycisk startu zostaje czynny: trwające pobieranie nie może odbierać możliwości
  // zmiany instrumentów. Kliknięcie w trakcie zastąpi plan, nie zawiśnie na odmowie.
  odswiezPrzyciskArchiwum();
  try {
    for (;;) {
      const dane = await callApi('api/archive/step', { method: 'POST' }, { allowRecovery: false });
      renderArchive(dane);
      refreshLibrary();                     // gotowe instrumenty mają być widać od razu
      if (!dane.plan || dane.plan.state !== 'running') {
        setStatus(archiveSummary(dane), dane.plan && dane.plan.state === 'done' ? 'ok' : '');
        break;
      }
    }
  } catch (err) {
    setStatus('Pobieranie archiwum przerwane: ' + err.message
      + ' Postęp jest zapisany — kliknij „Wznów”, żeby ruszyć dalej.', 'error');
  } finally {
    state.archiveRunning = state.archiveDriving = false;
    renderArchive({ plan: state.archivePlan, progress: state.archiveProgress });
  }
}

/** Etykieta przycisku startu mówi, co się stanie po kliknięciu. */
function odswiezPrzyciskArchiwum() {
  const trwa = state.archivePlan && state.archivePlan.state === 'running';
  $('btn-archive-start').textContent = trwa && state.archiveDriving
    ? 'Zacznij od nowa' : 'Pobierz do biblioteki';
}

/** Wznawia plan, który został „w trakcie", ale nikt go już nie posuwa. */
function resumeArchiveLoop() {
  $('archive-panel').open = true;
  setStatus('Wznawiam pobieranie archiwum.', 'ok');
  runArchiveLoop();
}

function archiveSummary(dane) {
  const plan = dane.plan || {};
  const gotowe = (plan.instruments || []).filter((p) => p.state === 'done').length;
  const bledy = (plan.instruments || []).filter((p) => p.state === 'error').length;
  if (plan.state === 'cancelled') {
    return `Pobieranie przerwane. ${gotowe} instrumentów zdążyło trafić do biblioteki.`;
  }
  return `Archiwum pobrane: ${gotowe} z ${(plan.instruments || []).length} instrumentów`
    + (bledy ? `, ${bledy} nieudanych — szczegóły w tabeli poniżej.` : '.');
}

async function retryArchive() {
  let ruszylo = false;
  state.archiveDriving = true;
  await withBusy('btn-archive-retry', 'Wznawiam nieudane instrumenty…', async () => {
    const dane = await callApi('api/archive/retry', { method: 'POST' }, { allowRecovery: false });
    renderArchive(dane);
    ruszylo = dane.plan && dane.plan.state === 'running';
  });
  state.archiveDriving = ruszylo;
  if (ruszylo) runArchiveLoop();
}

async function cancelArchive() {
  // Bieżący odcinek dobiegnie końca — przerwanie działa między odcinkami, nie w ich środku.
  await withBusy('btn-archive-cancel', 'Przerywam pobieranie — kończę bieżący odcinek…', async () => {
    renderArchive(await callApi('api/archive/cancel', { method: 'POST' }, { allowRecovery: false }));
  });
}

function renderArchive(dane) {
  const plan = dane && dane.plan;
  const postep = (dane && dane.progress) || {};
  // Zapamiętany plan mówi przyciskom, co się stanie po kliknięciu — bez tego przycisk
  // startu nie wiedział, że coś już trwa, i zwracał odmowę zamiast zapytać.
  state.archivePlan = plan || null;
  state.archiveProgress = postep;
  $('archive-table-wrap').hidden = !plan;
  $('archive-progress').hidden = !plan || plan.state !== 'running';
  odswiezPrzyciskArchiwum();
  if (!plan) return;

  $('archive-fill').style.width = `${Math.round((postep.fraction || 0) * 100)}%`;
  const zostalo = postep.seconds_left ? ` · zostało ~${formatDuration(postep.seconds_left)}` : '';
  $('archive-text').textContent =
    `${postep.instruments_done}/${postep.instruments_total} instrumentów · `
    + `${Number(postep.days_done).toLocaleString('pl-PL')} z `
    + `${Number(postep.days_total).toLocaleString('pl-PL')} dni${zostalo}`;

  // Plan bywa „w trakcie", choć nikt go już nie posuwa — kroki idą z przeglądarki, więc
  // zerwane połączenie albo zamknięta karta zostawiają zamrożony pasek. Zamiast udawać,
  // że coś się dzieje, mówimy wprost i dajemy czym ruszyć dalej.
  const zamrozone = plan.state === 'running' && !state.archiveDriving;
  $('btn-archive-resume').hidden = !zamrozone;
  $('btn-archive-cancel').hidden = zamrozone;
  if (zamrozone) $('archive-text').textContent += ' · wstrzymane';

  // Zakończony plan zostaje na ekranie razem z komunikatami — łatwo wziąć je za świeżą awarię.
  // Mówimy więc wprost, że to zapis poprzedniego podejścia, i dajemy przycisk powtórki obok.
  const zakonczony = plan.state !== 'running';
  const nieudane = (plan.instruments || []).filter((p) => p.state === 'error').length;
  $('archive-note-row').hidden = !(zakonczony && nieudane);
  if (zakonczony && nieudane) {
    $('archive-note').textContent = `Poprzednie pobranie zakończyło się błędem na `
      + `${nieudane} ${nieudane === 1 ? 'instrumencie' : 'instrumentach'} — poniżej jego zapis, `
      + 'nie bieżący stan. „Ponów nieudane” wznawia je od miejsca, w którym stanęły, '
      + 'bez ruszania tych, które trafiły już do biblioteki.';
  }

  const przerwane = plan.state === 'cancelled';
  $('archive-body').innerHTML = (plan.instruments || []).map((p) => {
    const udzial = p.days_total ? Math.round((p.days_done / p.days_total) * 100) : 0;
    // Nic już nie „pobiera" ani nie „czeka", gdy plan stoi — pokazywanie tego wprowadzałoby
    // w błąd tak samo po przerwaniu, jak przy pobieraniu, którego nikt nie posuwa.
    const wisi = p.state === 'running' || p.state === 'pending';
    const stan = wisi && (przerwane || zamrozone)
      ? (przerwane ? 'przerwane' : 'wstrzymane') : (ARCHIVE_STATES[p.state] || p.state);
    return `<tr>
      <td>${escapeHtml(p.label)}</td>
      <td class="num">${udzial}%</td>
      <td class="num">${p.bars ? Number(p.bars).toLocaleString('pl-PL') : '—'}${
        p.skipped_days ? `<span class="hint-limit"> · −${p.skipped_days} `
          + `${odmiana(p.skipped_days, 'doba', 'doby', 'dób')}</span>` : ''}</td>
      <td>${escapeHtml(stan)}${p.note ? ` — ${escapeHtml(p.note)}` : ''}</td>
    </tr>`;
  }).join('');
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

// Sama godzina wystarczy — data stoi w pierwszej kolumnie i powtarzanie jej tylko rozpycha tabelę.
function godzina(stamp) {
  return stamp ? String(stamp).slice(11) : '';
}

// Zakres, który cena miała przebić. Bez niego nie da się porównać wiersza z wykresem:
// widać wynik, ale nie widać, od czego był liczony.
function zakresOpis(t) {
  if (!t.signal_high && !t.signal_low) return '';
  return `Świeca sygnałowa: otwarcie ${fmtPrice.format(t.signal_open)} · `
    + `szczyt ${fmtPrice.format(t.signal_high)} · dołek ${fmtPrice.format(t.signal_low)} · `
    + `zamknięcie ${fmtPrice.format(t.signal_close)} `
    + `(rozpiętość ${((t.signal_high - t.signal_low) / (state.result?.config?.pip_size || 0.0001)).toFixed(1)} pipsa)`;
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
    // Wynik rozstrzygnięty wewnątrz jednej świecy — z OHLC nie wynika kolejność zdarzeń,
    // więc taki wiersz mógłby wyglądać inaczej na danych o drobniejszej rozdzielczości.
    const niepewne = (trade) => (trade.uncertain_exit
      ? ` <abbr class="uncertain" title="Wynik rozstrzygnięty wewnątrz jednej świecy: `
        + `ta sama świeca sięgnęła i stop lossa, i take profita, albo pozycja zamknęła się `
        + `na świecy wejścia. Z OHLC nie wynika, co stało się pierwsze — zdecydowało `
        + `ustawienie „Gdy jedna świeca dotyka i SL, i TP”. Na danych 1-minutowych `
        + `ten wiersz może wyjść inaczej.">?</abbr>`
      : '');
    const dirClass = t.direction === 1 ? 'tag-long' : 'tag-short';
    const outcomeClass = t.exit_reason === 'TP' ? 'tag-tp' : (t.exit_reason === 'SL' ? 'tag-sl' : 'tag-neutral');
    const attempt = t.attempt > 1 ? ` <span class="attempt">próba ${t.attempt}</span>` : '';
    return `<tr class="trade-row" data-day="${escapeHtml(t.date)}" tabindex="0"
                title="Kliknij, żeby zobaczyć świece z tego dnia — dokładnie te, na których liczył silnik">
      <td>${escapeHtml(t.date)}</td>
      <td>${escapeHtml(t.weekday_name)}${attempt}</td>
      <td data-strategy-col="range_breakout" title="${escapeHtml(zakresOpis(t))}">${escapeHtml(t.breakout_label)}</td>
      <td><span class="tag ${dirClass}">${escapeHtml(t.direction_label)}</span></td>
      <td class="num">${escapeHtml(fmtPrice.format(t.entry_price))}<span class="cell-sub">${
        escapeHtml(godzina(t.entry_time))}</span></td>
      <td class="num">${escapeHtml(fmtPrice.format(t.stop_loss))}</td>
      <td class="num">${escapeHtml(fmtPrice.format(t.take_profit))}</td>
      <td>${escapeHtml(godzina(t.exit_time) || '—')}<span class="cell-sub">${
        escapeHtml(t.exit_price ? fmtPrice.format(t.exit_price) : '')}</span></td>
      <td><span class="tag ${outcomeClass}">${escapeHtml(t.exit_reason || '—')}</span>${niepewne(t)}</td>
      <td class="num ${toneClass(t.pnl_money)}">${escapeHtml(signed(t.pnl_money, fmtMoney))}</td>
      <td class="num ${toneClass(t.pnl_pct)}">${escapeHtml(signed(t.pnl_pct, fmtPct2, '%'))}</td>
      <td class="num ${toneClass(t.cumulative_return_pct)}">${escapeHtml(signed(t.cumulative_return_pct, fmtPct2, '%'))}</td>
      <td class="num">${t.hold_hours === null ? '—' : escapeHtml(fmtNum1.format(t.hold_hours) + ' h')}</td>
    </tr>`;
  }).join('');
}

/* ---------------- podgląd świec z jednego dnia ---------------- */

// Rozstrzyga spory, których z samej tabeli wyników rozstrzygnąć się nie da: „na moim wykresie
// o tej godzinie nie było takiego poziomu". Pokazuje surowe świece, na których liczył silnik,
// z godziną w obu strefach — bo najczęstszą przyczyną rozjazdu jest właśnie strefa czasowa.
async function togglePodgladSwiec(wiersz) {
  const nastepny = wiersz.nextElementSibling;
  if (nastepny && nastepny.classList.contains('candles-row')) {
    nastepny.remove();
    return;
  }
  document.querySelectorAll('.candles-row').forEach((el) => el.remove());

  const dzien = wiersz.dataset.day;
  const kolumn = wiersz.children.length;
  const miejsce = document.createElement('tr');
  miejsce.className = 'candles-row';
  miejsce.innerHTML = `<td colspan="${kolumn}">Wczytuję świece z ${escapeHtml(dzien)}…</td>`;
  wiersz.after(miejsce);

  let dane;
  try {
    const tz = encodeURIComponent($('timezone').value);
    dane = await callApi(
      `api/datasets/${state.datasetId}/candles?date=${encodeURIComponent(dzien)}&timezone=${tz}`);
  } catch (err) {
    miejsce.innerHTML = `<td colspan="${kolumn}">Nie udało się wczytać świec: ${escapeHtml(err.message)}</td>`;
    return;
  }

  const sygnal = $('signal_time').value || '08:00';
  const wiersze = dane.candles.map((c) => {
    const wSygnale = c.time_local >= sygnal
      && c.time_local < dodajMinuty(sygnal, Number($('candle_minutes').value) || 15);
    return `<tr class="${wSygnale ? 'candle-signal' : ''}">
      <td>${escapeHtml(c.time_local)}</td>
      <td class="muted">${escapeHtml(c.time_utc)}</td>
      <td class="num">${escapeHtml(fmtPrice.format(c.open))}</td>
      <td class="num">${escapeHtml(fmtPrice.format(c.high))}</td>
      <td class="num">${escapeHtml(fmtPrice.format(c.low))}</td>
      <td class="num">${escapeHtml(fmtPrice.format(c.close))}</td>
    </tr>`;
  }).join('');

  miejsce.innerHTML = `<td colspan="${kolumn}">
    <div class="candles-box">
      <p class="hint">Świece z ${escapeHtml(dzien)} — dokładnie te, na których liczył silnik.
        Podświetlone to okno świecy sygnałowej. Kolumna „UTC” pokazuje tę samą świecę w czasie
        uniwersalnym: jeśli Twój wykres zgadza się z nią, a nie z pierwszą kolumną, to znaczy,
        że strefa czasowa w sekcji 1 jest ustawiona inaczej niż na wykresie.</p>
      <div class="table-scroll">
        <table class="data-table candles-table">
          <thead><tr>
            <th>${escapeHtml(dane.timezone)}</th><th>UTC</th>
            <th class="num">Otwarcie</th><th class="num">Szczyt</th>
            <th class="num">Dołek</th><th class="num">Zamknięcie</th>
          </tr></thead>
          <tbody>${wiersze}</tbody>
        </table>
      </div>
      ${dane.truncated ? '<p class="hint hint-limit">Pokazano początek dnia — świec było więcej.</p>' : ''}
    </div>
  </td>`;
}

function dodajMinuty(hhmm, minuty) {
  const [g, m] = hhmm.split(':').map(Number);
  const suma = g * 60 + m + minuty;
  return `${String(Math.floor(suma / 60) % 24).padStart(2, '0')}:${String(suma % 60).padStart(2, '0')}`;
}

function exportCsv() {
  const header = ['data', 'dzien_tygodnia', 'wybicie', 'proba', 'kierunek',
    'swieca_otwarcie', 'swieca_szczyt', 'swieca_dolek', 'swieca_zamkniecie',
    'wejscie_czas', 'wejscie', 'stop_loss', 'take_profit', 'wyjscie_czas', 'wyjscie',
    'wynik', 'wynik_niepewny', 'zysk_strata', 'procent',
    'narastajaco_procent', 'godziny', 'status', 'powod'];
  const lines = [header.join(',')];

  for (const t of visibleTrades()) {
    const played = t.status === 'closed' || t.status === 'open';
    lines.push([
      t.date,
      t.weekday_name,
      t.breakout_side || '',
      t.attempt,
      played ? t.direction_label : '',
      t.signal_open.toFixed(5),
      t.signal_high.toFixed(5),
      t.signal_low.toFixed(5),
      t.signal_close.toFixed(5),
      t.entry_time || '',
      played ? t.entry_price.toFixed(5) : '',
      played ? t.stop_loss.toFixed(5) : '',
      played ? t.take_profit.toFixed(5) : '',
      t.exit_time || '',
      played ? t.exit_price.toFixed(5) : '',
      t.exit_reason || '',
      t.uncertain_exit ? 'tak' : '',
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
