'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.KARMA_PLAYWRIGHT);
const file=process.env.SUTRA_PERMISSION_RESULT,sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{
 let browser;for(let i=0;i<150;i++){try{browser=await chromium.connectOverCDP('http://127.0.0.1:'+process.env.SUTRA_PERMISSION_PORT);break;}catch{await sleep(200);}}
 assert(browser,'VS Code debugging endpoint did not open');
 const handled=new Set();let page;
 const shot=async name=>page.screenshot({path:path.join(path.dirname(file),name+'.png')});
 try{
  for(let i=0;i<1000;i++){
   const report=fs.existsSync(file)?JSON.parse(fs.readFileSync(file,'utf8')):{};
   if(report.phase==='failed')throw Error(report.error);
   page=browser.contexts().flatMap(context=>context.pages()).find(page=>page.url().includes('workbench'));
   if(!page){await sleep(150);continue;}
   if(report.phase==='dismiss_permission'&&!handled.has(report.phase)){
    const dialog=page.locator('.monaco-dialog-box').filter({hasText:'Workspace access for Sutra'});
    await dialog.waitFor();await shot('01-permission-after-settled-request');await page.keyboard.press('Escape');
    await dialog.waitFor({state:'hidden'});await shot('02-dismissed-request-retained');
    fs.writeFileSync(file+'.dismissed','dismissed');handled.add(report.phase);
   }
   if(report.phase==='copy_resume'&&!handled.has(report.phase)){
    const copy=page.getByRole('button',{name:'Copy resume instruction',exact:true});await copy.waitFor();await shot('03-resume-copy-no-chat-navigation');await copy.click();handled.add(report.phase);
   }
   if(report.phase==='approve_permission'&&!handled.has(report.phase)){
    const dialog=page.locator('.monaco-dialog-box').filter({hasText:'Workspace access for Sutra'});
    await dialog.getByRole('button',{name:'Read only',exact:true}).waitFor();await shot('04-command-opens-permission-dialog');
    await dialog.getByRole('button',{name:'Read only',exact:true}).click();handled.add(report.phase);
   }
   if(report.phase==='host_checks_complete'&&!handled.has(report.phase)){
    const text=await page.locator('body').innerText();
    assert(report.responses[0].text.includes('Workspace access needed'),'The settled provider response must contain an actionable access request');
    assert(report.responses.at(-1).text.includes('deterministic UI test report'),'The same provider history must contain the resumed result');
    assert(!text.includes('the model stopped before finishing'),'Permission dismissal must not become a model-interruption failure');
    assert(!text.includes('Sorry, your request failed'),'The permission journey must end without a generic failed response');
    await shot('05-same-chat-resumed-report');fs.writeFileSync(file+'.transcript.txt',text);fs.writeFileSync(file+'.checked','checked');handled.add(report.phase);
   }
   if(report.phase==='complete'){assert(report.passed);console.log(JSON.stringify({passed:true,scope:report.scope,checks:report.checks}));return;}
   await sleep(150);
  }
  throw Error('Permission journey timed out. Last UI: '+(page?await page.locator('body').innerText():'no window'));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
