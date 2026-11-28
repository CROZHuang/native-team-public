// Browser regression for the shared web assets. All data and requests are local fixtures.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { readFile } = require("node:fs/promises");
const { chromium } = require("playwright");

const assets = process.env.WORKBENCH_ASSETS || path.join(__dirname, "../web/prototype");
const files = { "index.html": "text/html", "styles.css": "text/css", "mark.svg": "image/svg+xml",
  "i18n.js": "text/javascript", "app.js": "text/javascript", "workflow.js": "text/javascript", "data-source.js": "text/javascript" };
const pageOf = items => ({ items, total: items.length, next_cursor: null });
const team = {
  id: "test", name: "待评审", project: "/sample", purpose: "A sample", state: "active", read_at: 1700000000,
  members: [{ alias: "sample", provider: "codex", state: "active" }],
  decisions: pageOf([]), task_counts: { review: 1 },
  tasks: pageOf([{ id: "task-1", brief: "待评审", state: "review", owner: "sample", reviewer: "peer", writes: [] }]),
  activity: pageOf([{ id: "event-1", kind: "direct", author: "sample", body: "待评审 {0}", created: 1700000000 }]),
  quota_collector: { claude: { detail: "2 个已登记会话，1 个已收到原生状态栏快照。有 1 份快照未通过校验。" } }
};

test("switch workbench language and preserve original records and route", async () => {
  const browser = await chromium.launch({ executablePath: process.env.WORKBENCH_BROWSER });
  try {
    const context = await browser.newContext();
    const errors = [];
    let scenario = "team";
    await context.route("**/*", async route => {
      const { hostname, pathname } = new URL(route.request().url());
      assert.equal(hostname, "127.0.0.1");
      if (pathname === "/config.js") return route.fulfill({ contentType: "text/javascript", body: 'window.nativeTeamMode = "live";' });
      if (pathname === "/api/teams") {
        if (scenario === "error") return route.fulfill({ status: 503, json: { error: "无法读取本机团队记录。" } });
        return route.fulfill({ json: { teams: scenario === "empty" ? [] : [team], read_at: team.read_at } });
      }
      if (pathname === "/api/teams/test") return route.fulfill({ json: team });
      const file = pathname === "/" ? "index.html" : pathname.slice(1);
      if (!Object.hasOwn(files, file)) return route.fulfill({ status: 404, body: "" });
      return route.fulfill({ body: await readFile(path.join(assets, file)), contentType: files[file] + "; charset=utf-8" });
    });
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    page.on("pageerror", error => errors.push(error.message));
    await page.goto("http://127.0.0.1:31234/");
    await page.getByRole("tab", { name: "任务与评审" }).click();
    const picker = page.getByRole("combobox", { name: "界面语言" });
    await picker.focus();
    await picker.press("ArrowDown");
    await page.getByRole("tab", { name: "Tasks & reviews" }).waitFor();
    assert.equal(new URL(page.url()).hash, "#test/tasks");
    assert.equal(await page.locator("html").getAttribute("lang"), "en");
    await page.locator(".task-row").getByText("In review", { exact: true }).waitFor();
    assert.equal(await page.locator(".task-row strong").textContent(), "待评审");
    await page.reload();
    for (const [tab, heading] of [["Overview", "Member workstreams"], ["Tasks & reviews", "Task assignments and reviews"],
      ["Activity", "Shared activity"], ["Workflow", "Team workflow"], ["Accounts & quota", "Accounts & quota"], ["Team memory", "Team memory"]]) {
      await page.getByRole("tab", { name: tab }).click();
      await page.getByRole("tabpanel").getByRole("heading", { name: heading, exact: true }).waitFor();
    }
    await page.getByRole("tab", { name: "Accounts & quota" }).click();
    await page.getByText("Registered sessions: 2. Sessions with status-line readings: 1. Snapshots that failed validation: 1.").waitFor();
    await page.getByRole("tab", { name: "Activity" }).click();
    await page.getByRole("button", { name: "待评审 {0}", exact: true }).click();
    await page.getByRole("dialog").getByRole("heading", { name: "待评审 {0}" }).waitFor();
    await page.getByRole("button", { name: "Close details" }).click();
    await page.setViewportSize({ width: 375, height: 812 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    scenario = "empty";
    await page.goto("http://127.0.0.1:31235/");
    await page.getByRole("heading", { name: "No teams registered yet" }).waitFor();
    await page.getByRole("button", { name: "About", exact: true }).click();
    await page.getByRole("dialog").getByRole("heading", { name: "About the workbench" }).waitFor();
    await page.keyboard.press("Escape");
    scenario = "error";
    await page.reload();
    await page.getByRole("button", { name: "Retry", exact: true }).waitFor();
    await page.getByRole("combobox", { name: "Interface language" }).selectOption("zh-CN");
    await page.getByRole("button", { name: "重新读取" }).waitFor();
    assert.equal(await page.locator("html").getAttribute("lang"), "zh-CN");
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});
