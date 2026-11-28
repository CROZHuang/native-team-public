"use strict";

(() => {
  const { t } = window.nativeTeamI18n;
  const source = window.nativeTeamSource;
  let teams = [];
  const tabs = ["overview", "tasks", "activity", "workflow", "quota", "memory"];
  const teamStates = { active: t("协作中"), paused: t("已暂停"), done: t("已完成"), cancelled: t("已取消") };
  const taskStates = { ready: t("待开始"), working: t("进行中"), review: t("待评审"), accepted: t("已验收"), blocked: t("受阻"), cancelled: t("已取消") };
  const decisionStates = { pending: t("待判断"), proposed: t("待回应"), disputed: t("有异议"), agreed: t("已确定"), superseded: t("已被替代") };
  const activityTypes = { decision: t("需求与决定"), review: t("交付与评审"), result: t("交付与评审"), update: t("进展更新") };
  const state = { teamId: "", tab: "overview", taskFilter: "all", activityFilter: "all", taskQuery: "", activityQuery: "", workflowMode: "graph", scenario: "normal", loading: false, error: "", moreLoading: false, readAt: null };
  const $ = (selector) => document.querySelector(selector);
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const team = () => teams.find((item) => item.id === state.teamId);
  const badge = (label, status) => `<span class="badge badge-${esc(status)}">${esc(label)}</span>`;
  const list = (items, className = "detail-list") => `<ul class="${className}">${items.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>`;
  const detailButton = (kind, id, text, className = "text-button") => `<button type="button" class="${className}" data-detail="${esc(kind)}" data-id="${esc(id)}">${esc(text)}</button>`;
  const closed = (item) => ["done", "cancelled"].includes(item.state);
  const workflow = window.nativeTeamWorkflow({ esc, badge, detailButton, date: source.date, taskStates, decisionStates, closed });
  const age = (seconds) => seconds >= 86400 ? t("{0} 天", Math.floor(seconds / 86400)) : seconds >= 3600 ? t("{0} 小时", Math.floor(seconds / 3600)) : seconds >= 60 ? t("{0} 分钟", Math.floor(seconds / 60)) : t("不到 1 分钟");
  const preference = (key, value) => {
    try {
      if (value !== undefined) localStorage.setItem(`native-team:${key}`, String(value));
      return localStorage.getItem(`native-team:${key}`);
    } catch { return null; } // Local-file previews and private browsing may deny persistence.
  };
  let refreshInterval = Number(preference("refresh")) || 0;
  if (![0, 10, 30].includes(refreshInterval)) refreshInterval = 0;
  let refreshTimer;
  let backgroundRequest = false;

  function refreshPause() {
    if (document.hidden) return t("页面不可见，已暂停");
    if ($("#detail-dialog").open) return t("查看详情中，已暂停");
    if (document.activeElement?.matches("input, textarea, select")) return t("输入中，已暂停");
    if (state.moreLoading || team()?.historyExpanded) return t("浏览历史中，已暂停；手动刷新可恢复");
    return "";
  }

  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    if (!source.live || !refreshInterval || document.hidden) return;
    refreshTimer = setTimeout(async () => {
      if (!state.loading && !refreshPause()) await refreshRecords(true, true);
      else renderStatus();
      scheduleRefresh();
    }, (state.error ? Math.max(30, refreshInterval) : refreshInterval) * 1000);
  }

  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    $("#theme-button").setAttribute("aria-pressed", String(theme === "dark"));
    $("#theme-button").textContent = theme === "dark" ? t("浅色外观") : t("深色外观");
    preference("theme", theme);
  }

  function preserveView(draw) {
    const active = document.activeElement;
    const id = active?.id;
    const attributes = ["data-team", "data-detail", "data-id", "data-task-filter", "data-activity-filter", "data-workflow-mode"];
    const selector = id ? `#${CSS.escape(id)}` : attributes.filter((name) => active?.hasAttribute(name)).map((name) => `[${name}="${CSS.escape(active.getAttribute(name))}"]`).join("");
    const selection = active?.matches('input[type="search"]') ? [active.selectionStart, active.selectionEnd] : null;
    const x = window.scrollX, y = window.scrollY;
    const directoryScroll = $("#team-list").scrollLeft;
    draw();
    if (selector) {
      const replacement = active.isConnected ? active : [...document.querySelectorAll(selector)].find((element) => element.getClientRects().length);
      replacement?.focus({ preventScroll: true });
      if (selection && replacement) replacement.setSelectionRange(...selection);
    }
    $("#team-list").scrollLeft = directoryScroll;
    window.scrollTo(x, y);
  }

  let requestVersion = 0;
  let controller;
  let pageVersion = 0;
  let pageController;
  let detailVersion = 0;
  let detailController;
  let handledRoute = location.hash;

  function cancelDetail() {
    detailVersion += 1;
    detailController?.abort();
    detailController = null;
  }

  function cancelPagination() {
    pageVersion += 1;
    pageController?.abort();
    state.moreLoading = false;
  }

  async function refreshRecords(refreshDirectory = true, background = false) {
    if (state.loading || (background && refreshPause())) return;
    cancelPagination();
    if (!background) cancelDetail();
    const version = ++requestVersion;
    controller?.abort();
    controller = new AbortController();
    const signal = controller.signal;
    state.loading = true;
    backgroundRequest = background;
    renderStatus();
    try {
      let nextTeams = teams;
      let nextId = state.teamId;
      if (refreshDirectory) {
        const listing = await source.directory(signal);
        if (version !== requestVersion) return;
        nextTeams = listing.map((entry) => {
          const old = teams.find((candidate) => candidate.id === entry.id);
          return { ...(old?.loaded ? old : entry), name: entry.name, project: entry.project, projectPath: entry.projectPath, state: entry.state, directoryAttention: entry.attention };
        });
        const [routeId, routeTab] = location.hash.slice(1).split("/");
        if (nextTeams.some((entry) => entry.id === routeId)) nextId = routeId;
        if (!nextTeams.some((entry) => entry.id === nextId)) nextId = nextTeams[0]?.id || "";
        if (!background && tabs.includes(routeTab)) state.tab = routeTab;
      }
      if (nextId) {
        const item = await source.detail(nextId, signal);
        if (version !== requestVersion) return;
        nextTeams = nextTeams.map((entry) => entry.id === item.id ? { ...item, directoryAttention: entry.directoryAttention } : entry);
      }
      // Commit the pair together. Failed or newly-interrupted refreshes leave the
      // last good view intact, including fields the user is currently reading.
      if (background && refreshPause()) return;
      teams = nextTeams;
      state.teamId = nextId;
      state.readAt = source.directoryReadAt;
      state.error = "";
      render();
    } catch (error) {
      if (version !== requestVersion) return;
      state.error = error.message || t("无法读取记录。");
    } finally {
      if (version === requestVersion) {
        state.loading = false;
        backgroundRequest = false;
        if (!team()?.loaded) renderPanel();
        renderStatus();
        scheduleRefresh();
      }
    }
  }

  function renderStatus() {
    $("#refresh-button").hidden = !source.live;
    $("#refresh-button").disabled = state.loading;
    $("#refresh-button").textContent = state.loading ? t("正在读取…") : t("刷新记录");
    $("#refresh-interval-label").hidden = !source.live;
    $("#refresh-hint").textContent = source.live && refreshInterval ? (refreshPause() || t("自动刷新 · {0} 秒", refreshInterval)) : "";
    $(".scenario-label").hidden = source.live;
    $("#sidebar-mode").textContent = source.live ? t("本机真实记录 · 只读") : t("本地设计示例");
    const stamp = team()?.read_at ?? state.readAt;
    $("#snapshot-time").textContent = source.live ? (stamp ? t("读取于 {0}", source.date(stamp)) : t("尚未成功读取")) : t("示例快照 · 2026年11月23日 10:30");
    const status = $("#data-status");
    status.classList.toggle("data-warning", Boolean(state.error));
    status.innerHTML = source.live
      ? `<span class="prototype-label">${t("真实团队 · 只读")}</span><span>${esc(state.error ? `${state.error}${stamp ? t(" 当前保留上次读取的记录。") : ""}` : state.loading ? t("正在读取本机共享记录…") : t("数据来自 Native Team；{0}。", stamp ? t("读取于 {0}", source.date(stamp)) : t("暂无团队记录")))}</span>`
      : `<span class="prototype-label">${t("设计示例")}</span><span>${t("这是虚构数据。查看真实团队，请在终端运行")} <code>native-team ui</code>${t("。")}</span>`;
  }

  function pagination(item, section) {
    const page = item.pages?.[section];
    if (!page) return "";
    const newer = !page.next && item[section].length < page.total;
    return `<div class="history-pagination"><span>${t("已加载 {0} / {1} 条{2}", item[section].length, page.total, page.next ? t("；筛选仅作用于已加载记录") : newer ? t("；另有更新记录，刷新后查看") : "")}</span>${page.next ? `<button type="button" class="outline-button" data-load-more="${section}" ${state.moreLoading ? "disabled" : ""}>${state.moreLoading ? t("正在读取…") : t("加载更早记录")}</button>` : newer ? `<button type="button" class="outline-button" data-refresh>${t("刷新记录")}</button>` : ""}</div>`;
  }

  async function loadMore(section) {
    const item = team();
    if (state.moreLoading || !item) return;
    const version = ++pageVersion;
    pageController = new AbortController();
    const signal = pageController.signal;
    state.moreLoading = true;
    renderPanel();
    try {
      await source.more(item, section, signal);
      if (version === pageVersion && team() === item) item.historyExpanded = true;
    } catch (error) {
      if (version === pageVersion && team() === item) state.error = error.message;
    } finally {
      if (version === pageVersion) state.moreLoading = false;
      if (version === pageVersion && team() === item) {
        renderPanel();
        renderStatus();
        if (!$("#detail-dialog").open) ($(`[data-load-more="${section}"]`) || $(`#panel-${state.tab}`)).focus();
      }
    }
  }

  function readRoute() {
    const parts = location.hash.slice(1).split("/");
    if (teams.some((item) => item.id === parts[0])) state.teamId = parts[0];
    if (tabs.includes(parts[1])) state.tab = parts[1];
  }

  function navigate(teamId, tab, updateHistory = true) {
    const teamChanged = state.teamId !== teamId;
    if ($("#detail-dialog").open) $("#detail-dialog").close();
    if (state.teamId !== teamId || state.tab !== tab) { cancelPagination(); cancelDetail(); }
    if (state.teamId !== teamId) {
      controller?.abort();
      requestVersion += 1;
      state.loading = false;
      state.error = "";
      state.taskFilter = "all";
      state.activityFilter = "all";
      state.taskQuery = "";
      state.activityQuery = "";
    }
    state.teamId = teamId;
    state.tab = tab;
    const route = `#${teamId}/${tab}`;
    if (updateHistory && location.hash !== route) history.pushState(null, "", route);
    handledRoute = location.hash;
    render();
    if (state.teamId && (!team()?.loaded || (teamChanged && source.live))) void refreshRecords(false);
  }

  function directoryAttention(item) {
    const value = item.directoryAttention || {};
    const labels = [["decisions", t("待决定")], ["handoff_tasks", t("待交接")], ["blocked_tasks", t("受阻")], ["deliveries", t("投递待查")], ["cooldowns", t("冷却")]];
    const counts = labels.filter(([key]) => value[key] && !(closed(item) && key === "cooldowns"));
    return counts.length ? `<span class="directory-attention ${closed(item) ? "historical-attention" : ""}">${closed(item) ? t("历史未解决：") : t("需关注：")}${counts.map(([key, label]) => `${label} ${value[key]}`).join(" · ")}</span>` : "";
  }

  function renderDirectory() {
    const focusedTeam = document.activeElement?.dataset?.team;
    const query = $("#team-search").value.trim().toLocaleLowerCase();
    const matches = teams.filter((item) => `${item.name} ${item.project}`.toLocaleLowerCase().includes(query));
    $("#team-count").textContent = t("{0} 个团队", teams.length);
    $("#search-status").textContent = t("找到 {0} 个团队", matches.length);
    const groups = [
      { label: t("当前团队"), items: matches.filter((item) => !closed(item)) },
      { label: t("历史记录"), items: matches.filter(closed) }
    ];
    $("#team-list").innerHTML = matches.length ? groups.filter((group) => group.items.length).map((group) => `
      <section class="team-group"><h2>${group.label}</h2>${group.items.map((item) => `
        <button type="button" class="team-link ${item.id === state.teamId ? "is-selected" : ""}" data-team="${esc(item.id)}" ${item.id === state.teamId ? 'aria-current="page"' : ""}>
          <span class="team-link-title">${esc(item.name)}</span><span class="team-link-bottom"><span>${esc(item.project)}</span><span>${esc(teamStates[item.state] || item.state)}</span></span>
          ${directoryAttention(item)}
        </button>`).join("")}</section>`).join("") : `<p class="search-empty">${t("没有找到团队。")}<br>${t("试试项目名，或清空搜索。")}</p>`;
    if (focusedTeam) $(`[data-team="${CSS.escape(focusedTeam)}"]`)?.focus();
  }

  function monitorAlert(item) {
    const monitor = item?.monitor;
    if (!source.live || !monitor) return "";
    if (!monitor.all_active_codex && !monitor.all_pending_codex) return "";
    if (monitor.running === true && monitor.status === "unresponsive") {
      return `<div class="monitor-alert" role="alert"><strong>${t("Codex 通知服务无响应")}</strong><p>${t("服务仍占用运行锁，但已 {0} 秒没有心跳，可能已卡住。请检查或重启本机通知服务（", esc(String(monitor.heartbeat_age ?? "?")))}<code>native-team monitor</code>${t("；Linux 服务：")}<code>systemctl --user status native-team-monitor</code>${t("）。")}</p></div>`;
    }
    if (monitor.running === true && monitor.status === "degraded") {
      return `<div class="monitor-alert monitor-degraded" role="status"><strong>${t("Codex 通知服务部分降级")}</strong><p>${t("{0}其他成员照常送达。服务会自动恢复，无需立即处理。", monitor.backoff ? t("{0} 位成员的唤醒线程正在退避重启；", esc(String(monitor.backoff))) : "")}</p></div>`;
    }
    if (monitor.running !== false) return "";
    const parts = [];
    if (monitor.pending_codex > 0) parts.push(t("当前团队积压 {0} 条待发送的 Codex 唤醒", monitor.pending_codex));
    if (monitor.all_pending_codex > monitor.pending_codex) parts.push(t("全部项目共积压 {0} 条", monitor.all_pending_codex));
    const summary = parts.length ? t("{0}，任务与消息不会自动唤醒 Codex。", parts.join(t("，"))) : t("活跃 Codex 成员当前不会收到任务或消息自动唤醒。");
    return `<div class="monitor-alert" role="alert"><strong>${t("Codex 通知服务未运行")}</strong><p>${t("{0} 请检查或启动本机通知服务（", esc(summary))}<code>native-team monitor</code>${t("；Linux 服务：")}<code>systemctl --user status native-team-monitor</code>${t("）。")}</p></div>`;
  }

  function renderHeader() {
    const item = team();
    if (!item) {
      $("#breadcrumb-name").textContent = t("所有团队");
      $("#team-header").innerHTML = `<h1 id="team-title">${t("团队工作台")}</h1>`;
      $("#task-count").textContent = "0";
      return;
    }
    const active = item.members.filter((member) => member.state === "active").length;
    $("#breadcrumb-name").textContent = item.name;
    $("#team-header").innerHTML = `
      ${monitorAlert(item)}
      <div class="team-heading"><h1 id="team-title">${esc(item.name)}</h1>${badge(teamStates[item.state] || item.state, item.state)}</div>
      <p class="team-purpose">${esc(item.purpose || t("尚未记录团队目标"))}</p>
      <div class="team-meta"><span>${esc(item.projectPath)}</span><span>${t("{0} 位成员{1}", item.members.length, item.state === "active" ? (active < item.members.length ? t(" · {0} 位已停用", item.members.length - active) : t(" · 均已登记")) : t(" · 保留协作记录"))}</span><span class="readonly-label">${t("只读视图")}</span></div>`;
    $("#task-count").textContent = state.scenario === "empty" ? "0" : String(item.pages?.tasks.total ?? item.tasks.length);
    document.title = `${item.name} · Native Team${source.live ? "" : t(" 设计示例")}`;
  }

  function lifecycleNotice(item) {
    if (item.state === "paused") return `<div class="lifecycle-note"><strong>${t("进度已保存，团队已暂停")}</strong><p>${t("可以继续查看记录。未来恢复需在原生会话中进行，并先同步已有上下文。")}</p></div>`;
    if (item.state === "done") return `<div class="lifecycle-note"><strong>${t("工作已结束，历史仍在")}</strong><p>${t("这里只供查阅。已完成团队目前不能直接恢复；查看页面不会重新启动任务。")}</p></div>`;
    if (item.state === "cancelled") return `<div class="lifecycle-note"><strong>${t("团队已取消，记录保留")}</strong><p>${t("可以查阅此前的共享内容。查看不会重启团队，也不会改变成员状态。")}</p></div>`;
    return "";
  }

  const providerKey = (provider) => {
    const value = String(provider || "").toLowerCase();
    return value === "agy" || value === "codex" || value === "claude" ? value : "other";
  };
  const providerLabel = (provider) => ({ codex: "Codex", claude: "Claude", agy: "agy" }[providerKey(provider)] || String(provider || ""));
  function providerAvatar(provider) {
    const key = providerKey(provider);
    const icons = {
      agy: '<svg viewBox="0 0 20 20" aria-hidden="true" focusable="false"><path d="M10 2.4C10.55 6.85 13.15 9.45 17.6 10C13.15 10.55 10.55 13.15 10 17.6C9.45 13.15 6.85 10.55 2.4 10C6.85 9.45 9.45 6.85 10 2.4Z" fill="currentColor" stroke="none"/></svg>',
      claude: '<svg viewBox="0 0 20 20" aria-hidden="true" focusable="false"><path d="M10 3.2v13.6M3.2 10h13.6M5.2 5.2l9.6 9.6M14.8 5.2l-9.6 9.6" stroke-width="1.85"/></svg>',
      codex: '<svg viewBox="0 0 20 20" aria-hidden="true" focusable="false"><path d="M4.5 6.5 8.8 10l-4.3 3.5M10.4 13.5h5.1" stroke-width="1.95"/></svg>',
    };
    const glyph = icons[key] || esc((String(provider || "?").trim()[0] || "?").toUpperCase());
    return `<span class="avatar avatar-${key}" data-provider="${key}" title="${esc(providerLabel(provider))}">${glyph}</span>`;
  }

  function memberRows(item) {
    return item.members.map((member) => `
      <button class="member-row" type="button" data-detail="member" data-id="${esc(member.id)}">
        <span class="member-identity">${providerAvatar(member.provider)}<span><strong>${esc(member.id)}</strong><span class="provider">${esc(providerLabel(member.provider))}<span>${closed(item) ? t("历史成员") : member.state === "inactive" ? t("已停用") : t("已登记")}</span></span></span></span>
        <span class="member-work"><strong>${esc(member.lane)}</strong><span>${esc(member.current)}</span>${!closed(item) && availabilityLabel(member) ? `<span class="availability-label ${member.availability?.status === "ready" && member.availability?.posture !== "conserve" ? "availability-ready" : ""}">${esc(availabilityLabel(member))}</span>` : ""}</span>
        <span class="member-next"><span class="field-label">${t("下一步")}</span><span>${esc(member.next)}</span><span class="report-time">${t("记录于 {0}", esc(member.reported))}</span>${!closed(item) && waitingLines(member).length ? `<span class="waiting-label">${esc(waitingLines(member).join(t("；")))}</span>` : ""}</span>
      </button>`).join("");
  }

  function availabilityLabel(member) {
    const value = member.availability;
    if (member.provider === "claude" && member.native_quota && !value?.held && (!value?.recorded || value.posture === "unknown")) return t("订阅额度按会话观测");
    if (!value?.recorded) return "";
    if (value.held && value.recorded_by === "native quota monitor") return t("额度冷却 · 等待原生客户端确认恢复");
    if (value.resume_due) return t("已到记录的重置时间 · 额度待验证");
    if (value.held) return value.resume_after == null ? t("额度冷却 · 恢复时间未知") : t("额度冷却 · 记录的恢复时间 {0}", source.date(value.resume_after));
    if (value.posture === "conserve") return t("节省额度 · 必要交接照常通知");
    if (value.posture === "unknown") {
      const quota = member.native_quota;
      return quota?.association === "verified" && quota.stale ? t("额度待刷新 · 未暂停协作") : t("额度未知 · 未暂停协作");
    }
    return t("已记录可继续 · 非在线检测");
  }

  function quotaObservations(value) {
    const observations = value?.observations;
    if (!Array.isArray(observations) || !observations.length) return `<p class="muted">${t("没有剩余额度观测记录。")}</p>`;
    const scopes = { account: t("账户"), model: t("模型"), window: t("用量窗口") };
    return `<ul class="quota-observations">${observations.map((item) => {
      const remaining = item.remaining == null ? t("未提供余额") : `${item.remaining}${item.unit === "percent" ? "%" : ` ${item.unit}`}`;
      return `<li><strong>${esc(scopes[item.scope] || item.scope)} · ${esc(item.bucket)}</strong><p>${item.stale ? t("快照已失效 · 当前余额未知；历史观测：") : t("观测时剩余：")}${esc(remaining)}</p><p class="view-note">${t("来源：{0}", esc(item.source))}<br>${t("观测于 {0} · 本次读取时距观测 {1}", esc(source.date(item.observed_at)), age(Math.max(0, item.age_seconds || 0)))}<br>${t("有效期至 {0} · 记录的重置时间：{1}", esc(source.date(item.expires_at)), item.resets_at == null ? t("未知") : esc(source.date(item.resets_at)))}</p></li>`;
    }).join("")}</ul><p class="view-note">${t("快照不是实时余额，也不为会话预留额度；多个窗口可能共享同一账户，不能把数字相加。重置时间仅供参考，不会自动解除冷却。")}</p>`;
  }

  function waitingLines(member) {
    const waiting = member.waiting;
    if (!waiting) return [];
    const lines = [];
    if (waiting.unreceived_messages) lines.push(t("{0} 条重要消息未读取，最早已等 {1}", waiting.unreceived_messages, age(waiting.message_age_seconds)));
    if (waiting.pending_tasks) lines.push(t("{0} 项任务待接手或评审，最早已等 {1}", waiting.pending_tasks, age(waiting.task_age_seconds)));
    return lines;
  }

  function memberAvailabilityDetail(member, item) {
    if (!source.live || closed(item)) return "";
    const value = member.availability;
    const origin = value?.recorded_by === "native quota monitor" ? t("程序采集的原生额度状态，无需模型上报；到期仍需新的恢复证据。") : t("这是成员或用户登记的信息，不是服务商配额查询；到期不代表额度已恢复。");
    return `<section><h3>${t("当前额度记录")}</h3><p>${esc(availabilityLabel(member) || t("未记录额度限制"))}</p>${value?.reason ? `<p>${esc(value.recorded_by === "native quota monitor" ? t(value.reason) : value.reason)}</p>` : ""}<p class="view-note">${value?.recorded ? t("记录于 {0} · {1}。", esc(source.date(value.updated)), esc(value.recorded_by)) : ""}${origin}</p></section><section><h3>${t("额度观测快照")}</h3>${quotaObservations(value)}</section><section><h3>${t("等待情况")}</h3>${waitingLines(member).length ? list(waitingLines(member)) : `<p class="muted">${t("未记录待读取的重要消息或待接手、待评审任务。")}</p>`}<p class="view-note">${t("时长按本页读取时刻计算。任务等待不代表消息尚未读取；刷新可更新记录。")}</p></section>`;
  }

  function taskStatus(task) {
    return `<span class="task-status">${badge(taskStates[task.state] || task.state, task.state)}${task.handoff_needed ? badge(t("执行待交接"), "blocked") : ""}${task.reviewer_handoff_needed ? badge(t("评审待交接"), "blocked") : ""}</span>`;
  }

  function deliveryDetails(event) {
    if (!event.deliveries?.length) return "";
    const labels = { pending: t("等待通知"), queued: t("已排入原生通知队列"), queued_warning: t("已入队，投递需核查"), failed: t("通知失败"), uncertain: t("投递情况未确认"), suppressed: t("无需再通知") };
    return `<ul class="delivery-list">${event.deliveries.map((delivery) => `<li><strong>${esc(delivery.alias)}</strong><span>${esc(delivery.acked_at != null ? t("已确认接收 · {0}", source.date(delivery.acked_at)) : delivery.received_at != null ? t("已读取，尚未确认 · {0}", source.date(delivery.received_at)) : labels[delivery.state] || delivery.state)}</span></li>`).join("")}</ul>`;
  }

  function attentionSummary(item) {
    const pending = item.decisions.filter((decision) => ["pending", "proposed", "disputed"].includes(decision.state));
    if (!source.live) return pending.length ? `<section class="attention-strip"><span class="attention-icon" aria-hidden="true">?</span><div><strong>${t("有 {0} 项需要你判断", pending.length)}</strong><p>${esc(pending[0].title)}</p></div>${detailButton("decision", pending[0].id, t("查看建议"), "outline-button")}</section>` : "";
    const unresolved = (item.decision_counts?.proposed || 0) + (item.decision_counts?.disputed || 0);
    const lines = [];
    const cooling = item.members.filter((member) => member.state === "active" && member.availability?.held);
    if (!closed(item) && cooling.length) lines.push(`<div><span>${t("{0} 位成员已记录额度冷却；团队工作和上下文保留", cooling.length)}</span>${detailButton("member", cooling[0].id, t("查看额度记录"), "outline-button")}</div>`);
    if (unresolved) lines.push(`<div><span>${t("全团队有 {0} 项共同决定尚未达成一致{1}", unresolved, pending.length < unresolved ? t("，包括尚未加载的历史记录") : "")}</span><button type="button" class="outline-button" data-go-tab="memory">${t("查看决定")}</button></div>`);
    if (item.attention?.handoff_tasks || item.task_counts?.blocked) lines.push(`<div><span>${t("{0} 项任务待交接，{1} 项受阻", item.attention?.handoff_tasks || 0, item.task_counts?.blocked || 0)}</span><button type="button" class="outline-button" data-go-tab="tasks">${t("查看任务")}</button></div>`);
    if (item.attention?.deliveries) lines.push(`<div><span>${t("{0} 条未读取通知投递失败或情况未确认", item.attention.deliveries)}</span><button type="button" class="outline-button" data-go-tab="activity">${t("核对消息")}</button></div>`);
    return lines.length ? `<section class="attention-strip"><div class="attention-lines"><strong>${closed(item) ? t("历史记录中的未解决事项") : t("共享记录中需要关注")}</strong>${lines.join("")}</div></section>` : "";
  }

  function renderOverview(item) {
    return `${lifecycleNotice(item)}
      <section class="current-focus"><div class="focus-copy"><span class="field-label">${source.live ? t("最近一次成员更新") : t("当前推进")}</span><h2>${esc(item.current)}</h2><p><span>${t("接下来")}</span>${esc(item.next)}</p></div></section>
      ${attentionSummary(item)}
      <section class="work-section"><div class="section-heading"><div><h2>${t("成员各自的主线")}</h2><p>${t("平级协作。点击成员，查看完整报告与交接。")}</p></div><span class="section-meta">${t("{0} 位成员", item.members.length)}</span></div>
        <div class="member-column-head" aria-hidden="true"><span>${t("成员")}</span><span>${t("当前工作")}</span><span>${t("下一步")}</span></div><div class="member-list">${memberRows(item)}</div>
      </section>
      <div class="overview-bottom"><section class="recent-section"><div class="section-heading"><h2>${t("最近的协作")}</h2><button class="text-button" type="button" data-go-tab="activity">${t("查看全部")}</button></div>
        ${item.activity.length ? item.activity.slice(0, 2).map((event) => `<button type="button" class="recent-item" data-detail="event" data-id="${esc(event.id)}"><time>${esc(event.time)}</time><span><strong>${esc(event.title)}</strong><span>${esc(event.author)}</span></span></button>`).join("") : `<p class="muted empty-inline">${t("暂无共享动态。已有的检查点保存在团队记忆中。")}</p>`}
      </section><section class="checkpoint-card"><span class="field-label">${t("留给下一次继续")}</span><h2>${esc(item.memory.title)}</h2><p>${esc(item.checkpoint)}</p><button type="button" class="text-button" data-go-tab="memory">${t("打开团队记忆")}</button></section></div>`;
  }

  function filterButtons(options, selected, kind) {
    return `<div class="filter-row" role="group" aria-label="${kind === "task" ? t("任务状态") : t("动态类型")}">${options.map(([id, label]) => `<button class="filter-button" type="button" data-${kind}-filter="${id}" aria-pressed="${selected === id}">${esc(label)}</button>`).join("")}</div>`;
  }

  function emptyContent(title, text) {
    return `<div class="empty-state"><div class="empty-mark" aria-hidden="true"><span></span><span></span><span></span></div><h2>${esc(title)}</h2><p>${esc(text)}</p></div>`;
  }

  function matchingTasks(item) {
    const query = state.taskQuery.trim().toLocaleLowerCase();
    return item.tasks.filter((task) => (state.taskFilter === "all" || task.state === state.taskFilter) && `${task.title} ${task.brief} ${task.owner} ${task.reviewer} ${task.result}`.toLocaleLowerCase().includes(query));
  }

  function taskFilters(item) {
    return filterButtons([["all", t("全部")], ...Object.entries(taskStates).map(([status, label]) => [status, `${label}${item.task_counts?.[status] ? ` ${item.task_counts[status]}` : ""}`])], state.taskFilter, "task");
  }

  function taskResults(item, visible) {
    const searching = Boolean(state.taskQuery.trim());
    return `${source.live ? `<p class="view-note">${t("全团队有 {0} 项未完成任务；状态数量涵盖全部历史，列表按已加载记录筛选。", item.memory.openTotal)}</p>` : ""}
      ${visible.length ? `<div class="task-table"><div class="task-table-head" aria-hidden="true"><span>${t("任务")}</span><span>${t("执行 / 评审")}</span><span>${t("状态")}</span></div>${visible.map((task) => `<button type="button" class="task-row" data-detail="task" data-id="${esc(task.id)}"><span><strong>${esc(task.title)}</strong><span class="task-description">${esc(task.brief)}</span></span><span class="task-people"><span>${esc(task.owner)}</span><span>${t("评审 {0}", esc(task.reviewer))}</span></span>${taskStatus(task)}</button>`).join("")}</div>` : emptyContent(searching ? t("没有匹配的任务") : t("这里暂时没有任务"), searching ? t("关键词仅搜索已加载记录；试试其他词，或加载更早记录。") : state.taskFilter === "all" ? t("团队的讨论和记忆仍可查看。并非每次协作都需要创建任务。") : t("已加载记录中没有符合该状态的任务，可以切换到“全部”或加载更早记录。"))}${pagination(item, "tasks")}`;
  }

  function renderTasks(item) {
    const visible = matchingTasks(item);
    return `<div class="section-heading panel-intro"><div><h2>${t("每次交接，都有去处")}</h2><p>${t("执行与评审由不同成员负责；验收结果保留证据。")}</p></div></div>
      <div id="task-filters">${taskFilters(item)}</div>
      <label class="record-search" for="task-query">${t("查找已加载任务")}<input id="task-query" type="search" value="${esc(state.taskQuery)}" placeholder="${t("关键词、成员或报告")}" autocomplete="off"><span id="task-matches" role="status">${t("{0} 条匹配", visible.length)}</span></label>
      <div id="task-results">${taskResults(item, visible)}</div>`;
  }

  function matchingActivity(item) {
    const query = state.activityQuery.trim().toLocaleLowerCase();
    return item.activity.filter((event) => (state.activityFilter === "all" || (state.activityFilter === "decision" ? event.type === "decision" : state.activityFilter === "attention" ? event.type === "attention" || event.delivery_attention > 0 : ["result", "review"].includes(event.type))) && `${event.title} ${event.body} ${event.author}`.toLocaleLowerCase().includes(query));
  }

  function activityFilters() {
    return filterButtons([["all", t("全部")], ["decision", t("需求与决定")], ["review", t("交付与评审")], ["attention", t("阻塞与交接")]], state.activityFilter, "activity");
  }

  function activityResults(item, visible) {
    const searching = Boolean(state.activityQuery.trim());
    return `${visible.length ? `<ol class="activity-feed">${visible.map((event) => `<li><time>${esc(event.time)}</time><div class="event-content"><div class="event-meta"><strong>${esc(event.author)}</strong><span>${esc(event.kindLabel || activityTypes[event.type])}</span></div>${detailButton("event", event.id, event.title, "event-title")}<p>${esc(event.body)}</p><span class="receipt-label">${esc(event.receipt)}</span>${event.delivery_attention ? badge(t("{0} 条投递需核查", event.delivery_attention), "blocked") : ""}</div></li>`).join("")}</ol>` : emptyContent(searching ? t("没有匹配的共享动态") : t("还没有这类共享动态"), searching ? t("关键词仅搜索已加载消息；试试其他词，或加载更早记录。") : t("消息出现后，可以在这里查看内容和接收情况。已读取不等于已同意或已完成。"))}${pagination(item, "activity")}`;
  }

  function renderActivity(item) {
    const visible = matchingActivity(item);
    return `<div class="section-heading panel-intro"><div><h2>${t("共享信息，保持对齐")}</h2><p>${t("展示成员共享内容和自动额度提醒，不包含私人终端聊天。")}</p></div></div>
      <div id="activity-filters">${activityFilters()}</div>
      <label class="record-search" for="activity-query">${t("查找已加载动态")}<input id="activity-query" type="search" value="${esc(state.activityQuery)}" placeholder="${t("关键词或成员")}" autocomplete="off"><span id="activity-matches" role="status">${t("{0} 条匹配", visible.length)}</span></label>
      <div id="activity-results">${activityResults(item, visible)}</div>
      <p class="view-note">${t("消息已送达、成员已读取、成员已回应和任务已验收，会分别显示。")}</p>`;
  }

  function updateRecordPanel(item, kind, refreshFilters = true) {
    const visible = kind === "task" ? matchingTasks(item) : matchingActivity(item);
    if (refreshFilters) $(`#${kind}-filters`).innerHTML = kind === "task" ? taskFilters(item) : activityFilters();
    const input = $(`#${kind}-query`);
    if (input.value !== state[`${kind}Query`]) input.value = state[`${kind}Query`];
    $(`#${kind}-matches`).textContent = t("{0} 条匹配", visible.length);
    $(`#${kind}-results`).innerHTML = kind === "task" ? taskResults(item, visible) : activityResults(item, visible);
  }

  function renderMemory(item) {
    const memory = item.memory;
    return `<div class="section-heading panel-intro"><div><h2>${t("回来时，从这里接上")}</h2><p>${t("留下结论、依据与下一步，让继续工作有起点。")}</p></div><span class="section-meta">${t("更新于 {0}", esc(memory.updated))}</span></div>
      <section class="memory-checkpoint"><span class="field-label">${source.live ? t("最新共享工作板 · 成员原文") : t("最近检查点")}</span><h3>${esc(memory.title)}</h3><p>${esc(memory.summary)}</p><div class="memory-next"><strong>${t("下一步")}</strong><p>${esc(memory.next)}</p></div></section>
      ${source.live && memory.reportIds.length ? `<div class="memory-report-links">${memory.reportIds.map((id) => detailButton("member", id, t("{0} 的工作板", id), "outline-button")).join("")}</div>` : ""}
      <div class="memory-grid"><section><h3>${source.live ? t("已达成一致的决定") : t("已经确定的约束")}</h3>${memory.constraints.length ? list(memory.constraints, "memory-list") : `<p class="muted">${t("已加载记录中尚无已确认的决定。")}</p>`}</section><section><h3>${t("尚待推进")}</h3>${source.live ? `<p class="view-note">${t("全团队有 {0} 项未完成任务，此处已加载 {1} 项。", memory.openTotal, memory.open.length)}</p>` : ""}${memory.open.length ? list(memory.open, "memory-list open-items") : source.live && memory.openTotal ? `<p class="muted">${t("待办仍在更早记录中。")}</p>` : `<p class="muted">${t("没有记录中的待办任务。")}</p>`}${source.live && memory.openTotal > memory.open.length ? `<button type="button" class="text-button" data-go-tab="tasks">${t("查看任务与历史")}</button>` : ""}</section></div>
      ${item.decisions.length ? `<section class="memory-decisions"><div class="section-heading"><h3>${t("重要决定与理由")}</h3><span class="section-meta">${t("保留已确定与待讨论的区别")}</span></div>${item.decisions.map((decision) => `<button class="decision-row" type="button" data-detail="decision" data-id="${esc(decision.id)}"><span><strong>${esc(decision.title)}</strong><span>${esc(decision.summary)}</span></span>${badge(decisionStates[decision.state] || decision.state, decision.state)}</button>`).join("")}</section>` : ""}${pagination(item, "decisions")}
      <section class="memory-sources"><h3>${t("成果与依据")}</h3>${source.live ? (memory.taskIds.length ? `<div class="memory-evidence-links">${memory.taskIds.map((id) => detailButton("task", id, item.tasks.find((task) => task.id === id).title, "outline-button")).join("")}</div>` : `<p class="muted">${t("已加载记录中尚无已验收任务。")}</p>`) : list(memory.evidence, "memory-list")}<p class="view-note">${source.live ? t("来源为已有共享工作板、决定和任务证据；未生成 AI 总结。更早的任务可在“任务与评审”中继续加载。") : t("当前均为示例条目。真实接入后，这里应指向可检查的共享成果。")}</p></section>`;
  }

  function renderQuota(item) {
    if (closed(item)) return emptyContent(t("历史团队保留协作记录"), t("当前账号额度不属于这个团队的历史状态，因此不在这里显示。"));
    const collector = item.quota_collector || {};
    const groups = new Map();
    const missing = [];
    const borrowed = [];
    const shared = collector.claude?.shared_account;
    function claudeWindowStale(window) {
      if (!window || window.applies !== false) return Boolean(window?.stale);
      // An already-running older workbench forwards timestamps but ages only the whole session.
      const age = item.read_at - window.observed_at;
      return window.stale || (window.observed_at != null && (age < 0 || age >= (shared?.fresh_seconds ?? 300))) || (window.resets_at != null && window.resets_at <= item.read_at);
    }
    if (shared?.team_id === item.id) {
      const members = item.members.filter((m) => m.provider === "claude" && m.state !== "inactive" && shared.members.includes(m.id));
      if (members.length) {
        const windows = new Map();
        for (const bucket of ["five_hour", "seven_day"]) {
          const member = members.find((m) => m.id === shared.sources[bucket]);
          const window = member?.native_quota?.association === "verified" && member.native_quota.windows.find((w) => w.bucket === bucket);
          const observed = window?.observed_at;
          windows.set(bucket, { ...window, bucket, observed, stale: claudeWindowStale(window), names: window ? [member.id] : [] });
        }
        groups.set("shared-claude", { shared: true, provider: "claude", account: t("Claude Code 共享订阅额度"), members, windows });
      }
    }
    for (const member of item.members) {
      if (member.state === "inactive") continue;
      if (groups.get("shared-claude")?.members.includes(member)) continue;
      const value = member.native_quota;
      if (!value) { missing.push(member); continue; }
      const key = `${member.provider}:${value.account === "账户未识别" ? member.id : value.account}`;
      const label = member.provider === "claude" ? t("Claude Code 订阅额度")
        : value.account_key ? t("账户 {0}", value.account_key.slice(0, 8)) : t(value.account || "账户未识别");
      if (!groups.has(key)) groups.set(key, { provider: member.provider, account: label, members: [], windows: new Map() });
      const group = groups.get(key);
      group.members.push(member);
      for (const window of value.windows) {
        if (window.shared_from) { borrowed.push([group, member, window]); continue; }
        const stale = member.provider === "claude" ? claudeWindowStale(window) : window.stale;
        const key = JSON.stringify([window.bucket, window.remaining, window.amount, window.resets_at, window.disabled, stale]);
        const observed = window.observed_at ?? (member.provider === "claude" ? null : value.observed_at);
        if (!group.windows.has(key)) group.windows.set(key, { ...window, stale, names: [], observed, applies: false });
        const row = group.windows.get(key);
        row.names.push(member.id);
        if (observed != null) row.observed = Math.max(row.observed ?? observed, observed);
        row.applies ||= window.applies;
      }
    }
    // An idle member's borrowed reading only shows when no member of the same card
    // observed that window itself; the idle member stays listed on the card.
    for (const [group, member, window] of borrowed) {
      if ([...group.windows.values()].some((row) => row.bucket === window.bucket && !row.shared_from)) continue;
      const stale = member.provider === "claude" ? claudeWindowStale(window) : window.stale;
      const key = JSON.stringify([window.bucket, window.remaining, window.amount, window.resets_at, window.disabled, stale, window.shared_from]);
      if (!group.windows.has(key)) group.windows.set(key, { ...window, stale, names: [], observed: window.observed_at ?? null, applies: false });
      const row = group.windows.get(key);
      row.names.push(member.id);
      row.applies ||= window.applies;
    }
    const cards = [...groups.values()].map((group) => `<section class="quota-account-card"><div class="section-heading"><div><span class="eyebrow">${esc(group.provider)}</span><h3>${esc(group.account)}</h3></div><span class="section-meta">${t("{0} 个已关联成员", group.members.length)}</span></div>
      ${group.shared ? `<div class="memory-report-links">${group.members.map((m) => detailButton("member", m.id, m.id + (m.availability?.held ? t("（冷却中）") : ""), "outline-button")).join("")}</div>` : `<div class="quota-members">${group.members.map((member) => `<div>${detailButton("member", member.id, member.id)}<span>${esc(member.native_quota.model || t("模型未提供"))}</span><p>${esc(availabilityLabel(member) || t("等待额度判断"))}</p>${member.native_quota.association !== "verified" ? `<p class="quota-warning">${t("原生会话已变化，关联待重新确认。")}</p>` : ""}</div>`).join("")}</div>`}
      <div class="quota-windows">${[...group.windows.values()].map((window) => {
        const claude = group.provider === "claude";
        const label = claude ? ({ five_hour: t("5 小时"), seven_day: t("7 天") }[window.bucket] || window.bucket) : window.bucket;
        const amount = claude && window.remaining == null ? t("未提供") : window.disabled ? t("未启用") : window.stale ? t("待刷新") : window.remaining != null ? `${Number(window.remaining.toFixed(1))}%` : window.amount != null ? t("{0}（单位未提供）", window.amount) : t("未知");
        return `<div class="quota-window ${!window.disabled && !window.stale && window.remaining === 0 ? "quota-exhausted" : ""}"><div><strong>${esc(label)}</strong><span>${claude ? t("订阅额度剩余") : window.applies ? t("用于当前模型") : t("其他额度窗口")}</span></div><strong class="quota-amount">${esc(amount)}</strong><p>${t("预计重置：{0}", window.resets_at == null ? t("未提供") : esc(source.date(window.resets_at)))}</p>${window.names.length ? `<p class="view-note">${t("{0}来源成员：{1}{2}", window.observed == null ? "" : `${t("观测于 {0}", esc(source.date(window.observed)))}<br>`, esc(window.names.join(t("、"))), window.shared_from ? t("（会话空闲，读数来自同账户的 {0}）", esc(window.shared_from)) : "")}</p>` : ""}</div>`;
      }).join("") || `<p class="muted">${t("原生客户端尚未提供额度窗口。")}</p>`}</div></section>`).join("");
    const monitorBadge = item.monitor?.running === false ? badge(t("通知服务未运行"), "blocked") : item.monitor?.status === "unresponsive" ? badge(t("通知服务无响应"), "blocked") : item.monitor?.status === "degraded" ? badge(t("通知服务部分降级"), "pending") : item.monitor?.running === true ? badge(t("通知服务运行中"), "active") : "";
    return `<div class="section-heading panel-intro"><div><h2>${t("账户与额度")}</h2><p>${t("登录由原生客户端管理。这里自动接收额度状态，无需模型主动上报。")}</p></div><div>${monitorBadge} ${badge(collector.enabled ? t("自动采集已启用") : t("自动采集未启用"), collector.enabled ? "active" : "paused")}</div></div>
      <div class="quota-source-status">${Object.entries({ codex: "Codex", agy: "agy", claude: "Claude Code" }).map(([provider, label]) => { const value = collector[provider]; return `<p><strong>${label}</strong><span>${esc(source.collectorDetail(value?.detail) || t("等待首次原生数据"))}</span>${value?.checked_at ? `<small>${t("检查于 {0}", esc(source.date(value.checked_at)))}</small>` : ""}</p>`; }).join("")}</div>
      <p class="view-note">${t("同一账户由多个窗口共享，数字不能相加。灰色“待刷新”表示当前余额未知；重置时间经过不会自动解除冷却。")}</p>
      <div class="quota-account-grid">${cards}</div>
      ${missing.length ? `<section class="quota-account-card"><h3>${t("等待关联的成员")}</h3><p>${t("尚未收到可确认属于这些会话的原生额度数据。")}</p><div class="memory-report-links">${missing.map((member) => detailButton("member", member.id, member.id, "outline-button")).join("")}</div></section>` : ""}
      <p class="view-note">${t("账户名称是本机生成的标识，不保存登录密码、令牌或私人聊天。agy 的状态栏快照可能是客户端缓存；余额不变时不会因重复渲染而延长有效期。")}</p>`;
  }

  function renderPanel() {
    preserveView(renderPanelContents);
  }

  function renderPanelContents() {
    const item = team();
    tabs.forEach((tab) => {
      const active = tab === state.tab;
      $(`#tab-${tab}`).setAttribute("aria-selected", String(active));
      $(`#tab-${tab}`).tabIndex = active ? 0 : -1;
      $(`#panel-${tab}`).hidden = !active;
    });
    const panel = $(`#panel-${state.tab}`);
    if (!item?.loaded) {
      panel.innerHTML = state.error
        ? emptyContent(t("暂时无法读取团队记录"), state.error) + `<div class="empty-action"><button type="button" class="outline-button" data-refresh>${t("重新读取")}</button></div>`
        : state.loading ? emptyContent(t("正在读取本机记录…"), t("读取不会唤醒成员或确认消息。"))
        : emptyContent(t("还没有登记的团队"), t("在已连接 Native Team 的原生会话中组队后，刷新这里即可查看。"));
    } else if (state.scenario === "error") {
      panel.innerHTML = `<div class="empty-state error-state"><span class="error-symbol" aria-hidden="true">!</span><h2>${t("暂时无法读取团队记录")}</h2><p>${t("此处正在演示读取失败。真实接入时应保留已显示的快照，并明确标注可能过期。")}</p><button type="button" class="primary-button" data-retry>${t("重试示例读取")}</button><p class="view-note">${t("重试只恢复原型展示，不会启动团队。")}</p></div>`;
    } else if (state.scenario === "empty") {
      panel.innerHTML = emptyContent(t("团队已登记，还没有共享记录"), t("下一次成员发布主线、结果或检查点后，会展示在这里。可以先在原生会话中自然讨论。")) + `<div class="empty-action"><button type="button" class="outline-button" data-retry>${t("返回有内容的示例")}</button></div>`;
    } else {
      const kind = state.tab === "tasks" ? "task" : state.tab === "activity" ? "activity" : null;
      if (kind && panel.dataset.teamId === item.id && panel.querySelector(`#${kind}-results`)) {
        updateRecordPanel(item, kind);
      } else {
        panel.innerHTML = state.tab === "workflow"
          ? workflow.render(item, state.workflowMode, panel.clientWidth)
          : ({ overview: renderOverview, tasks: renderTasks, activity: renderActivity, memory: renderMemory, quota: renderQuota })[state.tab](item);
      }
      panel.dataset.teamId = item.id;
    }
  }

  function render() {
    preserveView(() => {
      renderDirectory();
      renderHeader();
      renderPanelContents();
      renderStatus();
    });
  }

  // A reviewer's marks stay in this browser, separate from shared task acceptance.
  const reviewStorageKey = "native-team:viewed-files:v1";
  let viewedFiles = new Map();
  try {
    const rows = JSON.parse(localStorage.getItem(reviewStorageKey) || "[]");
    if (Array.isArray(rows)) viewedFiles = new Map(rows.slice(-1024));
  } catch { /* Private browsing can disable storage. */ }
  function renderManifest(task) {
    const report = task.changes;
    if (!report?.files?.length) return "";
    let viewed = 0;
    const entries = report.files.map((file, index) => {
      const key = JSON.stringify([task.id, file.path]);
      const previous = viewedFiles.get(key);
      const digest = file.status + ":" + file.fingerprint.toLowerCase();
      const checked = previous?.digest === digest;
      if (checked) viewed++;
      const status = checked ? t("已查看") : previous ? t("有新变更") : t("未查看");
      const counts = [file.additions != null ? "+" + file.additions : "", file.deletions != null ? "−" + file.deletions : ""].filter(Boolean).join(" ");
      return `<li><label><input type="checkbox" data-review-index="${index}" data-review-task="${esc(task.id)}" ${checked ? "checked" : ""}> ${esc(file.path)} · ${status}</label>${checked ? "" : `<p>${esc(file.status)} ${esc(counts)} ${esc(file.summary || "")}</p>`}</li>`;
    });
    return `<section class="change-manifest"><h3>${t("变更清单 · {0}/{1} 已查看", viewed, entries.length)}</h3><ul>${entries.join("")}</ul></section>`;
  }
  document.addEventListener("change", (event) => {
    const input = event.target.closest("[data-review-index]");
    if (!input) return;
    const task = team()?.tasks.find((item) => item.id === input.dataset.reviewTask);
    const file = task?.changes?.files[Number(input.dataset.reviewIndex)];
    if (!file) return;
    const key = JSON.stringify([task.id, file.path]);
    viewedFiles.delete(key);
    if (input.checked) viewedFiles.set(key, {digest: file.status + ":" + file.fingerprint.toLowerCase(), revision: task.changes.revision});
    while (viewedFiles.size > 1024) viewedFiles.delete(viewedFiles.keys().next().value);
    try { localStorage.setItem(reviewStorageKey, JSON.stringify([...viewedFiles])); } catch { /* Keep in-memory marks. */ }
    input.closest(".change-manifest").outerHTML = renderManifest(task);
    document.querySelector(`[data-review-index="${Number(input.dataset.reviewIndex)}"]`)?.focus();
  });
  let detailTrigger = null;
  function openDetail(kind, id, trigger) {
    cancelDetail();
    const item = team();
    if (!item && kind !== "about") return;
    let title = "", label = "", body = "";
    if (kind === "task") {
      const task = item.tasks.find((entry) => entry.id === id);
      if (!task) return;
      title = task.title; label = t("任务详情");
      body = `${taskStatus(task)}<p class="detail-lead">${esc(task.brief)}</p><dl class="people-grid"><div><dt>${t("执行成员")}</dt><dd>${esc(task.owner)}</dd></div><div><dt>${t("独立评审")}</dt><dd>${esc(task.reviewer)}</dd></div></dl>${renderManifest(task)}${task.handoff_needed || task.reviewer_handoff_needed ? `<p class="detail-note">${t("任务仍在等待原生会话完成交接；列出的成员不代表已接手。")}</p>` : ""}<section><h3>${t("怎样算完成")}</h3>${list(task.acceptance)}</section><section><h3>${t("当前报告")}</h3><p>${esc(task.result)}</p></section><section><h3>${t("证据与依据")}</h3>${task.evidence.length ? list(task.evidence) : `<p class="muted">${t("尚未提交证据。")}</p>`}</section>${source.live ? `<section><h3>${t("任务声明的写入范围")}</h3>${task.paths?.length ? list(task.paths) : `<p class="muted">${t("只读任务，没有声明写入路径。")}</p>`}</section>` : ""}<p class="detail-note">${esc(task.note)}</p>`;
    } else if (kind === "flow") {
      const flow = workflow.flows(item).find((entry) => entry.id === id);
      if (!flow) return;
      title = `${flow.from} → ${flow.to}`; label = t("任务流");
      body = `<p class="detail-lead">${t("{0} · {1} 项任务", flow.kind === "request" ? t("发起成员 → 执行成员") : t("执行成员 → 独立评审"), flow.tasks.length)}</p><p class="detail-note">${t("这是当前记录的任务分工；连线本身不证明任务已开始、送达或完成。")}</p><section>${flow.tasks.map((task) => `<p>${detailButton("task", task.id, task.title)}</p>${taskStatus(task)}`).join("")}</section>`;
    } else if (kind === "msgflow") {
      const flow = workflow.messageFlows(item).find((entry) => entry.id === id);
      if (!flow) return;
      title = `${flow.from} → ${flow.to}`; label = t("消息往来");
      body = `<p class="detail-lead">${t("{0} · {1} 条，{2} 已读取 {3} 条", esc(workflow.messageLabels[flow.messageKind] || flow.messageKind), flow.messages.length, esc(flow.to), flow.received)}</p><p class="detail-note">${t("连线表示已记录的消息投递；已读取只代表收到了内容，不代表同意建议或完成任务。")}</p><section>${[...flow.messages].reverse().map((message) => `<p>${detailButton("event", message.id, t("消息 #{0} · {1}", message.seq, source.date(message.at)))}</p>`).join("")}</section>`;
    } else if (kind === "member") {
      const member = item.members.find((entry) => entry.id === id);
      if (!member) return;
      title = member.id; label = t("成员主线");
      body = `<p class="detail-lead">${esc(providerLabel(member.provider))} · ${esc(member.lane)}</p><p class="detail-note">${closed(item) ? t("团队已结束；下列为保留的历史报告。") : item.state === "paused" ? t("团队已暂停；成员登记不代表正在执行。") : t("以下是成员报告的工作，不是终端在线监测。")}</p><section><h3>${t("当前工作")}</h3><p>${esc(member.current)}</p></section><section><h3>${t("下一步")}</h3><p>${esc(member.next)}</p></section>${memberAvailabilityDetail(member, item)}${member.checkout ? `<section><h3>${t("工作目录")}</h3><p>${esc(member.checkout)} ${esc(member.branch || "")}</p>${member.merge_warnings?.length ? `<strong>${t("合并时需复核")}</strong>${list(member.merge_warnings.map(w => w.alias + " · " + w.paths.join(", ")))}` : ""}</section>` : ""}<section><h3>${t("阻塞与等待")}</h3><p>${esc(member.blockers)}</p></section><section><h3>${t("交接")}</h3><p>${esc(member.handoff)}</p></section>${member.task ? detailButton("task", member.task, t("查看相关任务"), "outline-button") : ""}<p class="view-note">${t("{0}：{1}", source.live ? t("工作板时间") : t("示例报告时间"), esc(member.reported))}</p>`;
    } else if (kind === "decision") {
      const decision = item.decisions.find((entry) => entry.id === id);
      if (!decision) return;
      title = decision.title; label = t("共同决定");
      body = `${badge(decisionStates[decision.state] || decision.state, decision.state)}<p class="detail-lead">${esc(decision.summary)}</p>${decision.author ? `<p class="view-note">${t("提议成员：{0}", esc(decision.author))}</p>` : ""}<section><h3>${source.live ? t("提议内容") : t("为什么提出")}</h3><p>${esc(decision.reason)}</p></section>${decision.authority ? `<section><h3>${t("记录的用户指示")}</h3><p>${esc(decision.authority)}</p></section>` : ""}${decision.options.length ? `<section><h3>${t("讨论中的选择")}</h3>${list(decision.options)}</section>` : ""}<section><h3>${t("当前回应")}</h3><p>${esc(decision.responses)}</p></section><p class="detail-note">${source.live ? t("页面只展示已记录的提议和回应。查看不会提交决定，也不代表成员同意。") : t("这是只读示例。此处没有提交决定；真实操作应保留明确的用户意图和成员回应。")}</p>`;
    } else if (kind === "event") {
      const event = item.activity.find((entry) => entry.id === id) || item.activityExtra?.[id];
      const seq = event ? null : item.workflow?.messages?.find((message) => message.id === id)?.seq;
      if (!event && source.live && seq && !item.activityMissing?.[id]) {
        // Older messages linked from the chart may sit beyond the loaded activity page.
        const version = detailVersion;
        detailController = new AbortController();
        void source.message(item, seq, detailController.signal).then((found) => {
          // A closed dialog, another detail, navigation or a newer read wins.
          const current = team();
          if (version !== detailVersion || current?.id !== item.id) return;
          if (found && current !== item) (current.activityExtra ||= {})[found.id] = found;
          if (!found) (current.activityMissing ||= {})[id] = true;
          openDetail(kind, id, trigger);
        }).catch((error) => {
          if (version !== detailVersion || team()?.id !== item.id || error.name === "AbortError") return;
          state.error = error.message;
          renderStatus();
        });
        return;
      }
      if (!event && !seq) return;
      label = t("共享动态");
      if (!event) {
        title = t("消息 #{0}", seq);
        body = `<p class="detail-lead">${t("这条消息不在当前可读取的记录中。")}</p><p class="detail-note">${t("请刷新记录后再试；工作台只读，不会补写缺失的内容。")}</p>`;
      } else {
      title = event.title;
      body = `<p class="detail-lead">${esc(event.author)} · ${esc(event.time)}</p><section><p>${esc(event.body)}</p></section><div class="receipt-box"><strong>${esc(event.receipt)}</strong>${deliveryDetails(event)}<p>${t("读取只代表收到了内容，不代表同意建议或完成任务。")}</p></div>${event.target ? detailButton(event.targetType, event.target, t("查看相关记录"), "outline-button") : ""}`;
      }
    } else if (kind === "memory") {
      title = item.memory.title; label = t("团队检查点");
      body = `<p class="detail-lead">${esc(item.memory.summary)}</p><section><h3>${t("下一步")}</h3><p>${esc(item.memory.next)}</p></section><section><h3>${t("保留的约束")}</h3>${list(item.memory.constraints)}</section>`;
    } else if (kind === "about") {
      title = source.live ? t("查看团队已经共享的工作") : t("你正在查看设计示例"); label = t("关于工作台");
      body = source.live
        ? `<p class="detail-lead">${t("此工作台读取与 native-team teams 相同的本机记录，包含所有项目的团队和历史。")}</p><section><h3>${t("如何更新")}</h3><p>${t("点击“刷新记录”读取当前状态。需要保留运行 native-team ui 的终端；再次打开时运行同一命令即可。")}</p></section><section><h3>${t("这里能看到什么")}</h3><p>${t("成员发布的工作板、任务与证据、共享消息及决定。未共享的终端工作不会自动出现；没有记录的字段会明确标注。")}</p></section><section><h3>${t("只读边界")}</h3><p>${t("浏览不唤醒模型、不确认消息、不更改团队状态，也不读取私人终端聊天或项目文件。")}</p></section>`
        : `<p class="detail-lead">${t("这是虚构数据的界面样例。要看真实团队，请在终端运行 native-team ui；它会自动打开本机工作台。")}</p><section><h3>${t("设计示例")}</h3><p>${t("可以切换顶部场景，检查空状态与读取失败。这里不连接真实团队。")}</p></section>`;
    } else return;
    const dialog = $("#detail-dialog");
    if (!dialog.open) detailTrigger = trigger;
    $("#detail-kind").textContent = label;
    $("#detail-content").innerHTML = `<h2 id="detail-title">${esc(title)}</h2>${body}`;
    dialog.scrollTop = 0;
    if (!dialog.open) dialog.showModal();
    $("#close-detail").focus();
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest('button, [role="button"][data-detail]');
    if (!button) return;
    if (button.dataset.team) {
      navigate(button.dataset.team, "overview");
      $(`[data-team="${CSS.escape(state.teamId)}"]`)?.focus();
    }
    if (button.dataset.tab) navigate(state.teamId, button.dataset.tab);
    if (button.dataset.goTab) {
      navigate(state.teamId, button.dataset.goTab);
      $(`#tab-${state.tab}`).focus();
    }
    if (button.dataset.taskFilter) {
      state.taskFilter = button.dataset.taskFilter;
      renderPanel();
      $(`[data-task-filter="${state.taskFilter}"]`).focus();
    }
    if (button.dataset.activityFilter) {
      state.activityFilter = button.dataset.activityFilter;
      renderPanel();
      $(`[data-activity-filter="${state.activityFilter}"]`).focus();
    }
    if (button.dataset.workflowMode) {
      state.workflowMode = button.dataset.workflowMode;
      renderPanel();
      $(`[data-workflow-mode="${state.workflowMode}"]`).focus();
    }
    if (button.dataset.detail) openDetail(button.dataset.detail, button.dataset.id, button);
    if (button.dataset.loadMore) void loadMore(button.dataset.loadMore);
    if (button.hasAttribute("data-refresh")) void refreshRecords();
    if (button.hasAttribute("data-retry")) {
      state.scenario = "normal";
      $("#scenario").value = "normal";
      render();
      $(`#panel-${state.tab}`).focus();
    }
  });
  $("#team-search").addEventListener("input", renderDirectory);
  function searchInput(event) {
    if (event.target.id === "task-query") state.taskQuery = event.target.value;
    else if (event.target.id === "activity-query") state.activityQuery = event.target.value;
    else return;
    // Replacing an input while the native IME is composing cancels its candidate session.
    if (!event.isComposing) updateRecordPanel(team(), event.target.id === "task-query" ? "task" : "activity", false);
  }
  document.addEventListener("input", searchInput);
  document.addEventListener("compositionend", searchInput);
  document.addEventListener("keydown", (event) => {
    if (["Enter", " "].includes(event.key) && event.target.matches('[role="button"][data-detail]')) {
      event.preventDefault();
      openDetail(event.target.dataset.detail, event.target.dataset.id, event.target);
    }
  });
  $("#refresh-button").addEventListener("click", () => void refreshRecords());
  $("#scenario").addEventListener("change", (event) => { state.scenario = event.target.value; render(); });
  $("#about-button").addEventListener("click", (event) => openDetail("about", "", event.currentTarget));
  $("#theme-button").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  $("#refresh-interval").value = String(refreshInterval);
  $("#refresh-interval").addEventListener("change", (event) => {
    refreshInterval = Number(event.target.value);
    preference("refresh", refreshInterval);
    renderStatus();
    scheduleRefresh();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && backgroundRequest) {
      requestVersion += 1;
      controller?.abort();
      state.loading = false;
      backgroundRequest = false;
    }
    renderStatus();
    scheduleRefresh();
  });
  document.addEventListener("focusin", renderStatus);
  document.addEventListener("focusout", () => queueMicrotask(renderStatus));
  $("#close-detail").addEventListener("click", () => $("#detail-dialog").close());
  $("#detail-dialog").addEventListener("close", () => {
    cancelDetail();
    if (detailTrigger?.isConnected) detailTrigger.focus({ preventScroll: true });
    renderStatus();
  });
  $("#detail-dialog").addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const controls = [...event.currentTarget.querySelectorAll('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])')].filter((element) => element.getClientRects().length);
    const first = controls[0];
    const last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  $("#detail-dialog").addEventListener("click", (event) => {
    const dialog = event.currentTarget;
    const bounds = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom)) dialog.close();
  });
  $(".tab-bar").addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const index = tabs.indexOf(state.tab);
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    navigate(state.teamId, tabs[next]);
    $(`#tab-${state.tab}`).focus();
  });
  function routeChanged() {
    if (location.hash === handledRoute || location.hash === "#workspace") return;
    const previous = state.teamId;
    readRoute();
    const selected = state.teamId;
    state.teamId = previous;
    navigate(selected, state.tab, false);
  }
  window.addEventListener("popstate", routeChanged);
  window.addEventListener("hashchange", routeChanged);
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (state.tab === "workflow" && !$("#detail-dialog").open) renderPanel();
    }, 150);
  });
  window.addEventListener("pagehide", () => { clearTimeout(refreshTimer); controller?.abort(); pageController?.abort(); detailController?.abort(); });
  setTheme(preference("theme") === "dark" ? "dark" : "light");
  void refreshRecords();
})();
