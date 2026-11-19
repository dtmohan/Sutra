'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.KARMA_PLAYWRIGHT);
const file=process.env.SUTRA_ARM_RESULT,sleep=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 let browser;for(let i=0;i<100;i++){try{browser=await chromium.connectOverCDP('http://127.0.0.1:'+process.env.SUTRA_ARM_PORT);break;}catch{await sleep(200);}}
 if(!browser)throw Error('VS Code debugging endpoint did not open');
 const page=browser.contexts()[0].pages()[0],draft='PRESERVE THIS EXISTING CHAT DRAFT CED-724';let prepared=false,confirmed=0;
 const until=Date.now()+600000;
 while(Date.now()<until){
  const report=fs.existsSync(file)?JSON.parse(fs.readFileSync(file,'utf8')):{};
  if(report.phase==='failed')throw Error(JSON.stringify(report));
  if(report.phase==='chat_open'&&!prepared){
   const inputs=page.getByRole('textbox').filter({visible:true});
   if(await inputs.count()){
    await inputs.last().focus();await inputs.last().pressSequentially(draft);prepared=true;fs.writeFileSync(file+'.draft','ready');
    await page.screenshot({path:path.join(path.dirname(file),'VSCode-before-arm.png')});
   }
  }
  const dialog=page.locator('.monaco-dialog-box').filter({hasText:'Workspace access for Sutra'});
  const arm=dialog.getByRole('button',{name:'Read only',exact:true});
  if(await arm.count()&&await arm.first().isVisible()){
   assert.equal(confirmed,0,'Unexpected second arming confirmation');
   await page.screenshot({path:path.join(path.dirname(file),'VSCode-inline-confirmation.png')});
   await arm.first().click();confirmed++;
  }
  if(report.phase==='complete'){
   assert(report.passed);assert(prepared);assert.equal(confirmed,1);
   const visible=(await page.locator('body').innerText()).replace(/\s/g,'');
   assert(visible.includes(draft.replace(/\s/g,'')),'Arming replaced the current chat or lost its draft');
   report.checks.push('Existing native chat draft survives in-place arming and model switch; no replacement chat opens');
   fs.writeFileSync(file,JSON.stringify(report,null,2));await page.screenshot({path:path.join(path.dirname(file),'VSCode-after-arm.png')});
   console.log(JSON.stringify(report));await browser.close();return;
  }
  await sleep(150);
 }
 throw Error('Inline arming test timed out. Visible UI: '+(await page.locator('body').innerText()).slice(-4000));
})().catch(e=>{console.error(e);process.exit(1);});
