"use strict";

const state = {
  scenarios: [],
  current: null,
  hasKey: false,
  overrides: {},      // { sessionLabel: { model } }
  columns: new Map(), // label -> DOM refs + лента диалога
  source: null,
  running: false,
  judge: null,        // блок «Вердикт» текущего прогона
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
    list.innerHTML = '<p class="empty-hint">Сценариев нет — не нашлось ни одного файла day-*/scenario.py</p>';
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
  // Переключение сценария обрывает текущий прогон. Иначе старый EventSource
  // остаётся открытым и продолжает слать события, а колонки он ищет по label —
  // совпавший label нового сценария принял бы чужой текст.
  stopRun();
  state.current = sc;
  state.overrides = {};
  document.querySelectorAll("#scenario-list li").forEach((li) => {
    li.classList.toggle("active", li.dataset.id === id);
  });
  renderScenarioBar(sc);
  renderColumns(sc.sessions, sc.layout);
  resetVerdict();
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
    box.innerHTML = '<span class="hint">каталог моделей недоступен — берём модели из сценария</span>';
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
  ["tokens_out", "сгенерировано", fmtNum],
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

// Тело сообщения строим через textContent: промпт печатается как есть,
// без интерпретации разметки. {{depends_on}} подсвечиваем отдельным span.
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

    // Подпись к панели: при серии метрики в ней относятся к конкретному
    // прогону, и это должно быть написано, а не подразумеваться.
    const statsNote = document.createElement("div");
    statsNote.className = "stats-note hidden";

    // Доля уникальных ответов — то, ради чего серия и делается.
    const uniq = document.createElement("div");
    uniq.className = "uniq hidden";

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
      statsNote,
      uniq,
      status,
      dependsOn: s.depends_on || null,
      base: (s.messages || []).map((m) => ({ role: m.role, content: m.content })),
      turns: [],          // всё, что добавилось после промпта сценария
      answer: null,       // тело ответа сценария, в него стримятся delta
      repeatsTotal: 1,    // длина серии: приходит в session_start
      repeats: new Map(), // индекс прогона -> тело его блока
      texts: [],          // тексты прогонов серии — из них считается уникальность
      lastMetrics: null,
      busy: false,
    };

    // Лента скроллится, метрики и поле ввода остаются на месте.
    col.append(head, chat, uniq, statsNote, stats, status, buildComposer(entry));
    box.appendChild(col);

    renderPrompt(entry, entry.base, false);
    state.columns.set(s.label, entry);
  });
}

// Ответ сценария. При repeats=1 это одно сообщение assistant на колонку,
// при серии — по сообщению на прогон с подписью «прогон N из M».
function answerBlock(col, repeat) {
  if (repeat === undefined || repeat === null) {
    if (!col.answer) col.answer = appendMessage(col, "assistant", "");
    return col.answer;
  }
  if (!col.repeats.has(repeat)) {
    col.repeats.set(
      repeat,
      appendMessage(col, "assistant", "", `assistant · прогон ${repeat + 1} из ${col.repeatsTotal}`)
    );
  }
  return col.repeats.get(repeat);
}

// Метрики прогона дописываются под его же ответом и больше не меняются:
// панель внизу показывает текущий прогон, а прошлые остаются в ленте.
function repeatMetricsLine(body, metrics) {
  if (!metrics) return;
  const parts = [];
  if (metrics.ttft_ms !== null && metrics.ttft_ms !== undefined) parts.push("TTFT " + fmtMs(metrics.ttft_ms));
  if (metrics.tokens_per_second) parts.push(metrics.tokens_per_second.toFixed(1) + " ток/с");
  if (metrics.completion_tokens || metrics.tokens_out) parts.push((metrics.completion_tokens || metrics.tokens_out) + " токенов");
  if (metrics.cost_usd !== null && metrics.cost_usd !== undefined) parts.push(fmtCost(metrics.cost_usd));
  if (metrics.finish_reason) parts.push(metrics.finish_reason);
  if (!parts.length) return;
  const line = document.createElement("div");
  line.className = "repeat-metrics";
  line.textContent = parts.join("  ·  ");
  body.parentElement.appendChild(line);
}

