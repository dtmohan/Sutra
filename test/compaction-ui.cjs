// Operate only the isolated profile created by launch_karma_compaction.py.
const {chromium}=require(process.env.KARMA_PLAYWRIGHT||'playwright'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
 const root=path.resolve(__dirname,'..'),cfg=JSON.parse(fs.readFileSync(path.join(root,'compaction-paths.json'))),file=path.join(root,'compaction-vscode.json');
 const b=await chromium.connectOverCDP('http://127.0.0.1:'+cfg.port);let captured=false;
 try{for(let i=0;i<1500;i++){
  const report=JSON.parse(fs.readFileSync(file));if(report.phase==='failed')throw Error(report.error);
  for(const p of b.contexts().flatMap(c=>c.pages()))if(p.url().includes('workbench')){
   const dialog=p.getByRole('dialog');if(report.phase==='reload_pending'&&await dialog.count()&&(await dialog.innerText()).includes('reload the window'))await dialog.getByRole('button',{name:'Yes',exact:true}).click();
   if(!captured)for(const frame of p.frames())try{
    if(await frame.locator('#canvas .node').count()===9){assert.equal(await frame.locator('#error').textContent(),'');assert(/Valerion/i.test(await frame.locator('#canvas').innerText()));await frame.locator('body').screenshot({path:path.join(root,'Native-listening-chain.png')});captured=true;}
   }catch(e){if(!/detached|destroyed/.test(e.message))throw e;}
  }
  if(report.passed){console.log('PASS native compaction and reload; graph captured: '+captured);return;}
  await new Promise(r=>setTimeout(r,400));
 }throw Error('Isolated compaction test timed out');}finally{await b.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
