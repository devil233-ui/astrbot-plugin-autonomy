/**
 * 自主性面板前端。
 *
 * 通过 AstrBot 插件视图 bridge 与后端通信（endpoint 用相对名，宿主会拼成
 * /api/v1/plugins/extensions/astrbot_plugin_autonomy/<endpoint>）：
 *   GET  state          读取当前快照
 *   POST configure      保存唤醒方式 / 她安排的唤醒开关
 *   POST wake           现在判断一次
 *   POST stop           结束并取消排队
 *   POST self-cancel    取消她自己安排的唤醒
 *
 * 数据流与 WhaleLife 面板一致：后端快照是唯一真相，表单只是它的编辑副本；
 * 用户改过（dirty）时轮询不覆盖输入，保存成功后再以新快照回填。
 */

const bridge =
  window.AstrBotPluginView || window.AstrBotPluginPage || null;

const POLL_MS = 5000;

const state = {
  snapshot: null,
  umo: "",
  dirty: false,
  busy: false,
};

const TRIGGER_LABEL = {
  random: "随机唤醒",
  self: "自己安排的唤醒",
  manual: "手动判断",
  agreement: "约定的唤醒",
};

const STATUS_LABEL = {
  queued: "排队中",
  running: "正在判断",
  completed: "已结束",
  failed: "出错了",
  interrupted: "被叫停",
  cancelled: "已取消",
  settling: "收尾中",
  uncertain: "结果不确定",
  busy: "忙碌跳过",
};

const DECISION_LABEL = {
  acted: "说了点什么",
  no_action: "这轮不动",
};

const INTENT_LABEL = {
  open: "进行中",
  done: "已完成",
  abandoned: "已放弃",
};

const $ = (id) => document.getElementById(id);

function requireBridge() {
  if (!bridge) {
    throw new Error("请在 AstrBot 面板中打开这个页面");
  }
}

async function apiGet(endpoint, params) {
  requireBridge();
  const response = await bridge.apiGet(endpoint, params || {});
  return unwrap(response);
}

async function apiPost(endpoint, body) {
  requireBridge();
  const response = await bridge.apiPost(endpoint, body || {});
  return unwrap(response);
}

function unwrap(response) {
  if (response && response.success === false) {
    throw new Error(response.error || "请求失败");
  }
  if (response && response.data !== undefined) {
    return response.data;
  }
  return response;
}

// ---------- 时间与文案 ----------
function pad(value) {
  return String(value).padStart(2, "0");
}

function formatStamp(raw) {
  if (!raw) return "—";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return String(raw);
  const now = new Date();
  const sameDay =
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate();
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  if (sameDay) return clock;
  return `${date.getMonth() + 1}/${date.getDate()} ${clock}`;
}

