'use strict';

// Run against a normal isolated development window with CDP enabled.
// VS Code extension-test mode suppresses modal dialogs.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.KARMA_PLAYWRIGHT);
const resultPath = process.env.SUTRA_ARM_RESULT;
const port = process.env.SUTRA_ARM_PORT;
const sleep = ms => new Promise(resolve => setTimeout(resolve,ms));

async function connect() {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    try {
      return await chromium.connectOverCDP('http://127.0.0.1:'+port);
    } catch {
      await sleep(200);
    }
  }
  throw new Error('VS Code debugging endpoint did not open');
}

async function workbench(browser) {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    const page = browser.contexts().flatMap(context => context.pages())
      .find(candidate => candidate.url().includes('workbench'));
    if (page) return page;
    await sleep(200);
  }
  throw new Error('Isolated VS Code workbench did not open');
}

async function command(page, title) {
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  const palette = page.locator('.quick-input-widget:visible');
  await palette.locator('input').fill('>'+title);
  await palette.locator('.quick-input-list .monaco-list-row').filter({hasText:title}).first().click();
}

async function main() {
  const report = {passed:false, checks:[]};
  let browser;
  try {
    browser = await connect();
    let page = await workbench(browser);
    const permissions = 'Sutra: Workspace Permissions…';
    const dialog = () => page.locator('.monaco-dialog-box').filter({hasText:'Workspace permissions for Sutra'});
    const status = label => page.locator('.statusbar').getByText(label,{exact:false}).waitFor({state:'visible'});
    const screenshot = name => page.screenshot({path:path.join(path.dirname(resultPath),name)});

    await status('Sutra · chat only');
    await command(page,permissions);
    await dialog().getByRole('button',{name:'Cancel',exact:true}).click();
    await status('Sutra · chat only');
    report.checks.push('Cancel in the permission dialog leaves workspace access off');

    await command(page,permissions);
    await screenshot('Workspace-permissions.png');
    await dialog().getByRole('button',{name:'Read only',exact:true}).click();
    await status('Sutra · Read only');
    report.checks.push('Read only enables workspace access through the native dialog');

    await command(page,permissions);
    await dialog().getByRole('button',{name:'Cancel',exact:true}).click();
    await status('Sutra · Read only');
    report.checks.push('Cancel while configuring an existing grant preserves it');

    async function reload() {
      const navigation = page.waitForEvent('framenavigated',{
        predicate:frame => frame === page.mainFrame(), timeout:30000,
      });
      await command(page,'Developer: Reload Window');
      await navigation;
      page = await workbench(browser);
    }

    await reload();
    await status('Sutra · Read only');
    assert.equal(await dialog().count(),0);
    report.checks.push('An actual VS Code window reload restores workspace authorization without a dialog');

    await command(page,'Sutra: Revoke Workspace Access');
    await status('Sutra · chat only');
    await reload();
    await status('Sutra · chat only');
    assert.equal(await dialog().count(),0);
    report.checks.push('Revocation remains in force after reloading VS Code again');
    report.passed = true;
    console.log(JSON.stringify(report));
  } catch (error) {
    report.error = error.stack;
    throw error;
  } finally {
    fs.writeFileSync(resultPath,JSON.stringify(report,null,2)+'\n');
    if (browser) await browser.close();
  }
}

main().catch(error => {console.error(error); process.exitCode=1;});
