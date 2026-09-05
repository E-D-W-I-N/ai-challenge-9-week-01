"use strict";

const state = {
  scenarios: [],
  current: null,
  overrides: {},      // { sessionLabel: { model } }
  columns: new Map(), // label -> DOM refs
  source: null,
  runStarted: 0,
};

const $ = (sel) => document.querySelector(sel);

function fmtMs(v) {
  if (v === null || v === undefined) return "—";
  return v >= 1000 ? (v / 1000).toFixed(2) + " с" : Math.round(v) + " мс";
}
function fmtCost(v) {
  if (v === null || v === undefined) return "—";
  return "$" + Number(v).toFixed(6);
}
function fmtNum(v) {
  return v === null || v === undefined ? "—" : String(v);
}

async function loadScenarios() {
  const res = await fetch("/api/scenarios");
  const data = await res.json();
  state.scenarios = data.scenarios;

  const badge = $("#key-status");
  badge.textContent = data.has_key ? "OPENROUTER_API_KEY найден" : "нет .env с OPENROUTER_API_KEY";
  badge.className = "badge " + (data.has_key ? "ok" : "bad");

  renderSidebar(data);

  const errBox = $("#registry-errors");
  const errs = Object.entries(data.errors || {});
  errBox.textContent = errs.length
    ? errs.map(([k, v]) => `${k}:\n${v}`).join("\n\n")
    : "";
}

// Сайдбар группирует сценарии по дням: заголовок дня, под ним его сценарии
// в порядке из SCENARIOS. Дни приходят с бэка уже отсортированными по номеру.
function renderSidebar(data) {
  const list = $("#scenario-list");
  list.innerHTML = "";
  if (!data.scenarios.length) {
    list.innerHTML = '<p class="empty-hint">Сценариев нет — ни одной day-*/scenario.py</p>';
    return;
  }

  const byDay = new Map();
  data.scenarios.forEach((sc) => {
    const key = sc.day || "";
    if (!byDay.has(key)) byDay.set(key, { title: sc.day_title || sc.day || "Сценарии", items: [] });
    byDay.get(key).items.push(sc);
  });

  byDay.forEach((group) => {
    const box = document.createElement("div");
    box.className = "day-group";

    const head = document.createElement("div");
    head.className = "day-title";
    head.textContent = group.title;

    const ul = document.createElement("ul");
    ul.className = "day-scenarios";
    group.items.forEach((sc) => {
      const li = document.createElement("li");
      li.innerHTML = `${sc.title}<span class="sid">${sc.id}</span>`;
      li.onclick = () => selectScenario(sc.id);
      li.dataset.id = sc.id;
      ul.appendChild(li);
    });

    box.append(head, ul);
    list.appendChild(box);
  });
}

function selectScenario(id) {
  const sc = state.scenarios.find((s) => s.id === id);
  if (!sc) return;
  state.current = sc;
  state.overrides = {};
  document.querySelectorAll("#scenario-list li").forEach((li) => {
    li.classList.toggle("active", li.dataset.id === id);
  });
  renderBriefing(sc);
  renderColumns(sc.sessions, sc.layout);
  $("#summary").classList.add("hidden");
}

function renderBriefing(sc) {
  const box = $("#briefing");
  box.classList.remove("empty");
  box.innerHTML = `
    <h2>${sc.title}</h2>
    <p>${sc.description}</p>
    <p class="watch"><strong>На что смотреть:</strong> ${sc.watch_for}</p>
    <div class="controls">
      <button class="start" id="start-btn">Старт</button>
      <div id="pickers" style="display:flex;gap:10px;flex-wrap:wrap"></div>
    </div>`;
  $("#start-btn").onclick = startRun;
  buildPickers(sc);
}