// «уникальных ответов: N из M» — счётчик растёт по ходу серии.
function updateUniq(col) {
  if (col.repeatsTotal <= 1 || !col.texts.length) return;
  const unique = new Set(col.texts.map((t) => t.trim())).size;
  col.uniq.classList.remove("hidden");
  col.uniq.textContent =
    `уникальных ответов: ${unique} из ${col.texts.length}` +
    ` (${Math.round((100 * unique) / col.texts.length)} %)`;
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
  const question = { role: "user", content: text };
  col.turns.push(question);
  const questionBox = appendMessage(col, "user", text).parentElement;

  const body = appendMessage(col, "assistant", "");
  setBusy(col, true);
  setStatus(col, "", "генерация…");

  // Обмен не состоялся: вопрос нельзя оставлять ни в ленте, ни в col.turns —
  // иначе он уйдёт в модель ещё раз, вторым user-сообщением подряд, а в кадре
  // этого не видно. Текст возвращается в поле ввода, чтобы можно было повторить.
  const rollback = () => {
    const i = col.turns.lastIndexOf(question);
    if (i >= 0) col.turns.splice(i, 1);
    questionBox.remove();
    body.parentElement.remove();
    if (!col.input.value) {
      col.input.value = text;
      autoGrow(col.input);
    }
  };

  let answer = "";
  let failed = false;
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
          failed = true;
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
  } catch (err) {
    failed = true;
    setStatus(col, "error", String(err.message || err));
  }

  if (answer) {
    // Ответ есть — он часть диалога, даже если поток оборвался на середине.
    col.turns.push({ role: "assistant", content: answer });
    if (!col.status.classList.contains("error")) setStatus(col, "", "готово");
  } else if (failed) {
    rollback();
  }

  setBusy(col, false);
  scrollChat(col);
}

// --- блок «Вердикт»: ответ модели-судьи на вопросы задания дня ---

// Судья вызывается только у сценариев с judge_questions, поэтому блока
// может не быть вовсе — тогда его просто не показываем.
function resetVerdict() {
  state.judge = null;
  const box = $("#verdict");
  box.className = "verdict hidden";
  box.innerHTML = "";
}

function verdictBox(modelLine) {
  const box = $("#verdict");
  box.className = "verdict";
  box.innerHTML = `<header><h3>Вердикт</h3><div class="judge-model"></div>
    <div class="judge-conflict hidden"></div></header>
    <div class="verdict-body"></div><div class="verdict-status"></div>`;
  box.querySelector(".judge-model").textContent = modelLine;
  return {
    root: box,
    model: box.querySelector(".judge-model"),
    conflict: box.querySelector(".judge-conflict"),
    body: box.querySelector(".verdict-body"),
    status: box.querySelector(".verdict-status"),
    text: "",
    cost: null,
  };
}

function verdictStatus(kind, text) {
  if (!state.judge) return;
  state.judge.status.className = "verdict-status" + (kind ? " " + kind : "");
  state.judge.status.textContent = text;
}

function startVerdict(e) {
  const judge = verdictBox("судит " + e.model);
  // Пока судья молчит, в кадре должно быть видно, что он работает,
  // а не пустой блок.
  judge.root.classList.add("busy");
  if (e.conflicts && e.conflicts.length) {
    judge.conflict.classList.remove("hidden");
    judge.conflict.textContent =
      "судья совпал с моделью колонки " + e.conflicts.map((c) => `«${c}»`).join(", ");
  }
  state.judge = judge;
  verdictStatus("", "судья читает ответы колонок…");
}

function appendVerdict(text) {
  if (!state.judge) return;
  state.judge.text += text;
  state.judge.body.textContent = state.judge.text;
  verdictStatus("", "судья пишет…");
}

function finishVerdict(e) {
  if (!state.judge) return;
  state.judge.root.classList.remove("busy");
  if (e.text) {
    state.judge.text = e.text;
    state.judge.body.textContent = e.text;
  }
  if (e.metrics && e.metrics.cost_usd !== null && e.metrics.cost_usd !== undefined) {
    state.judge.cost = e.metrics.cost_usd;
  }
  verdictStatus("", "готово");
}

// Вердикт — надстройка над прогоном: его ошибка не трогает ни колонки,
// ни сводку, только сам блок.
function failVerdict(message) {
  if (!state.judge) state.judge = verdictBox("судья");
  state.judge.root.classList.remove("busy");
  verdictStatus("error", message);
}

function skipVerdict(message) {
  state.judge = verdictBox("судья не вызывался");
  verdictStatus("", message);
}

// --- прогон сценария ---

// Кнопка «Старт» снова рабочая, колонки больше не «генерируют».
function resetRunUi() {
  state.running = false;
  const btn = $("#start-btn");
  if (btn) {
    btn.disabled = false;
    btn.textContent = "Старт";
  }
  state.columns.forEach((col) => setBusy(col, false));
}

