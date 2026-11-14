'use strict';
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.KARMA_PLAYWRIGHT);
const file=process.env.SUTRA_ARM_RESULT;
(async()=>{
 let browser;for(let i=0;i<100;i++){try{browser=await chromium.connectOverCDP('http://127.0.0.1:'+process.env.SUTRA_ARM_PORT);break;}catch{await new Promise(r=>setTimeout(r,200));}}
 if(!browser)throw Error('VS Code debugging endpoint did not open');
 const page=browser.contexts()[0].pages()[0];const handled=new Set();
 const until=Date.now()+60000;
 while(Date.now()<until&&!fs.existsSync(file)){
  const button=page.getByRole('button',{name:'Arm Sutra',exact:true});
  if(await button.count()&&await button.first().isVisible()){
   const phase=fs.existsSync(file+'.phase')?fs.readFileSync(file+'.phase','utf8'):'';
   if(!handled.has(phase)){
    if(!['decline','approve'].includes(phase))throw Error('Unexpected arming confirmation in '+phase);
    await page.screenshot({path:path.join(path.dirname(file),'VSCode-arm-'+phase+'.png')});
    if(phase==='decline')await page.getByRole('button',{name:'Cancel',exact:true}).click();
    else await button.first().click();
    handled.add(phase);
   }
  }
  await new Promise(r=>setTimeout(r,200));
 }
 if(!fs.existsSync(file))throw Error('Native arming test timed out');
 const report=JSON.parse(fs.readFileSync(file,'utf8'));
 if(handled.size!==2||!report.passed)throw Error(JSON.stringify(report));
 console.log(JSON.stringify(report));await browser.close();
})().catch(e=>{console.error(e);process.exitCode=1;});
