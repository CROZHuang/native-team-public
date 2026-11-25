"use strict";

(() => {
  const live = window.nativeTeamMode === "live";
  const date = (value) => value == null ? "尚未记录" : new Date(value * 1000).toLocaleString("zh-CN", { hour12: false });
  const short = (value, limit = 100) => {
    const line = String(value || "").trim().replace(/\s+/g, " ");
    return line.length > limit ? `${line.slice(0, limit)}…` : line;
  };
  const recorded = (value) => value?.trim() || "尚未记录";
  const taskLabels = { ready: "待开始", working: "进行中", review: "待评审", accepted: "已验收", blocked: "受阻", cancelled: "已取消" };
  const kinds = { task: "任务更新", proposal: "共同提议", decision: "决定回应", user_instruction: "用户方向", milestone: "阶段进展", direct: "定向消息", blocker: "阻塞提醒", membership: "成员变更", lifecycle: "团队状态", handoff: "工作交接", quota: "自动额度提醒" };
  let demoPromise;
  let directoryReadAt = null;

  function demoData() {
    if (!demoPromise) demoPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "./fixtures.js";
      script.onload = () => resolve(window.nativeTeamDemo);
      script.onerror = () => reject(new Error("无法载入设计示例。"));
      document.head.append(script);
    });
    return demoPromise;
  }

  async function json(path, signal) {
    const controller = new AbortController();
    const cancel = () => controller.abort();
    if (signal?.aborted) cancel();
    signal?.addEventListener("abort", cancel, { once: true });
    const timer = setTimeout(cancel, 8000);
    try {
      const response = await fetch(path, { cache: "no-store", credentials: "omit", signal: controller.signal });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "无法读取本机团队记录。");
      return result;
    } catch (error) {
      if (signal?.aborted) throw error;
      if (error.name === "AbortError") throw new Error("读取超时；请检查工作台终端是否仍在运行，再刷新。");
      if (error instanceof TypeError) throw new Error("无法连接本机工作台；请确认 native-team ui 仍在运行。");
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
      note: raw.instruction || "任务状态来自共享记录；独立评审者与执行者分别列出。"
    };
  }

  function decision(raw) {
    const responses = raw.responses.map((vote) => `${vote.alias}：${({ agree: "同意", object: "有异议" })[vote.response] || "尚未回应"}${vote.note ? `\n${vote.note}` : ""}`).join("\n\n");
    return { ...raw, title: raw.topic, summary: short(raw.proposal, 160), reason: raw.proposal, options: [], responses: responses || "尚未记录成员回应。" };
  }

  function event(raw) {
    const type = ["proposal", "decision", "user_instruction"].includes(raw.kind) ? "decision" : ["blocker", "handoff", "quota"].includes(raw.kind) ? "attention" : raw.kind === "task" ? "review" : "update";
    return {
      ...raw, author: raw.kind === "quota" ? "Native Team 额度监控" : raw.author, time: date(raw.created), type, title: short(raw.body, 110), body: raw.body,
      kindLabel: kinds[raw.kind] || "共享更新",
      receipt: raw.recipients ? `已读取 ${raw.received} / ${raw.recipients} · 已确认接收 ${raw.acknowledged} / ${raw.recipients}` : "无单独接收人记录"
    };
  }

  function deriveMemory(item) {
    const reports = item.members.filter((member) => member.reportedAt).sort((a, b) => b.reportedAt - a.reportedAt);
    const latest = reports[0];
    const accepted = item.tasks.filter((entry) => entry.state === "accepted");
    const pending = item.tasks.filter((entry) => !["accepted", "cancelled"].includes(entry.state));
    const latestName = latest ? `${latest.id}${latest.state === "inactive" ? "（已停用）" : ""}` : "";
    item.current = latest ? `${latestName}：${latest.current}` : "成员尚未发布共享工作板；已登记不代表没有在工作。";
    item.next = latest?.next || "尚未记录下一步。可在成员原生会话中继续正常工作并共享进展。";
    item.checkpoint = latest ? short(latest.current, 180) : "共享工作板、决定和已验收成果会保留在这里。当前尚无工作板记录。";
    item.memory = {
      title: latest ? `${latestName} 的最新工作板` : "尚无共享工作板",
      updated: latest?.reported || "尚未记录", summary: latest?.current || item.checkpoint,
      next: latest?.next || "尚未记录", constraints: item.decisions.filter((entry) => entry.state === "agreed").map((entry) => `${entry.title}：${entry.summary}`),
      open: pending.map((entry) => `${taskLabels[entry.state] || entry.state}${entry.handoff_needed || entry.reviewer_handoff_needed ? " · 待交接" : ""}：${entry.title}`),
      openTotal: Object.entries(item.task_counts || {}).reduce((sum, [status, count]) => sum + (["accepted", "cancelled"].includes(status) ? 0 : count), 0),
      evidence: [], taskIds: accepted.map((entry) => entry.id), reportIds: reports.map((member) => member.id)
    };
    return item;
  }

  function normalize(raw) {
    const item = {
      ...raw, loaded: true, projectPath: raw.project, project: raw.project.split("/").filter(Boolean).at(-1) || raw.project,
      members: raw.members.map((member) => ({
        ...member, id: member.alias, provider: member.provider === "codex" ? "Codex" : member.provider,
        lane: member.lane || "主线尚未填写", current: recorded(member.current), next: recorded(member.next_step),
        blockers: recorded(member.blockers), handoff: recorded(member.handoffs),
        reported: date(member.reported_at), reportedAt: member.reported_at
      })),
      tasks: raw.tasks.items.map(task), decisions: raw.decisions.items.map(decision), activity: raw.activity.items.map(event),
      pages: Object.fromEntries(["tasks", "activity", "decisions"].map((section) => [section, { total: raw[section].total, next: raw[section].next_cursor }]))
    };
    return deriveMemory(item);
  }

  window.nativeTeamSource = {
    live, date,
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
