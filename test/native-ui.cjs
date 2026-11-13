const {chromium}=require(process.env.KARMA_PLAYWRIGHT||'playwright');
const fs=require('node:fs'),path=require('node:path');
(async()=>{
 let b;for(let i=0;i<100&&!b;i++){try{b=await chromium.connectOverCDP('http://127.0.0.1:'+JSON.parse(fs.readFileSync(path.join(__dirname,'../native-smoke-paths.json'),'utf8')).port);}catch{await new Promise(r=>setTimeout(r,200));}}if(!b)throw Error('No isolated VS Code debugger');
 let p;for(let i=0;i<100&&!p;i++){p=b.contexts().flatMap(c=>c.pages()).find(p=>p.url().includes('workbench'));if(!p)await new Promise(r=>setTimeout(r,200));}if(!p)throw Error('No isolated workbench page');let last='';
 for(let i=0;i<1800;i++){
  if(p.isClosed())break;const text=await p.locator('body').innerText();
  if(text!==last){fs.writeFileSync(path.join(__dirname,'../native-ui.txt'),text);last=text;}
  for(const label of ['Arm current task','Arm this task']){const target=p.getByText(label,{exact:true});if(await target.count()===1&&await target.isVisible()){console.log('Select '+label);await target.click();}}
  for(const label of ['Yes','Apply edit','Run test']){const target=p.getByRole('button',{name:label,exact:true});if(await target.count()===1&&await target.isVisible()&&(label!=='Yes'||/Sutra: (apply edit|run test)/.test(text))){console.log('Confirmed '+label+' after preview');await p.screenshot({path:path.join(__dirname,'../native-confirm-'+label.replace(' ','-')+'.png')});await target.click();}}
  await new Promise(r=>setTimeout(r,200));
 }
 await b.close();
})().catch(e=>{console.error(e);process.exitCode=1;});