// Дропдаун модели на каждую колонку. Каталог ключа не требует —
// живой ещё до того, как пользователь создаст .env.
async function buildPickers(sc) {
  const box = $("#pickers");
  const needsTemperature = sc.sessions.some((s) => s.temperature !== null && s.temperature !== undefined);
  const hotTemperature = sc.sessions.some((s) => (s.temperature ?? 0) > 1.0);
  const params = new URLSearchParams();
  const requires = [];
  if (needsTemperature) requires.push("temperature");
  if (sc.sessions.some((s) => s.stop && s.stop.length)) requires.push("stop");
  if (sc.sessions.some((s) => s.response_format)) requires.push("response_format");
  if (requires.length) params.set("requires", requires.join(","));
  // День 5 сравнивает стоимость: :free и :batch уже отброшены на бэке, но
  // бесплатные варианты убираем и здесь.
  params.set("exclude_free", "true");
  // День 4: anthropic/* обрезает температуру на 1.0 и вернёт 400 на 1.2.
  if (hotTemperature) params.set("exclude_temperature_capped", "true");

  let models = [];
  try {
    const res = await fetch("/api/models?" + params.toString());
    models = (await res.json()).models || [];
  } catch (e) {
    box.innerHTML = '<span class="hint">каталог моделей недоступен, используются модели из сценария</span>';
    return;
  }

  box.innerHTML = "";
  sc.sessions.forEach((s) => {
    const wrap = document.createElement("label");
    wrap.className = "model-picker";
    const sel = document.createElement("select");
    const options = models.some((m) => m.id === s.model)
      ? models
      : [{ id: s.model, name: s.model + " (из сценария)", prompt_price_per_m: 0, completion_price_per_m: 0 }, ...models];
    options.forEach((m) => {
      const opt = document.createElement("option");
      opt.value = m.id;
      const price = m.prompt_price_per_m
        ? `  ·  $${m.prompt_price_per_m}/$${m.completion_price_per_m} за 1M`
        : "";
      opt.textContent = m.id + price;
      if (m.id === s.model) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.onchange = () => {
      state.overrides[s.label] = { ...(state.overrides[s.label] || {}), model: sel.value };
      const col = state.columns.get(s.label);
      if (col) col.modelId.textContent = sel.value;
    };
    wrap.append(document.createTextNode(s.label), sel);
    box.appendChild(wrap);
  });
}

const STAT_FIELDS = [
  ["ttft_ms", "TTFT", fmtMs],
  ["tokens_per_second", "ток/с", (v) => (v ? v.toFixed(1) : "—")],
  ["tokens_out", "сген. токенов", fmtNum],
  ["elapsed_ms", "прошло", fmtMs],
  ["prompt_tokens", "prompt", fmtNum],
  ["completion_tokens", "completion", fmtNum],
  ["total_tokens", "total", fmtNum],
  ["reasoning_tokens", "reasoning", fmtNum],
  ["cost_usd", "стоимость", fmtCost],
  ["finish_reason", "finish_reason", (v) => v || "—"],
  ["provider", "провайдер", (v) => v || "—"],
  ["context_fill_pct", "контекст", (v) => (v === null || v === undefined ? "—" : v.toFixed(1) + " %")],
];

const PLACEHOLDER = "{{depends_on}}";

// Тело сообщения строится через textContent: промпт печатается как есть,
// без интерпретации разметки. {{depends_on}} подсвечивается отдельным span.
function messageBody(content) {
  const body = document.createElement("div");
  body.className = "body";
  const text = typeof content === "string" ? content : JSON.stringify(content);
  if (!text.includes(PLACEHOLDER)) {
    body.textContent = text;
    return body;
  }
  text.split(PLACEHOLDER).forEach((part, i) => {
    if (i) {
      const ph = document.createElement("span");
      ph.className = "ph";
      ph.textContent = PLACEHOLDER;
      body.appendChild(ph);
    }
    body.appendChild(document.createTextNode(part));
  });
  return body;
}

// system сворачивается по клику, но открыт по умолчанию: зритель должен
// видеть, что инструкция есть и что в ней написано.
function promptMessage(m) {
  const role = String(m.role || "?").toLowerCase();
  if (role === "system") {
    const el = document.createElement("details");
    el.className = "msg system";
    el.open = true;
    const summary = document.createElement("summary");
    summary.textContent = "system";
    el.append(summary, messageBody(m.content));
    return el;
  }
  const el = document.createElement("div");
  el.className = "msg " + (role === "assistant" ? "assistant" : "user");
  const head = document.createElement("div");
  head.className = "role";
  head.textContent = role;
  el.append(head, messageBody(m.content));
  return el;
}

function renderPrompt(col, messages, resolved) {
  col.promptBox.innerHTML = "";
  (messages || []).forEach((m) => col.promptBox.appendChild(promptMessage(m)));
  if (!col.dependsOn) return;

  const hint = document.createElement("div");
  hint.className = "dep-hint";
  if (resolved) {
    hint.textContent = `промпт после подстановки вывода колонки «${col.dependsOn}»`;
  } else if ((messages || []).some((m) => typeof m.content === "string" && m.content.includes(PLACEHOLDER))) {
    hint.textContent = `${PLACEHOLDER} заменится выводом колонки «${col.dependsOn}» — итоговый промпт появится здесь на старте`;
  } else {
    hint.textContent = `колонка стартует после колонки «${col.dependsOn}»`;
  }
  col.promptBox.appendChild(hint);
}

function renderColumns(sessions, layout) {
  const box = $("#columns");
  box.className = "columns" + (layout === "single" ? " single" : "");
  box.innerHTML = "";
  state.columns.clear();

  sessions.forEach((s) => {
    const col = document.createElement("div");
    col.className = "column";

    const head = document.createElement("header");
    head.innerHTML = `<h3>${s.label}</h3>
      <div class="modelid">${s.model}</div>
      ${s.note ? `<div class="note">${s.note}</div>` : ""}`;

    // Лента: промпт сверху (виден до «Старта»), ответы дописываются под ним.
    const chat = document.createElement("div");
    chat.className = "chat";
    const promptBox = document.createElement("div");
    promptBox.className = "prompt";
    chat.appendChild(promptBox);

    const stats = document.createElement("div");
    stats.className = "stats";
    const values = {};
    STAT_FIELDS.forEach(([key, title]) => {
      const row = document.createElement("div");
      row.className = "stat";
      row.innerHTML = `<span class="k">${title}</span><span class="v">—</span>`;
      values[key] = row.querySelector(".v");
      stats.appendChild(row);
    });

    const uniq = document.createElement("div");
    uniq.className = "uniq hidden";

    const status = document.createElement("div");
    status.className = "status";
    status.textContent = "ожидание";

    // Статистика под лентой: чат сверху, метрики внизу.
    col.append(head, chat, uniq, stats, status);
    box.appendChild(col);

    const entry = {
      root: col,
      modelId: head.querySelector(".modelid"),
      values,
      chat,
      promptBox,
      uniq,
      status,
      dependsOn: s.depends_on || null,
      repeats: new Map(),
      texts: [],
      lastMetrics: null,
    };
    renderPrompt(entry, s.messages, false);
    state.columns.set(s.label, entry);
  });
}

function answerBlock(col, index, total) {
  if (col.repeats.has(index)) return col.repeats.get(index);
  const el = document.createElement("div");
  el.className = "msg assistant";
  const head = document.createElement("div");
  head.className = "role";
  head.textContent = total > 1 ? `assistant · прогон ${index + 1} из ${total}` : "assistant";
  const body = document.createElement("div");
  body.className = "body";
  el.append(head, body);
  col.chat.appendChild(el);
  col.repeats.set(index, body);
  return body;
}

function applyMetrics(col, metrics) {
  col.lastMetrics = metrics;
  STAT_FIELDS.forEach(([key, , fmt]) => {
    const el = col.values[key];
    if (!el) return;
    el.textContent = fmt(metrics[key]);
    el.classList.toggle("length", key === "finish_reason" && metrics[key] === "length");
    el.classList.toggle("err", key === "finish_reason" && metrics.error);
  });
}

function startRun() {
  if (!state.current) return;
  const btn = $("#start-btn");
  btn.disabled = true;
  btn.textContent = "идёт прогон…";

  const sessions = state.current.sessions.map((s) => ({
    ...s,
    ...(state.overrides[s.label] || {}),
  }));
  renderColumns(sessions, state.current.layout);
  $("#summary").classList.add("hidden");
  state.runStarted = performance.now();

  const totals = { cost: 0, tokens: 0, done: 0, expected: sessions.length };

  const qs = Object.keys(state.overrides).length
    ? "?overrides=" + encodeURIComponent(JSON.stringify(state.overrides))
    : "";
  const source = new EventSource(`/api/run/${state.current.id}${qs}`);
  state.source = source;

  source.onmessage = (ev) => {
    const e = JSON.parse(ev.data);
    const col = e.session ? state.columns.get(e.session) : null;

    switch (e.event) {
      case "session_waiting":
        if (col) {
          col.status.className = "status waiting";
          col.status.textContent = `ждёт вывод колонки «${e.on}»`;
        }
        break;
      case "session_start":
        if (col) {
          col.status.className = "status";
          col.status.textContent = "генерация…";
          col.repeatsTotal = e.repeats;
          // Для колонки с depends_on это первый момент, когда известен
          // итоговый промпт: заменяем предварительный текст на него.
          if (e.resolved_messages) renderPrompt(col, e.resolved_messages, true);
        }
        break;
      case "repeat_start":
        if (col) answerBlock(col, e.repeat, col.repeatsTotal || 1);
        break;
      case "delta":
        if (col) {
          answerBlock(col, e.repeat, col.repeatsTotal || 1).textContent += e.text;
          applyMetrics(col, e.metrics);
          col.chat.scrollTop = col.chat.scrollHeight;
        }
        break;
      case "metrics":
        if (col) applyMetrics(col, e.metrics);
        break;
      case "repeat_done":
        if (col) {
          applyMetrics(col, e.metrics);
          col.texts.push((e.text || "").trim());
          if (e.metrics.cost_usd) totals.cost += e.metrics.cost_usd;
          if (e.metrics.total_tokens) totals.tokens += e.metrics.total_tokens;
          if ((col.repeatsTotal || 1) > 1) {
            const uniqueCount = new Set(col.texts).size;
            col.uniq.classList.remove("hidden");
            col.uniq.textContent =
              `уникальных ответов: ${uniqueCount} из ${col.texts.length}` +
              ` (${Math.round((100 * uniqueCount) / col.texts.length)} %)`;
          }
        }
        break;
      case "repeat_error":
      case "session_error":
        if (col) {
          col.status.className = "status error";
          col.status.textContent = e.message;
          if (e.metrics) applyMetrics(col, e.metrics);
        }
        break;
      case "session_done":
        if (col) {
          col.status.className = "status";
          col.status.textContent = "готово";
        }
        totals.done += 1;
        break;
      case "run_done":
        source.close();
        btn.disabled = false;
        btn.textContent = "Старт";
        renderSummary(totals, e.wall_clock_ms);
        break;
    }
  };

  source.onerror = () => {
    source.close();
    btn.disabled = false;
    btn.textContent = "Старт";
  };
}

function renderSummary(totals, wallClockMs) {
  const cols = [...state.columns.entries()]
    .map(([label, c]) => ({ label, m: c.lastMetrics }))
    .filter((x) => x.m && !x.m.error);

  const fastest = cols
    .filter((x) => x.m.ttft_ms !== null)
    .sort((a, b) => a.m.ttft_ms - b.m.ttft_ms)[0];
  const cheapest = cols
    .filter((x) => x.m.cost_usd !== null && x.m.cost_usd !== undefined)
    .sort((a, b) => a.m.cost_usd - b.m.cost_usd)[0];

  const box = $("#summary");
  box.classList.remove("hidden");
  box.innerHTML = [
    ["суммарная стоимость", fmtCost(totals.cost)],
    ["суммарно токенов", String(totals.tokens)],
    ["wall-clock", fmtMs(wallClockMs)],
    ["самая быстрая", fastest ? `${fastest.label} · ${fmtMs(fastest.m.ttft_ms)}` : "—"],
    ["самая дешёвая", cheapest ? `${cheapest.label} · ${fmtCost(cheapest.m.cost_usd)}` : "—"],
  ]
    .map(([k, v]) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`)
    .join("");
}

loadScenarios();
