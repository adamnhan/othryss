import assert from "node:assert/strict";
import {spawn,execFileSync} from "node:child_process";
import {mkdir} from "node:fs/promises";
import {setTimeout as delay} from "node:timers/promises";
import {chromium} from "playwright";
await mkdir("artifacts/browser",{recursive:true});
const db=`artifacts/browser/alerts-test-${Date.now()}.sqlite`,alerts=db+".alerts";
const seed=JSON.parse(execFileSync("python",["scripts/seed_alerts_browser.py",db,alerts],{encoding:"utf8",windowsHide:true}));
const base="http://127.0.0.1:8885";
const server=spawn("python",["-m","othryss.server","--port","8885","--db",db,"--alerts-db",alerts,"--reference-db",db+".missing"],{windowsHide:true,stdio:"ignore"});
let browser;
try {
  for(let i=0;i<50;i++){try{if((await fetch(base).then(async response => { await response.arrayBuffer(); return response; })).ok)break;}catch{}await delay(100);}
  browser=await chromium.launch({channel:process.env.BROWSER_CHANNEL??"msedge",headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1050}});
  const errors=[];page.on("pageerror",e=>errors.push(e.message));
  await page.goto(`${base}#notifications`);await page.locator("#history-account").selectOption(seed.scope);
  await page.waitForFunction(()=>document.getElementById("alerts-content").textContent.includes("Provider accepted"));
  const text=await page.locator("#alerts-content").textContent();
  assert.match(text,/Retry scheduled/);assert.match(text,/Delivery unknown/);
  assert.doesNotMatch(text,/SECRET-CANARY|https:\/\/discord|PRIVATE REVIEW NOTE/);
  await page.locator("#alerts-content details").last().locator("summary").click();
  await page.locator("#alerts-panel").screenshot({path:"artifacts/browser/alerts-synthetic.png"});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.locator("#history-account").selectOption(seed.other);
  await page.waitForFunction(()=>document.getElementById("alerts-content").textContent.includes("No notification destinations configured"));
  assert.doesNotMatch(await page.locator("#alerts-content").textContent(),/incident-1|Provider accepted/);
  assert.equal((await fetch(`${base}/api/history/alerts?scope=missing`)).status,400);
  await page.route("**/api/history/alerts?**",r=>r.fulfill({status:503,contentType:"application/json",body:'{"error":"unavailable"}'}));
  await page.locator("#history-refresh").click();
  await page.waitForFunction(()=>document.getElementById("alerts-content").textContent.includes("Alert delivery status unavailable"));
  assert.deepEqual(errors,[]);
  console.log("Alerts browser passed: shared queue API, provider acceptance, retry, unknown outcome, attempt history, secret exclusion, scope switch, mobile and failure state. No external sends.");
} finally {await browser?.close();server.kill();}