// Обрывает активный прогон и забывает поток: всё, что придёт по нему после
// этого, уже не относится к тому, что на экране.
function stopRun() {
  if (state.source) {
    state.source.close();
    state.source = null;
  }
  resetRunUi();
}

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
  resetVerdict();
  $("#summary").classList.add("hidden");
  state.columns.forEach((col) => setBusy(col, true));

  const totals = { cost: 0, tokens: 0, done: 0, expected: sessions.length };
  // Ход прогона по колонкам: нужен, чтобы при обрыве объяснить происходящее
  // именно тем колонкам, которые всё ещё чего-то ждут.
  const started = new Set();   // пришёл session_start
  const settled = new Set();   // пришёл session_done или session_error
  const startedAt = performance.now();
  let gotEvent = false;

  const finish = () => {
    state.source = null;
    resetRunUi();
  };

  const qs = Object.keys(state.overrides).length
    ? "?overrides=" + encodeURIComponent(JSON.stringify(state.overrides))
    : "";
  const source = new EventSource(`/api/run/${state.current.id}${qs}`);
  state.source = source;

  // Поток принадлежит тому сценарию, на котором его запустили. Если он больше
  // не текущий — его успели оборвать, и всё пришедшее по нему отбрасываем.
  const isCurrent = () => state.source === source;

  // Обрыв потока EventSource сообщает без причины и без текста. Молчать здесь
  // нельзя: колонка так и осталась бы в «генерация…», а на записи это
  // неотличимо от медленной модели. Объясняем обрыв каждой колонке, которая
  // его не дождалась, и подводим итог по тому, что успело досчитаться.
  const abort = () => {
    const reason = gotEvent
      ? "соединение со стендом оборвано"
      : "стенд недоступен";
    state.columns.forEach((col, label) => {
      if (settled.has(label)) return;
      setStatus(col, "error", started.has(label)
        ? `${reason} — ответ не дописан`
        : `${reason} — колонка не запускалась`);
    });
    finish();
    if (totals.done) renderSummary(totals, performance.now() - startedAt, true);
  };

  source.onmessage = (ev) => {
    if (!isCurrent()) {
      source.close();
      return;
    }
    const e = JSON.parse(ev.data);
    gotEvent = true;
    const col = e.session ? state.columns.get(e.session) : null;

    switch (e.event) {
      case "session_waiting":
        if (col) setStatus(col, "waiting", `ждёт вывод колонки «${e.on}»`);
        break;
      case "session_start":
        started.add(e.session);
        if (col) {
          col.repeatsTotal = e.repeats || 1;
          setStatus(col, "", col.repeatsTotal > 1
            ? `генерация… прогон 1 из ${col.repeatsTotal}`
            : "генерация…");
          // Для колонки с depends_on это первый момент, когда известен
          // итоговый промпт: заменяем предварительный текст на него.
          if (e.resolved_messages) {
            col.base = e.resolved_messages.map((m) => ({ role: m.role, content: m.content }));
            renderPrompt(col, col.base, true);
          }
        }
        break;
      case "repeat_start":
        if (col) {
          col.repeatsTotal = e.repeats || col.repeatsTotal;
          answerBlock(col, e.repeat);
          setStatus(col, "", `генерация… прогон ${e.repeat + 1} из ${col.repeatsTotal}`);
          // Панель метрик подписана прогоном: видно, к чему относятся цифры.
          col.statsNote.classList.remove("hidden");
          col.statsNote.textContent = `метрики прогона ${e.repeat + 1} из ${col.repeatsTotal}`;
        }
        break;
      case "delta":
        if (col) {
          answerBlock(col, e.repeat).textContent += e.text;
          applyMetrics(col, e.metrics);
          scrollChat(col);
        }
        break;
      case "metrics":
        if (col) applyMetrics(col, e.metrics);
        break;
      case "repeat_done":
        if (col) {
          applyMetrics(col, e.metrics);
          repeatMetricsLine(answerBlock(col, e.repeat), e.metrics);
          col.texts.push(e.text || "");
          updateUniq(col);
          // Сумма по прогону складывается здесь: session_done у серии несёт
          // метрики последнего прогона и второй раз их считать нельзя.
          if (e.metrics) {
            if (e.metrics.cost_usd) totals.cost += e.metrics.cost_usd;
            if (e.metrics.total_tokens) totals.tokens += e.metrics.total_tokens;
          }
          scrollChat(col);
        }
        break;
      case "repeat_error":
        // Падение одного прогона не хоронит колонку: серия идёт дальше.
        if (col) {
          const body = answerBlock(col, e.repeat);
          body.classList.add("failed");
          body.textContent = e.message;
          applyMetrics(col, e.metrics);
          scrollChat(col);
        }
        break;
      case "session_error":
        settled.add(e.session);
        if (col) {
          setStatus(col, "error", e.message);
          applyMetrics(col, e.metrics);
        }
        break;
      case "session_done":
        settled.add(e.session);
        if (col) {
          applyMetrics(col, e.metrics);
          // У серии суммы уже сложены по repeat_done — иначе последний
          // прогон посчитался бы дважды.
          if (e.metrics && !(e.repeats > 1)) {
            if (e.metrics.cost_usd) totals.cost += e.metrics.cost_usd;
            if (e.metrics.total_tokens) totals.tokens += e.metrics.total_tokens;
          }
          if (e.repeats > 1) {
            // Серия, из которой не выжил ни один прогон, — это провал колонки,
            // а не «готово»: ответов ноль, и строка состояния обязана это
            // сказать, иначе она противоречит красным блокам в ленте.
            if (!(e.texts || []).length) {
              setStatus(col, "error", `ни один прогон не удался — 0 из ${e.repeats}`);
              col.statsNote.textContent = "метрик удачных прогонов нет";
            } else {
              col.statsNote.textContent = `метрики последнего прогона из ${e.repeats}`;
              updateUniq(col);
            }
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
      case "judge_start":
        startVerdict(e);
        break;
      case "judge_delta":
        appendVerdict(e.text);
        break;
      case "judge_done":
        finishVerdict(e);
        break;
      case "judge_error":
        failVerdict(e.message);
        break;
      case "judge_skipped":
        skipVerdict(e.message);
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
    if (isCurrent()) abort();
  };
}

// Сравнение колонок имеет смысл только когда колонок больше одной: «самая
// быстрая» на единственной колонке сравнивать не с чем. На прерванном прогоне
// сравнения нет вовсе — часть колонок не отработала, и победитель среди
// уцелевших сказал бы неправду. Остаются итоги по тому, что успело досчитаться.
function renderSummary(totals, wallClockMs, interrupted) {
  const rows = [
    ["суммарная стоимость", fmtCost(totals.cost)],
    ["суммарно токенов", String(totals.tokens)],
    [interrupted ? "wall-clock до обрыва" : "wall-clock", fmtMs(wallClockMs)],
  ];
  if (interrupted) {
    rows.unshift(["прогон", `прерван · ${totals.done} из ${totals.expected} колонок`]);
  }
  // Отдельной строкой: видно, во что обошёлся вердикт, и это не смешано
  // с суммой по колонкам.
  if (state.judge && state.judge.cost !== null && state.judge.cost !== undefined) {
    rows.push(["стоимость судьи", fmtCost(state.judge.cost)]);
  }

  if (!interrupted && state.columns.size > 1) {
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

// --- сворачивание сайдбара ---

// Во время записи список сценариев нужен только в момент выбора: дальше это
// ширина, которой не хватает колонкам. Состояние переживает перезагрузку —
// после `--reload` ведущему не приходится сворачивать заново.
const SIDEBAR_KEY = "ui.sidebar.collapsed";

function readCollapsed() {
  try {
    return localStorage.getItem(SIDEBAR_KEY) === "1";
  } catch (e) {
    return false;   // приватный режим или запрет на хранилище — не повод падать
  }
}

function applySidebar(collapsed) {
  const btn = $("#sidebar-toggle");
  $("#sidebar").classList.toggle("collapsed", collapsed);
  btn.textContent = collapsed ? "›" : "‹";
  btn.title = collapsed ? "Показать сценарии" : "Свернуть сценарии";
  btn.setAttribute("aria-label", btn.title);
  btn.setAttribute("aria-expanded", String(!collapsed));
}

function initSidebar() {
  applySidebar(readCollapsed());
  $("#sidebar-toggle").onclick = () => {
    const collapsed = !$("#sidebar").classList.contains("collapsed");
    applySidebar(collapsed);
    try {
      localStorage.setItem(SIDEBAR_KEY, collapsed ? "1" : "0");
    } catch (e) {
      /* не сохранилось — свернуть всё равно можно, просто забудется */
    }
  };
}

initSidebar();
loadScenarios();
