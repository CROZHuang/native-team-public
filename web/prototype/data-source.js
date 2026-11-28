"use strict";

(() => {
  const { t } = window.nativeTeamI18n;
  const live = window.nativeTeamMode === "live";
  const date = (value) => value == null ? t("尚未记录") : new Date(value * 1000).toLocaleString(window.nativeTeamI18n.locale, { hour12: false });
  const short = (value, limit = 100) => {
    const line = String(value || "").trim().replace(/\s+/g, " ");
    return line.length > limit ? `${line.slice(0, limit)}…` : line;
  };
  const recorded = (value) => value?.trim() || t("尚未记录");
  const taskLabels = { ready: t("待开始"), working: t("进行中"), review: t("待评审"), accepted: t("已验收"), blocked: t("受阻"), cancelled: t("已取消") };
  const kinds = { task: t("任务更新"), proposal: t("共同提议"), decision: t("决定回应"), user_instruction: t("用户方向"), milestone: t("阶段进展"), direct: t("定向消息"), blocker: t("阻塞提醒"), membership: t("成员变更"), lifecycle: t("团队状态"), handoff: t("工作交接"), quota: t("自动额度提醒") };
  let demoPromise;
  let directoryReadAt = null;

  function demoData() {
    if (!demoPromise) demoPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "./fixtures.js";
      script.onload = () => resolve(window.nativeTeamDemo);
      script.onerror = () => reject(new Error(t("无法载入设计示例。")));
      document.head.append(script);
    });
    return demoPromise;
  }

  async function json(path, signal, headers = {}, timeout = 8000) {
    const controller = new AbortController();
    const cancel = () => controller.abort();
    if (signal?.aborted) cancel();
    signal?.addEventListener("abort", cancel, { once: true });
    const timer = setTimeout(cancel, timeout);
    try {
      const response = await fetch(path, { cache: "no-store", credentials: "omit", signal: controller.signal, headers });
      const result = await response.json();
      if (!response.ok) throw new Error(t(result.error || "无法读取本机团队记录。"));
      return result;
    } catch (error) {
      if (signal?.aborted) throw error;
      if (error.name === "AbortError") throw new Error(t("读取超时；请检查工作台终端是否仍在运行，再刷新。"));
      if (error instanceof TypeError) throw new Error(t("无法连接本机工作台；请确认 native-team ui 仍在运行。"));
      throw error;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", cancel);
    }
  }

  function task(raw) {
    return {
      ...raw, title: short(raw.brief), acceptance: raw.acceptance ? [raw.acceptance] : [],
      result: recorded(raw.result), evidence: raw.evidence ? [raw.evidence] : [],
      note: raw.instruction || t("任务状态来自共享记录；独立评审者与执行者分别列出。")
    };
  }

  function decision(raw) {
    const responses = raw.responses.map((vote) => t("{0}：{1}{2}", vote.alias, ({ agree: t("同意"), object: t("有异议") })[vote.response] || t("尚未回应"), vote.note ? `\n${vote.note}` : "")).join("\n\n");
    return { ...raw, title: raw.topic, summary: short(raw.proposal, 160), reason: raw.proposal, options: [], responses: responses || t("尚未记录成员回应。") };
  }

  function event(raw) {
    const type = ["proposal", "decision", "user_instruction"].includes(raw.kind) ? "decision" : ["blocker", "handoff", "quota"].includes(raw.kind) ? "attention" : raw.kind === "task" ? "review" : "update";
    return {
      ...raw, author: raw.kind === "quota" ? t("Native Team 额度监控") : raw.author, time: date(raw.created), type, title: short(raw.body, 110), body: raw.body,
      kindLabel: kinds[raw.kind] || t("共享更新"),
      receipt: raw.recipients ? t("已读取 {0} / {1} · 已确认接收 {2} / {3}", raw.received, raw.recipients, raw.acknowledged, raw.recipients) : t("无单独接收人记录")
    };
  }

  function deriveMemory(item) {
    const reports = item.members.filter((member) => member.reportedAt).sort((a, b) => b.reportedAt - a.reportedAt);
    const latest = reports[0];
    const accepted = item.tasks.filter((entry) => entry.state === "accepted");
    const pending = item.tasks.filter((entry) => !["accepted", "cancelled"].includes(entry.state));
    const latestName = latest ? `${latest.id}${latest.state === "inactive" ? t("（已停用）") : ""}` : "";
    item.current = latest ? t("{0}：{1}", latestName, latest.current) : t("成员尚未发布共享工作板；已登记不代表没有在工作。");
    item.next = latest?.next || t("尚未记录下一步。可在成员原生会话中继续正常工作并共享进展。");
    item.checkpoint = latest ? short(latest.current, 180) : t("共享工作板、决定和已验收成果会保留在这里。当前尚无工作板记录。");
    item.memory = {
      title: latest ? t("{0} 的最新工作板", latestName) : t("尚无共享工作板"),
      updated: latest?.reported || t("尚未记录"), summary: latest?.current || item.checkpoint,
      next: latest?.next || t("尚未记录"), constraints: item.decisions.filter((entry) => entry.state === "agreed").map((entry) => t("{0}：{1}", entry.title, entry.summary)),
      open: pending.map((entry) => t("{0}{1}：{2}", taskLabels[entry.state] || entry.state, entry.handoff_needed || entry.reviewer_handoff_needed ? t(" · 待交接") : "", entry.title)),
      openTotal: Object.entries(item.task_counts || {}).reduce((sum, [status, count]) => sum + (["accepted", "cancelled"].includes(status) ? 0 : count), 0),
      evidence: [], taskIds: accepted.map((entry) => entry.id), reportIds: reports.map((member) => member.id)
    };
    return item;
  }

  // Collector status is program-generated. Older ledgers store the rendered Chinese text.
  function collectorDetail(detail) {
    if (!detail || window.nativeTeamI18n.locale === "zh-CN") return detail;
    const patterns = [
      [/^(\d+) 个已登记会话，(\d+) 个已收到原生状态栏快照。$/, "{0} 个已登记会话，{1} 个已收到原生状态栏快照。"],
      [/^已跳过 (\d+) 份无效快照，其余继续处理。$/, "已跳过 {0} 份无效快照，其余继续处理。"],
      [/^有 (\d+) 份快照未通过校验。$/, "有 {0} 份快照未通过校验。"],
      [/^状态栏采集失败（(\w+)）。$/, "状态栏采集失败（{0}）。"],
      [/^本地采集失败（(\w+)）；保留旧记录并标明过期。$/, "本地采集失败（{0}）；保留旧记录并标明过期。"],
      [/^原生额度读取失败（(\w+)）；不会把网络或登录错误判为额度耗尽。$/, "原生额度读取失败（{0}）；不会把网络或登录错误判为额度耗尽。"]
    ];
    return detail.split(/(?<=。)\s*/u).filter(Boolean).map(sentence => {
      for (const [pattern, message] of patterns) {
        const match = sentence.match(pattern);
        if (match) return t(message, ...match.slice(1));
      }
      return t(sentence);
    }).join(" ");
  }

  function normalize(raw) {
    const item = {
      ...raw, loaded: true, projectPath: raw.project, project: raw.project.split("/").filter(Boolean).at(-1) || raw.project,
      members: raw.members.map((member) => ({
        ...member, id: member.alias, provider: member.provider === "codex" ? "Codex" : member.provider,
        lane: member.lane || t("主线尚未填写"), current: recorded(member.current), next: recorded(member.next_step),
        blockers: recorded(member.blockers), handoff: recorded(member.handoffs),
        reported: date(member.reported_at), reportedAt: member.reported_at
      })),
      tasks: raw.tasks.items.map(task), decisions: raw.decisions.items.map(decision), activity: raw.activity.items.map(event),
      pages: Object.fromEntries(["tasks", "activity", "decisions"].map((section) => [section, { total: raw[section].total, next: raw[section].next_cursor }]))
    };
    return deriveMemory(item);
  }

  window.nativeTeamSource = {
    live, date, collectorDetail,
    localQuota(refresh, signal) {
      return json("/api/quota", signal, { "X-Native-Team-Quota": refresh ? "refresh" : "read" }, 15000);
    },
    get directoryReadAt() { return directoryReadAt; },
    async directory(signal) {
      if (!live) return (await demoData()).map((item) => ({ ...item, loaded: true }));
      const result = await json("/api/teams", signal);
      directoryReadAt = result.read_at;
      return result.teams.map((item) => ({ ...item, projectPath: item.project, project: item.project.split("/").filter(Boolean).at(-1) || item.project, members: [], tasks: [], decisions: [], activity: [], loaded: false }));
    },
    async detail(id, signal) {
      if (!live) return { ...structuredClone((await demoData()).find((item) => item.id === id)), loaded: true };
      return normalize(await json(`/api/teams/${encodeURIComponent(id)}`, signal));
    },
    async message(item, seq, signal) {
      // Read one older message by sequence through the existing activity page API.
      if (!live || !Number.isInteger(seq) || seq < 1) return null;
      const result = await json(`/api/teams/${encodeURIComponent(item.id)}/activity?cursor=${encodeURIComponent(String(seq + 1))}`, signal);
      if (signal?.aborted) throw new DOMException("Cancelled", "AbortError");
      const raw = result.items.find((entry) => entry.seq === seq);
      if (!raw) return null;
      const found = event(raw);
      (item.activityExtra ||= {})[found.id] = found;
      return found;
    },
    async more(item, section, signal) {
      const cursor = item.pages?.[section]?.next;
      if (!live || cursor == null) return item;
      const result = await json(`/api/teams/${encodeURIComponent(item.id)}/${section}?cursor=${encodeURIComponent(cursor)}`, signal);
      if (signal?.aborted) throw new DOMException("Cancelled", "AbortError");
      const map = { tasks: task, activity: event, decisions: decision }[section];
      const known = new Set(item[section].map((entry) => entry.id));
      item[section].push(...result.items.filter((entry) => !known.has(entry.id)).map(map));
      item.pages[section] = { total: result.total, next: result.next_cursor };
      return deriveMemory(item);
    }
  };
})();
