"use strict";

// Rendering uses the same escaping, status vocabulary and drawers as the workbench.
window.nativeTeamWorkflow = ({ esc, badge, detailButton, date, taskStates, decisionStates, closed }) => {
  const short = (text, limit) => { const value = String(text || ""); return value.length > limit ? `${value.slice(0, limit - 1)}…` : value; };
  function laneLines(text, columns) {
    let remaining = String(text || "").replace(/\s+/g, " ").trim();
    const lines = [];
    for (let row = 0; row < 2 && remaining; row += 1) {
      let length = 0, units = 0;
      for (const character of remaining) {
        const next = /[^\u0000-\u00ff]/.test(character) ? 1 : character === " " ? .3 : .56;
        if (units + next > columns - (row === 1 ? 1 : 0)) break;
        units += next; length += character.length;
      }
      if (length < remaining.length && row === 0) {
        const space = remaining.lastIndexOf(" ", length);
        if (space > length / 2) length = space;
      }
      lines.push(remaining.slice(0, length).trim() + (row === 1 && length < remaining.length ? "…" : ""));
      remaining = remaining.slice(length).trim();
    }
    return lines;
  }
  const selected = (item) => {
    const ids = item.workflow?.task_ids;
    return ids ? item.tasks.filter((task) => ids.includes(task.id)) : item.tasks.slice(0, 30);
  };

  function flows(item) {
    const groups = new Map();
    for (const task of selected(item)) {
      for (const [kind, from, to] of [["request", task.requester, task.owner], ["review", task.owner, task.reviewer]]) {
        if (!from || !to || from === to) continue;
        const id = JSON.stringify([kind, from, to]);
        if (!groups.has(id)) groups.set(id, { id, kind, from, to, tasks: [] });
        groups.get(id).tasks.push(task);
      }
    }
    return [...groups.values()];
  }

  function flowState(flow) {
    if (flow.tasks.some((task) => task.state === "blocked" || task.handoff_needed || task.reviewer_handoff_needed)) return "blocked";
    if (flow.tasks.some((task) => task.state === "review")) return "review";
    if (flow.tasks.some((task) => task.state === "working")) return "working";
    if (flow.tasks.every((task) => task.state === "accepted")) return "accepted";
    return "ready";
  }

  function graph(item, availableWidth) {
    if (!item.members.length) return '<p class="empty-inline muted">没有成员记录，暂不绘制关系。</p>';
    const width = Math.max(200, Math.min(960, availableWidth));
    const mobile = width < 560;
    const members = item.members.slice(0, 20);
    const count = members.length;
    const nodeWidth = Math.min(230, mobile ? width - 66 : (width - 120) / 2);
    const nodeHeight = 108;
    const height = mobile ? count * 140 + 8 : count <= 2 ? 212 : Math.max(470, Math.ceil(count / 2) * 158 + 40);
    const positions = new Map();
    members.forEach((member, i) => {
      let x, y;
      if (mobile) { x = 10; y = 16 + i * 140; }
      else if (count === 1) { x = (width - nodeWidth) / 2; y = 48; }
      else if (count === 2) { x = i ? width - nodeWidth - 20 : 20; y = 48; }
      else if (count > 6) { x = i % 2 ? width - nodeWidth - 20 : 20; y = 20 + Math.floor(i / 2) * 158; }
      else {
        const angle = -Math.PI / 2 + i * 2 * Math.PI / count;
        x = width / 2 + Math.cos(angle) * (width / 2 - nodeWidth / 2 - 16) - nodeWidth / 2;
        y = height / 2 + Math.sin(angle) * (height / 2 - nodeHeight / 2 - 16) - nodeHeight / 2;
      }
      positions.set(member.id, { x, y });
    });
    const edges = flows(item).filter((flow) => positions.has(flow.from) && positions.has(flow.to));
    const paths = edges.map((flow, i) => {
      const a = positions.get(flow.from), b = positions.get(flow.to);
      // Reversing an edge already reverses its normal: flipping the sign as well
      // would put a request and its reverse review on exactly the same curve.
      const offset = mobile ? (flow.kind === "request" ? -10 : 10) : (flow.kind === "request" ? 9 : 16);
      let d;
      if (mobile) {
        const x = a.x + nodeWidth, y1 = a.y + nodeHeight / 2 + offset, y2 = b.y + nodeHeight / 2 + offset;
        const bend = Math.min(width - 8, x + 28 + (i % 3) * 9);
        d = `M ${x} ${y1} C ${bend} ${y1}, ${bend} ${y2}, ${x + 4} ${y2}`;
      } else {
        const ax = a.x + nodeWidth / 2, ay = a.y + nodeHeight / 2;
        const bx = b.x + nodeWidth / 2, by = b.y + nodeHeight / 2;
        const dx = bx - ax, dy = by - ay;
        const scale = Math.max(Math.abs(dx) / (nodeWidth / 2 + 5), Math.abs(dy) / (nodeHeight / 2 + 5));
        const sx = ax + dx / scale, sy = ay + dy / scale;
        const tx = bx - dx / scale, ty = by - dy / scale;
        const length = Math.hypot(dx, dy) || 1;
        d = `M ${sx} ${sy} Q ${(sx + tx) / 2 - dy / length * offset * 3} ${(sy + ty) / 2 + dx / length * offset * 3}, ${tx} ${ty}`;
      }
      const label = `${flow.from} → ${flow.to} · ${flow.kind === "request" ? "发起执行" : "提交评审"} · ${flow.tasks.length} 项任务`;
      return `<g role="button" tabindex="0" class="workflow-edge flow-${flowState(flow)} ${flow.kind === "request" ? "flow-request" : ""}" data-detail="flow" data-id="${esc(flow.id)}" aria-label="${esc(label)}"><title>${esc(label)}</title><path class="edge-hit" d="${d}"/><path class="edge-line" d="${d}" marker-end="url(#workflow-arrow)"/></g>`;
    }).join("");
    const nodes = members.map((member) => {
      const { x, y } = positions.get(member.id);
      const posture = member.availability?.held ? "冷却" : member.availability?.posture === "conserve" ? "节省额度" : "";
      const status = closed(item) ? "历史成员" : member.state === "inactive" ? "已停用" : posture || "已登记";
      const full = `${member.id} · ${member.provider} · ${status} · ${member.lane}`;
      return `<g role="button" tabindex="0" class="workflow-node ${member.state === "inactive" ? "node-inactive" : ""}" data-detail="member" data-id="${esc(member.id)}" aria-label="${esc(full)}" transform="translate(${x} ${y})"><title>${esc(full)}</title><rect width="${nodeWidth}" height="${nodeHeight}" rx="8"/><text class="node-alias" x="14" y="29">${esc(short(member.id, Math.floor((nodeWidth - 28) / 8)))}</text><text class="node-meta" x="14" y="52">${esc(member.provider)} · ${esc(status)}</text><text class="node-lane">${laneLines(member.lane, (nodeWidth - 28) / 14).map((line, i) => `<tspan x="14" y="${79 + i * 19}">${esc(line)}</tspan>`).join("")}</text></g>`;
    }).join("");
    return `<svg class="workflow-graph" viewBox="0 0 ${width} ${height}" role="group" aria-label="成员协作关系，可用 Tab 选择成员和任务流"><defs><marker id="workflow-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 1 1 L 9 5 L 1 9"/></marker></defs>${paths}${nodes}</svg>${count < item.members.length ? '<p class="view-note">图中最多显示 20 位成员；完整名单见工作进展。</p>' : ""}`;
  }

  function timeline(item) {
    const events = [...(item.workflow?.events || [])].reverse();
    if (!events.length) return '<p class="empty-inline muted">当前范围没有保存的状态变更记录。关系图仍可查看已记录的任务分工。</p>';
    const kinds = { assigned: "创建任务", claimed: "接手", review: "提交评审", revise: "要求修改", accept: "验收", block: "评审阻塞", blocked: "报告阻塞", handoff: "变更分工", released: "释放执行", recovered: "恢复交接", cancelled: "取消", proposed: "提出决定", decision: "回应决定" };
    return `<ol class="workflow-timeline">${events.map((event) => {
      const record = (event.target_type === "task" ? item.tasks : item.decisions).find((entry) => entry.id === event.target_id);
      const status = (event.target_type === "task" ? taskStates : decisionStates)[event.state] || event.state || "状态无法读取";
      return `<li><time>${esc(date(event.at))}</time><div><div class="timeline-meta"><span>${esc(event.actor)} · ${esc(kinds[event.kind] || event.kind)}</span>${badge(status, event.state || "unknown")}</div>${detailButton(event.target_type, event.target_id, record?.title || "查看记录", "event-title")}<p class="view-note">修订 ${event.revision}${event.round > 0 ? ` · 返修轮次 ${event.round}` : ""}</p></div></li>`;
    }).join("")}</ol>`;
  }

  function render(item, mode, width) {
    const tasks = selected(item);
    const total = item.pages?.tasks.total ?? item.tasks.length;
    const workflow = item.workflow;
    const note = `展示最近 ${tasks.length} / ${total} 项任务的分工；时间线覆盖最近 ${workflow?.decision_ids?.length || 0} / ${item.pages?.decisions.total ?? item.decisions.length} 项决定，最多 ${workflow?.limits?.events || 200} 条事件。`;
    return `<div class="section-heading panel-intro"><div><h2>协作如何向前推进</h2><p>成员平级；箭头表示任务分工，不表示组织层级。</p></div></div><div class="filter-row" role="group" aria-label="协作图视图"><button type="button" class="filter-button" data-workflow-mode="graph" aria-pressed="${mode === "graph"}">协作关系</button><button type="button" class="filter-button" data-workflow-mode="timeline" aria-pressed="${mode === "timeline"}">状态历程</button></div><p class="view-note workflow-scope">${esc(note)}${workflow?.truncated ? " 较早事件已截断。" : ""}${workflow?.invalid_records ? ` ${workflow.invalid_records} 条历史无法完整解析，未补画状态。` : ""}</p>${mode === "timeline" ? `<p class="view-note workflow-scope">任务来自修订历史；决定仅展示保留的提议和结果事件，不是完整投票历史。当前决定状态见详情。</p>${timeline(item)}` : `<div class="workflow-legend"><span class="legend-request">发起 → 执行</span><span class="legend-review">执行 → 评审</span><span>点击连线可查看对应任务</span></div>${graph(item, width)}<section class="workflow-tasks"><h3>图中的任务</h3><p class="view-note">${closed(item) ? "历史分工" : "当前记录的分工"}；图中未显示的发起人以“未记录”标注。待交接任务不代表新成员已经接手。</p>${tasks.length ? `<ul>${tasks.map((task) => `<li>${detailButton("task", task.id, task.title)}<div class="flow-task-people">${esc(task.requester || "发起人未记录")} → ${esc(task.owner)} → ${esc(task.reviewer)}</div><div class="flow-task-state">${badge(taskStates[task.state] || task.state, task.state)}${task.handoff_needed || task.reviewer_handoff_needed ? badge("待交接", "blocked") : ""}</div></li>`).join("")}</ul>` : '<p class="empty-inline muted">暂时没有任务连线。成员仍可共享工作板和决定。</p>'}</section>`}`;
  }
  return { render, flows };
};
