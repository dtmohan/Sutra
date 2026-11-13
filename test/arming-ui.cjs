const {chromium}=require(process.env.KARMA_PLAYWRIGHT||'playwright'),fs=require('node:fs'),path=require('node:path');
(async()=>{const root=path.join(__dirname,'..'),file=path.join(root,'arming-vscode.json'),cfg=JSON.parse(fs.readFileSync(path.join(root,'arming-paths.json')));let b;
 for(let i=0;i<100&&!b;i++){try{b=await chromium.connectOverCDP('http://127.0.0.1:'+cfg.port);}catch{await new Promise(r=>setTimeout(r,200));}}if(!b)throw Error('No isolated workbench');
 const done=new Set();
 try{for(let i=0;i<2400;i++){
  const r=fs.existsSync(file)?JSON.parse(fs.readFileSync(file)):{};if(r.phase==='failed')throw Error(r.error);if(r.passed){console.log('PASS arming and live response');return;}
  const p=b.contexts().flatMap(c=>c.pages()).find(p=>p.url().includes('workbench'));if(!p){await new Promise(r=>setTimeout(r,200));continue;}
  if(r.phase==='first_arming'&&!done.has('first')){const target=p.getByText('Start a read-only workspace review',{exact:true});if(await target.isVisible()){done.add('first');await p.screenshot({path:path.join(root,'Arming-pending.png')});fs.writeFileSync(file+'.picker_open','visible');}}
  if(r.phase==='cancel_first'&&!done.has('cancel')){done.add('cancel');await p.keyboard.press('Escape');console.log('Cancelled pending setup');}
  if(r.phase==='arming'){
   const choose=p.getByText('Start a read-only workspace review',{exact:true});if(!done.has('choose')&&await choose.isVisible()){done.add('choose');await choose.click();console.log('Selected read-only review');}
   const goal=p.locator('.quick-input-widget input');if(done.has('choose')&&!done.has('goal')&&await goal.isVisible()&&(await goal.inputValue()).startsWith('Review this workspace')){done.add('goal');await goal.fill('Explain calculator.py and its add function. No source changes, tests, network or new dependencies.');await goal.press('Enter');console.log('Entered task goal');}
   const confirm=p.getByText('Arm this task',{exact:true});if(!done.has('confirm')&&await confirm.isVisible()){done.add('confirm');await p.screenshot({path:path.join(root,'Arming-confirmation.png')});await confirm.click();console.log('Confirmed read-only task scope');}
  }
  await new Promise(r=>setTimeout(r,200));
 }throw Error('Timed out waiting for arming/live test');}finally{await b.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
