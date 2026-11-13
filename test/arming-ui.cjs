'use strict';
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.KARMA_PLAYWRIGHT);
const file=process.env.SUTRA_ARM_RESULT;
(async()=>{
 let browser;for(let i=0;i<100;i++){try{browser=await chromium.connectOverCDP('http://127.0.0.1:'+process.env.SUTRA_ARM_PORT);break;}catch{await new Promise(r=>setTimeout(r,200));}}
 if(!browser)throw Error('VS Code debugging endpoint did not open');
 const page=browser.contexts()[0].pages()[0];let confirmed=false;
 const until=Date.now()+60000;
 while(Date.now()<until&&!fs.existsSync(file)){
  const button=page.getByRole('button',{name:'Arm Sutra',exact:true});
  if(await button.count()&&await button.first().isVisible()){
   if(confirmed)throw Error('Unexpected second arming confirmation');
   await page.screenshot({path:path.join(path.dirname(file),'VSCode-arm-confirmation.png')});
   await button.first().click();confirmed=true;
  }
  await new Promise(r=>setTimeout(r,200));
 }
 if(!fs.existsSync(file))throw Error('Native arming test timed out');
 const report=JSON.parse(fs.readFileSync(file,'utf8'));
 if(!confirmed||!report.passed)throw Error(JSON.stringify(report));
 console.log(JSON.stringify(report));await browser.close();
})().catch(e=>{console.error(e);process.exitCode=1;});
