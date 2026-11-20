'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.KARMA_PLAYWRIGHT);
const file=process.env.SUTRA_PERMISSION_RESULT,sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{
 let browser;for(let i=0;i<150;i++){try{browser=await chromium.connectOverCDP('http://127.0.0.1:'+process.env.SUTRA_PERMISSION_PORT);break;}catch{await sleep(200);}}
 assert(browser,'VS Code debugging endpoint did not open');let page;const handled=new Set();
 try{
  for(let i=0;i<1000;i++){
   const report=fs.existsSync(file)?JSON.parse(fs.readFileSync(file,'utf8')):{};
   page=browser.contexts().flatMap(c=>c.pages()).find(p=>p.url().includes('workbench'));
   if(!page){await sleep(100);continue;}
   const text=await page.locator('body').innerText();fs.writeFileSync(file+'.ui.txt',text);
   if(report.phase==='failed')throw Error(report.error);
   if(report.phase==='host_blocked'){
    await page.screenshot({path:path.join(path.dirname(file),'host-agent-blocked.png')});
    console.log(JSON.stringify({passed:false,blocked:true,reason:report.reason,ui:text}));return;
   }
   if(report.phase==='approve_native_permission'&&!handled.has(report.phase)){
    const allow=page.getByRole('button',{name:/Allow this tool to run in this session|^(Allow|Continue|Run)$/}).filter({visible:true});
    if(await allow.count()===1&&await allow.isVisible()){
     assert(/allow workspace reading|read files in.*workspace|Workspace access needed/s.test(text),'Native consent details missing');
     await page.screenshot({path:path.join(path.dirname(file),'01-native-tool-consent.png')});await allow.click();handled.add(report.phase);
    }
   }
   if(report.phase==='deny_native_permission'&&!handled.has(report.phase)){
    const cancel=page.getByRole('button',{name:/Proceed without running this tool|^(Skip|Cancel|Deny)$/}).filter({visible:true});
    if(await cancel.count()===1&&await cancel.isVisible()){
     await page.screenshot({path:path.join(path.dirname(file),'03-native-tool-denial.png')});await cancel.click();
     fs.writeFileSync(file+'.denied','denied');handled.add(report.phase);
    }
   }
   if(['revocation_decline','revocation_approve'].includes(report.phase)&&!handled.has(report.phase)){
    const dialog=page.locator('.monaco-dialog-box').filter({hasText:'Sutra:'}).filter({visible:true});
    if(await dialog.count()===1&&await dialog.isVisible()){
     await page.screenshot({path:path.join(path.dirname(file),report.phase+'.png')});
     if(report.phase==='revocation_decline'){await page.keyboard.press('Escape');fs.writeFileSync(file+'.regrant-declined','declined');}
     else await dialog.getByRole('button',{name:'Allow workspace access',exact:true}).click();
     handled.add(report.phase);
    }
   }
   if(report.phase==='host_checks_complete'&&!handled.has(report.phase)){
    assert(!text.includes('Copy resume instruction'));assert(!text.includes('send continue'));
    await page.screenshot({path:path.join(path.dirname(file),'04-automatic-continuation-and-denial.png')});
    fs.writeFileSync(file+'.checked','checked');handled.add(report.phase);
   }
   if(report.phase==='complete'){assert(report.passed);console.log(JSON.stringify({passed:true,scope:report.scope,checks:report.checks}));return;}
   await sleep(150);
  }
  throw Error('Native permission-tool UI journey timed out');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
