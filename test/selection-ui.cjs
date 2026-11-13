const {chromium}=require(process.env.KARMA_PLAYWRIGHT||'playwright'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{const root=path.join(__dirname,'..'),config=JSON.parse(fs.readFileSync(path.join(root,'selection-paths.json'))),file=path.join(root,'selection-vscode.json');let b;
for(let i=0;i<100&&!b;i++){try{b=await chromium.connectOverCDP('http://127.0.0.1:'+config.port);}catch{await new Promise(r=>setTimeout(r,200));}}
if(!b)throw Error('No isolated browser');let done=new Set();
try{for(let i=0;i<700;i++){
 const r=fs.existsSync(file)?JSON.parse(fs.readFileSync(file)):{};if(r.phase==='failed')throw Error(r.error);if(r.phase==='restart_pending'){console.log('RESTART required');break;}if(r.passed){console.log('PASS selection, new chat and reload');break;}
 const p=b.contexts().flatMap(c=>c.pages()).find(p=>p.url().includes('workbench'));if(!p){await new Promise(r=>setTimeout(r,200));continue;}
 if(['selected','new_chat','after_reload','after_restart'].includes(r.phase)&&!done.has(r.phase)){
  await p.getByRole('button',{name:'Models, Sutra Auto',exact:true}).waitFor({timeout:15000});
  if(r.phase==='selected'){
   await p.getByRole('button',{name:'Models, Sutra Auto',exact:true}).click();
   for(const name of ['Sutra Auto','Sutra Low','Sutra Mid','Sutra High'])assert(await p.getByText(name,{exact:true}).last().isVisible());
   await p.getByText('Sutra High',{exact:true}).last().click({force:true});await p.getByRole('button',{name:'Models, Sutra High',exact:true}).waitFor();
   await p.getByRole('button',{name:'Models, Sutra High',exact:true}).click();await p.getByText('Sutra Auto',{exact:true}).last().click({force:true});
  }
  await p.screenshot({path:path.join(root,'selection-'+r.phase+'.png')});console.log('Verified '+r.phase);done.add(r.phase);fs.writeFileSync(file+'.'+({selected:'selected_checked',new_chat:'new_checked',after_reload:'reload_checked',after_restart:'restart_checked'}[r.phase]),'verified');
 }
 await new Promise(r=>setTimeout(r,200));
}}finally{await b.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