function formatStampFull(raw) {
  if (!raw) return "—";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return String(raw);
  return (
    `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

function formatPercent(fraction) {
  if (fraction === null || fraction === undefined || Number.isNaN(fraction)) {
    return "—";
  }
  return `${(Number(fraction) * 100).toFixed(1)}%`;
}

function describeRun(run) {
  const status = STATUS_LABEL[run.status] || run.status || "未知状态";
  const trigger = TRIGGER_LABEL[run.trigger] || run.trigger || "";
  const decision = run.decision ? DECISION_LABEL[run.decision] || run.decision : "";
  const when = formatStamp(run.ended_at || run.started_at);
  return {
    head: [status, trigger, decision, when].filter(Boolean).join(" · "),
    reason: (run.reason || "").trim(),
    status,
  };
}

// ---------- 渲染 ----------
function setBadge(snapshot) {
  const badge = $("state-badge");
  if (!snapshot) {
    badge.textContent = "读取中";
    badge.dataset.tone = "off";
    return;
  }
  if (!snapshot.settings.enabled) {
    badge.textContent = "自动唤醒已暂停";
    badge.dataset.tone = "off";
    return;
  }
  if (snapshot.running) {
    badge.textContent = "正在自主判断";
    badge.dataset.tone = "busy";
    return;
  }
  if (snapshot.queued > 0) {
    badge.textContent = "排队中";
    badge.dataset.tone = "busy";
    return;
  }
  if (snapshot.sleeping) {
    badge.textContent = "普通唤醒休息中";
    badge.dataset.tone = "sleep";
    return;
  }
  badge.textContent = "等待唤醒";
  badge.dataset.tone = "idle";
}

function fillSessions(snapshot) {
  const select = $("f-session");
  const sessions = snapshot.sessions || [];
  const wanted = state.umo || snapshot.current || "";
  const signature = sessions.join("|");
  if (select.dataset.signature !== signature) {
    select.innerHTML = "";
    sessions.forEach((umo) => {
      const option = document.createElement("option");
      option.value = umo;
      option.textContent = umo;
      select.appendChild(option);
    });
    if (!sessions.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "（配置里还没有会话）";
      select.appendChild(option);
    }
    select.dataset.signature = signature;
  }
  if (wanted) {
    select.value = wanted;
  }
}

function fillForm(snapshot) {
  const settings = snapshot.settings || {};
  if (!state.dirty) {
    $("f-enabled").checked = Boolean(settings.enabled);
    $("f-mode").value = settings.mode || "adaptive";
    $("f-interval").value = settings.interval_minutes ?? 30;
    $("f-probability").value = Math.round((Number(settings.probability) || 0) * 1000) / 10;
    $("f-timezone").value = settings.timezone || "";
    $("f-recent").value = settings.recent_minutes ?? 20;
    $("f-recent-mul").value = settings.recent_multiplier ?? 0.15;
    $("f-distant").value = settings.distant_minutes ?? 360;
    $("f-distant-mul").value = settings.distant_multiplier ?? 1.5;
    $("f-sleep").value = settings.sleep_windows || "";
    $("f-motivation").value = settings.motivation || "";
    $("f-owner").value = settings.owner_id || "";
    $("f-allow-self").checked = Boolean(settings.allow_self_wake);
    $("f-self-sleep").checked = Boolean(settings.self_wake_during_sleep);
  }
  const interval = Number(settings.interval_minutes) || 30;
  $("hint-interval").textContent =
    `每次随机等待 ${Math.round(interval * 0.5)} ~ ${Math.round(interval * 1.5)} 分钟，` +
    `平均 ${interval} 分钟检查一次。`;
}

function fillStatus(snapshot) {
  $("s-probability").textContent = formatPercent(snapshot.probability);
  $("s-activity").textContent = formatStamp(snapshot.last_activity_at);
  $("s-next").textContent = formatStamp(snapshot.next_check_at);
  $("s-queue").textContent = `${snapshot.queued || 0} 个 · ${snapshot.running ? "正在判断" : "未在判断"}`;

  const selfWake = snapshot.self_wake;
  if (selfWake && selfWake.at) {
    $("s-self-wake").textContent = formatStampFull(selfWake.at);
    $("s-self-note").textContent = selfWake.note
      ? `第 ${selfWake.version} 版：${selfWake.note}`
      : `第 ${selfWake.version} 版`;
  } else {
    $("s-self-wake").textContent = "尚未安排";
    $("s-self-note").textContent = "";
  }

  $("foot-now").textContent = `现在 ${formatStampFull(snapshot.now)}`;
  $("foot-tz").textContent = snapshot.timezone || "";
}

function fillIntents(snapshot) {
  const list = $("intent-list");
  const items = snapshot.intents || [];
  list.innerHTML = "";
  items.forEach((intent) => {
    const li = document.createElement("li");
    const tag = document.createElement("span");
    tag.className = "tag";
    if (intent.kind === "agreement") tag.classList.add("agreement");
    if (intent.state !== "open") tag.classList.add("done");
    tag.textContent =
      intent.kind === "agreement" ? "约定" : "念头";
    if (intent.state !== "open") {
      tag.textContent += ` · ${INTENT_LABEL[intent.state] || intent.state}`;
    }
    const text = document.createElement("span");
    text.className = "intent-text";
    text.textContent = intent.text || "";
    li.appendChild(tag);
    li.appendChild(text);
    const when = intent.due_at || intent.preferred_at;
    if (when) {
      const stamp = document.createElement("span");
      stamp.className = "intent-when";
      stamp.textContent = formatStampFull(when);
      li.appendChild(stamp);
    }
    list.appendChild(li);
  });
  $("intent-empty").style.display = items.length ? "none" : "";
}

function fillRuns(snapshot) {
  const list = $("run-list");
  const runs = snapshot.runs || [];
  list.innerHTML = "";
  runs.forEach((run) => {
    const li = document.createElement("li");
    const head = document.createElement("div");
    head.className = "run-head";
    const { head: headText, reason } = describeRun(run);
    const strong = document.createElement("strong");
    strong.textContent = STATUS_LABEL[run.status] || run.status || "";
    head.appendChild(strong);
    const rest = headText.slice(strong.textContent.length).replace(/^\s*·\s*/, "");
    if (rest) {
      head.appendChild(document.createTextNode(` · ${rest}`));
    }
    li.appendChild(head);
    if (reason) {
      const p = document.createElement("p");
      p.className = "run-reason";
      p.textContent = reason;
      li.appendChild(p);
    }
    list.appendChild(li);
  });
  $("run-empty").style.display = runs.length ? "none" : "";
}

function render() {
  const snapshot = state.snapshot;
  setBadge(snapshot);
  if (!snapshot) return;
  fillSessions(snapshot);
  fillForm(snapshot);
  fillStatus(snapshot);
  fillIntents(snapshot);
  fillRuns(snapshot);
}

// ---------- 请求 ----------
async function refresh(options) {
  const params = {};
  const umo = state.umo || (state.snapshot && state.snapshot.current) || "";
  if (umo) params.umo = umo;
  try {
    const snapshot = await apiGet("state", params);
    state.snapshot = snapshot;
    if (!state.umo) state.umo = snapshot.current || "";
    render();
    if (options && options.silent !== true) {
      state.dirty = false;
    }
  } catch (error) {
    const badge = $("state-badge");
    badge.textContent = "读取失败";
    badge.dataset.tone = "off";
    console.error("[autonomy] 读取状态失败", error);
  }
}

function collectPatch() {
  return {
    enabled: $("f-enabled").checked,
    mode: $("f-mode").value,
    interval_minutes: Number($("f-interval").value),
    probability: Number($("f-probability").value) / 100,
    timezone: $("f-timezone").value.trim(),
    recent_minutes: Number($("f-recent").value),
    recent_multiplier: Number($("f-recent-mul").value),
    distant_minutes: Number($("f-distant").value),
    distant_multiplier: Number($("f-distant-mul").value),
    sleep_windows: $("f-sleep").value.trim(),
    motivation: $("f-motivation").value.trim(),
    owner_id: $("f-owner").value.trim(),
    allow_self_wake: $("f-allow-self").checked,
    self_wake_during_sleep: $("f-self-sleep").checked,
  };
}

function note(el, text, tone) {
  el.textContent = text;
  el.className = "save-note" + (tone ? ` ${tone}` : "");
}

async function save() {
  if (state.busy) return;
  state.busy = true;
  $("btn-save").disabled = true;
  note($("save-note"), "保存中…");
  try {
    await apiPost("configure", collectPatch());
    state.dirty = false;
    note($("save-note"), "已保存", "ok");
    await refresh({ silent: true });
  } catch (error) {
    note($("save-note"), String(error.message || error), "err");
  } finally {
    state.busy = false;
    $("btn-save").disabled = false;
  }
}

async function wakeNow() {
  if (state.busy) return;
  state.busy = true;
  const button = $("btn-wake");
  button.disabled = true;
  try {
    await apiPost("wake", { umo: state.umo });
    await refresh({ silent: true });
  } catch (error) {
    note($("save-note"), String(error.message || error), "err");
  } finally {
    state.busy = false;
    button.disabled = false;
  }
}

async function stopAll() {
  if (state.busy) return;
  state.busy = true;
  const button = $("btn-stop");
  button.disabled = true;
  try {
    const result = await apiPost("stop", { umo: state.umo });
    const jobs = (result && result.cancelled_jobs) || [];
    const stopped = (result && result.stopped_events) || 0;
    note(
      $("save-note"),
      `已清掉 ${jobs.length} 个排队任务，叫停 ${stopped} 个进行中的轮次`,
      "ok",
    );
    await refresh({ silent: true });
  } catch (error) {
    note($("save-note"), String(error.message || error), "err");
  } finally {
    state.busy = false;
    button.disabled = false;
  }
}

async function cancelSelfWake() {
  try {
    await apiPost("self-cancel", { umo: state.umo });
    note($("self-note"), "已取消", "ok");
    await refresh({ silent: true });
  } catch (error) {
    note($("self-note"), String(error.message || error), "err");
  }
}

// ---------- 事件 ----------
function bind() {
  ["f-enabled", "f-mode", "f-interval", "f-probability", "f-timezone", "f-recent",
   "f-recent-mul", "f-distant", "f-distant-mul", "f-sleep", "f-motivation", "f-owner",
   "f-allow-self", "f-self-sleep"].forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("input", () => {
      state.dirty = true;
      note($("save-note"), "有未保存的改动");
    });
    el.addEventListener("change", () => {
      state.dirty = true;
    });
  });

  $("f-session").addEventListener("change", (event) => {
    state.umo = event.target.value;
    refresh();
  });

  $("btn-save").addEventListener("click", save);
  $("btn-reload").addEventListener("click", () => {
    state.dirty = false;
    note($("save-note"), "已放弃改动");
    render();
  });
  $("btn-wake").addEventListener("click", wakeNow);
  $("btn-stop").addEventListener("click", stopAll);
  $("btn-self-cancel").addEventListener("click", cancelSelfWake);
}

async function main() {
  bind();
  if (bridge && typeof bridge.ready === "function") {
    try {
      await bridge.ready();
    } catch (error) {
      console.warn("[autonomy] bridge ready 失败", error);
    }
  }
  await refresh();
  setInterval(() => {
    if (document.hidden) return;
    refresh({ silent: true });
  }, POLL_MS);
}

main();
