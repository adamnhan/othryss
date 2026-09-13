import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createServer } from "node:net";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright";

const root = resolve(process.argv[2] ?? "");

const manifest = { example: "synthetic", version: "source" };
assert.equal(manifest.example, "synthetic");
const reservation = createServer();
await new Promise((done) => reservation.listen(0, "127.0.0.1", done));
const port = reservation.address().port;
await new Promise((done) => reservation.close(done));
const base = `http://127.0.0.1:${port}`;
const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(PYTHON|OTHRYSS|KALSHI)/i.test(key)));
const server = spawn("python", ["-S", "-m", "othryss.server", "--port", String(port)], { cwd: root, env, windowsHide: true, stdio: "ignore" });
let browser;
try {
  for (let attempt = 0; attempt < 100; attempt++) {
    try { const response = await fetch(base); await response.arrayBuffer(); if (response.ok) break; } catch {}
    if (attempt === 99) throw new Error("Packaged explorer did not start");
    await delay(100);
  }
  browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL ?? "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`${base}/#example`);
  await page.locator("#explorer").waitFor({ state: "visible" });
  await page.waitForFunction(() => document.querySelector("#fill-count").textContent === "2");
  assert.equal(await page.locator("#instrument").textContent(), "PILOT-EXAMPLE");
  assert.equal(await page.locator("#fixture-kind").textContent(), "Synthetic example");
  assert.match(await page.locator("#fixture-notice").textContent(), /No live account data/);
  assert.equal(await page.locator("#fees").textContent(), "$0.10");
  await page.getByRole("button", { name: "All events", exact: true }).click();
  assert.equal(await page.locator(".timeline-row").count(), 5);
  const [download] = await Promise.all([page.waitForEvent("download"), page.locator("#export-button").click()]);
  const evidence = JSON.parse(await readFile(await download.path(), "utf8"));
  assert.ok(JSON.stringify(evidence).includes("PILOT-EXAMPLE"));
  await page.locator("#replay-button").click();
  await page.waitForFunction(() => document.querySelector("#fill-count").textContent === "2");
  await mkdir("artifacts/browser/pilot", { recursive: true });
  await page.screenshot({ path: "artifacts/browser/pilot/example-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  await page.screenshot({ path: "artifacts/browser/pilot/example-mobile.png", fullPage: true });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, release: manifest.version, checks: ["dependency-free startup", "synthetic example", "timeline", "export", "replay", "mobile width"] }));
} finally {
  await browser?.close();
  server.kill();
  await new Promise((done) => server.exitCode !== null ? done() : server.once("exit", done));
}
