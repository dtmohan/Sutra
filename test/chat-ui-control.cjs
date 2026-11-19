const {chromium}=require(process.env.KARMA_PLAYWRIGHT||'playwright'),fs=require('node:fs'),path=require('node:path');
(async()=>{const root=path.join(__dirname,'..');const config=JSON.parse(fs.readFileSync(path.join(root,'native-smoke-paths.json')));let b,p;
for(let i=0;i<100&&!b;i++){try{b=await chromium.connectOverCDP('http://127.0.0.1:'+config.port);}catch{await new Promise(r=>setTimeout(r,200));}}
for(let i=0;i<100&&!p;i++){p=b.contexts().flatMap(c=>c.pages()).find(p=>p.url().includes('workbench'));if(!p)await new Promise(r=>setTimeout(r,200));}if(!p)throw Error('No isolated workbench');
for(let i=0;i<100;i++){
 const permissions=p.locator('.monaco-dialog-box').filter({hasText:'Workspace access for Sutra'});
 if(await permissions.count()&&await permissions.first().isVisible())throw Error('The saved workspace profile should be restored without a dialog');
 const r=JSON.parse(fs.readFileSync(path.join(root,'native-chat-ui.json')));if(r.phase==='ready')break;await new Promise(r=>setTimeout(r,200));
}
console.log('READY\n'+(await p.locator('body').innerText()).slice(-3000));console.log('TEXTBOXES',await p.getByRole('textbox').evaluateAll(els=>els.map(e=>({tag:e.tagName,role:e.getAttribute('role'),label:e.getAttribute('aria-label')}))));
await b.close();
})().catch(e=>{console.error(e);process.exitCode=1;});
