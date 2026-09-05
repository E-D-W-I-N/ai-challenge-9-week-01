"use strict";

const state = {
  scenarios: [],
  current: null,
  hasKey: false,
  overrides: {},      // { sessionLabel: { model } }
  columns: new Map(), // label -> DOM refs + лента диалога
  source: null,
  running: false,
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
  state.hasKey = !!data.has_key;

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
  renderScenarioBar(sc);
  renderColumns(sc.sessions, sc.layout);
  $("#summary").classList.add("hidden");
}

// Шапка сценария — одна компактная строка: название, модели, «Старт».
// description и watch_for живут в сворачиваемом блоке, закрытом по умолчанию.
function renderScenarioBar(sc) {
  const bar = $("#scenario-bar");
  bar.innerHTML = `
    <h2 class="scenario-title">${sc.title}</h2>
    <div id="pickers" class="pickers"></div>
    <button class="ghost" id="about-btn" aria-expanded="false">О сценарии</button>
    <button class="start" id="start-btn">Старт</button>`;

  const about = $("#about");
  about.classList.add("hidden");
  about.innerHTML = `
    <p>${sc.description}</p>
    <p class="watch"><strong>На что смотреть:</strong> ${sc.watch_for}</p>`;

  const aboutBtn = $("#about-btn");
  aboutBtn.onclick = () => {
    const open = !about.classList.toggle("hidden");
    aboutBtn.setAttribute("aria-expanded", String(open));
    aboutBtn.classList.toggle("active", open);
  };
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
    if (sc.sessions.length > 1) {
      const name = document.createElement("span");
      name.className = "picker-label";
      name.textContent = s.label;
      wrap.appendChild(name);
    }
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
    wrap.appendChild(sel);
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

function scrollChat(col) {
  col.chat.scrollTop = col.chat.scrollHeight;
}

// Сообщение, дописанное после промпта сценария: ручной вопрос или ответ модели.
function appendMessage(col, role, text, caption) {
  const el = document.createElement("div");
  el.className = "msg " + role;
  const head = document.createElement("div");
  head.className = "role";
  head.textContent = caption || role;
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = text || "";
  el.append(head, body);
  col.chat.appendChild(el);
  scrollChat(col);
  return body;
}

function autoGrow(input) {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
}

function setStatus(col, kind, text) {
  col.status.className = "status" + (kind ? " " + kind : "");
  col.status.textContent = text;
}

function setBusy(col, busy) {
  col.busy = busy;
  col.root.classList.toggle("busy", busy);
  if (!col.input) return;
  col.input.disabled = busy || !state.hasKey;
  col.sendBtn.disabled = busy || !state.hasKey;
}

function buildComposer(col) {
  const form = document.createElement("form");
  form.className = "composer";

  const input = document.createElement("textarea");
  input.className = "composer-input";
  input.rows = 1;
  const send = document.createElement("button");
  send.type = "submit";
  send.className = "send";
  send.textContent = "Отправить";

  input.title = "Enter — отправить, Shift+Enter — перенос строки";
  if (state.hasKey) {
    input.placeholder = "Спросить модель…";
  } else {
    // Без ключа поле недоступно, но видно, чего не хватает.
    input.placeholder = "Нужен .env с OPENROUTER_API_KEY";
    input.disabled = true;
    send.disabled = true;
    form.classList.add("locked");
  }

  input.addEventListener("input", () => autoGrow(input));
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      form.requestSubmit();
    }
  });
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    sendManual(col);
  });

  form.append(input, send);
  col.input = input;
  col.sendBtn = send;
  return form;
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

    // Лента: промпт сверху (виден до «Старта»), ответы и ручные вопросы — под ним.
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

    const status = document.createElement("div");
    status.className = "status";
    status.textContent = "ожидание";

    const entry = {
      label: s.label,
      session: s,
      root: col,
      modelId: head.querySelector(".modelid"),
      values,
      chat,
      promptBox,
      status,
      dependsOn: s.depends_on || null,
      base: (s.messages || []).map((m) => ({ role: m.role, content: m.content })),
      turns: [],          // всё, что добавилось после промпта сценария
      answer: null,       // тело ответа сценария, в него стримятся delta
      lastMetrics: null,
      busy: false,
    };

    // Лента скроллится, метрики и поле ввода остаются на месте.
    col.append(head, chat, stats, status, buildComposer(entry));
    box.appendChild(col);

    renderPrompt(entry, entry.base, false);
    state.columns.set(s.label, entry);
  });
}

// Ответ сценария — одно сообщение assistant на колонку.
function answerBlock(col) {
  if (!col.answer) col.answer = appendMessage(col, "assistant", "");
  return col.answer;
}

function applyMetrics(col, metrics) {
  if (!metrics) return;
  col.lastMetrics = metrics;
  STAT_FIELDS.forEach(([key, , fmt]) => {
    const el = col.values[key];
    if (!el) return;
    el.textContent = fmt(metrics[key]);
    el.classList.toggle("length", key === "finish_reason" && metrics[key] === "length");
    el.classList.toggle("err", key === "finish_reason" && metrics.error);
  });
}

// --- ручной ввод: диалог продолжается всей накопленной лентой колонки ---

function columnModel(col) {
  return (state.overrides[col.label] || {}).model || col.session.model;
}

function columnPayload(col) {
  const s = col.session;
  return {
    label: col.label,
    model: columnModel(col),
    messages: col.base.concat(col.turns),
    temperature: s.temperature ?? null,
    max_tokens: s.max_tokens ?? null,
    stop: s.stop ?? null,
    response_format: s.response_format ?? null,
    extra_body: s.extra_body || {},
  };
}

