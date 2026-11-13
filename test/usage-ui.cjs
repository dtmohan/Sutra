const {chromium}=require(process.env.KARMA_PLAYWRIGHT||'playwright'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
 const file=process.env.SUTRA_USAGE_RESULT;let browser;
 for(let i=0;i<100&&!browser;i++){try{browser=await chromium.connectOverCDP('http://127.0.0.1:'+process.env.SUTRA_USAGE_PORT);}catch{await new Promise(r=>setTimeout(r,200));}}
 if(!browser)throw Error('Isolated VS Code did not open');
 const done=new Set();let passed=false;
 for(let i=0;i<300;i++){
  const r=fs.existsSync(file)?JSON.parse(fs.readFileSync(file)):{};if(r.phase==='failed')throw Error(r.error);if(r.passed){passed=true;break;}
  const page=browser.contexts().flatMap(c=>c.pages()).find(p=>p.url().includes('workbench'));
  if(page&&['opened','hidden'].includes(r.phase)&&!done.has(r.phase)){
   const status=page.locator('.statusbar-item').filter({hasText:'Sutra Usage'});await status.waitFor({timeout:15000});
   if(r.phase==='hidden')await status.click();
   const panel=page.locator('.part.panel');await panel.waitFor({state:'visible',timeout:15000});
   await panel.getByRole('tab',{name:/Sutra Usage/i}).waitFor({state:'visible',timeout:15000});
   let frame;for(let j=0;j<100&&!frame;j++){for(const f of page.frames()){if(await f.locator('#scope').count())frame=f;}if(!frame)await new Promise(r=>setTimeout(r,100));}
   assert(frame,'Usage webview must be present');await frame.getByRole('heading',{name:'Token usage',exact:true}).waitFor();await frame.getByText('No model calls yet',{exact:true}).waitFor({timeout:20000});
   assert.equal(await frame.locator('#input').textContent(),'—');assert.equal(await frame.locator('#calls').textContent(),'0');assert(!(await frame.locator('#error').isVisible()));
   assert(!/View container.*does not exist|property.*is mandatory/i.test(await page.locator('body').innerText()),'Extension contributions must be valid');
   await page.screenshot({path:path.join(path.dirname(file),'VSCode-usage-'+r.phase+'.png')});
   fs.writeFileSync(file+'.'+(r.phase==='hidden'?'reopened':'opened'),'ok');done.add(r.phase);
  }
  await new Promise(r=>setTimeout(r,200));
 }
 assert(passed,'Native UI checks did not finish');console.log('PASS packaged usage view and status-bar navigation; no inference');
})().catch(e=>{console.error(e);process.exitCode=1;});