// SSE поверх POST: тело запроса — вся лента колонки, в GET-строку она не влезет.
async function streamChat(payload, onEvent) {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body && body.detail) {
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      }
    } catch (e) { /* тело не JSON — остаётся код статуса */ }
    throw new Error(detail);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      frame.split("\n").forEach((line) => {
        if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)));
      });
    }
  }
}

async function sendManual(col) {
  const text = (col.input.value || "").trim();
  if (!text || col.busy || !state.hasKey) return;

  col.input.value = "";
  autoGrow(col.input);
  // Вопрос уходит в модель вместе со всей лентой: диалог продолжается.
  col.turns.push({ role: "user", content: text });
  appendMessage(col, "user", text);

  const body = appendMessage(col, "assistant", "");
  setBusy(col, true);
  setStatus(col, "", "генерация…");

  let answer = "";
  try {
    await streamChat(columnPayload(col), (e) => {
      switch (e.event) {
        case "delta":
          answer += e.text;
          body.textContent = answer;
          applyMetrics(col, e.metrics);
          scrollChat(col);
          break;
        case "metrics":
          applyMetrics(col, e.metrics);
          break;
        case "error":
          applyMetrics(col, e.metrics);
          setStatus(col, "error", e.message);
          break;
        case "done":
          applyMetrics(col, e.metrics);
          answer = e.text || answer;
          body.textContent = answer;
          break;
      }
    });
    if (answer) {
      col.turns.push({ role: "assistant", content: answer });
      if (!col.status.classList.contains("error")) setStatus(col, "", "готово");
    }
  } catch (err) {
    setStatus(col, "error", String(err.message || err));
  } finally {
    setBusy(col, false);
    scrollChat(col);
  }
}

// --- прогон сценария ---

function startRun() {
  if (!state.current || state.running) return;
  const btn = $("#start-btn");
  btn.disabled = true;
  btn.textContent = "идёт прогон…";
  state.running = true;

  const sessions = state.current.sessions.map((s) => ({
    ...s,
    ...(state.overrides[s.label] || {}),
  }));
  renderColumns(sessions, state.current.layout);
  $("#summary").classList.add("hidden");
  state.columns.forEach((col) => setBusy(col, true));

  const totals = { cost: 0, tokens: 0, done: 0, expected: sessions.length };

  const finish = () => {
    state.running = false;
    btn.disabled = false;
    btn.textContent = "Старт";
    state.columns.forEach((col) => setBusy(col, false));
  };

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
        if (col) setStatus(col, "waiting", `ждёт вывод колонки «${e.on}»`);
        break;
      case "session_start":
        if (col) {
          setStatus(col, "", "генерация…");
          // Для колонки с depends_on это первый момент, когда известен
          // итоговый промпт: заменяем предварительный текст на него.
          if (e.resolved_messages) {
            col.base = e.resolved_messages.map((m) => ({ role: m.role, content: m.content }));
            renderPrompt(col, col.base, true);
          }
        }
        break;
      case "delta":
        if (col) {
          answerBlock(col).textContent += e.text;
          applyMetrics(col, e.metrics);
          scrollChat(col);
        }
        break;
      case "metrics":
        if (col) applyMetrics(col, e.metrics);
        break;
      case "session_error":
        if (col) {
          setStatus(col, "error", e.message);
          applyMetrics(col, e.metrics);
        }
        break;
      case "session_done":
        if (col) {
          applyMetrics(col, e.metrics);
          if (e.metrics) {
            if (e.metrics.cost_usd) totals.cost += e.metrics.cost_usd;
            if (e.metrics.total_tokens) totals.tokens += e.metrics.total_tokens;
          }
          if (!col.status.classList.contains("error")) setStatus(col, "", "готово");
          // Ответ сценария становится частью диалога: следующий ручной
          // вопрос уйдёт в модель вместе с ним.
          const answer = (e.text || "").trim();
          if (answer) col.turns.push({ role: "assistant", content: answer });
          setBusy(col, false);
        }
        totals.done += 1;
        break;
      case "run_done":
        source.close();
        finish();
        renderSummary(totals, e.wall_clock_ms);
        break;
    }
  };

  source.onerror = () => {
    source.close();
    finish();
  };
}

// Сравнение колонок имеет смысл только когда колонок больше одной:
// «самая быстрая» на единственной колонке сравнивать не с чем.
function renderSummary(totals, wallClockMs) {
  const rows = [
    ["суммарная стоимость", fmtCost(totals.cost)],
    ["суммарно токенов", String(totals.tokens)],
    ["wall-clock", fmtMs(wallClockMs)],
  ];

  if (state.columns.size > 1) {
    const cols = [...state.columns.entries()]
      .map(([label, c]) => ({ label, m: c.lastMetrics }))
      .filter((x) => x.m && !x.m.error);
    const fastest = cols
      .filter((x) => x.m.ttft_ms !== null && x.m.ttft_ms !== undefined)
      .sort((a, b) => a.m.ttft_ms - b.m.ttft_ms)[0];
    const cheapest = cols
      .filter((x) => x.m.cost_usd !== null && x.m.cost_usd !== undefined)
      .sort((a, b) => a.m.cost_usd - b.m.cost_usd)[0];
    if (fastest) rows.push(["самая быстрая", `${fastest.label} · ${fmtMs(fastest.m.ttft_ms)}`]);
    if (cheapest) rows.push(["самая дешёвая", `${cheapest.label} · ${fmtCost(cheapest.m.cost_usd)}`]);
  }

  const box = $("#summary");
  box.classList.remove("hidden");
  box.innerHTML = rows
    .map(([k, v]) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`)
    .join("");
}

loadScenarios();
