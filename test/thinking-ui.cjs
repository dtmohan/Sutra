const {chromium}=require(process.env.KARMA_PLAYWRIGHT),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
 const file=process.env.SUTRA_THINK_RESULT;let browser;
 for(let i=0;i<80&&!browser;i++){try{browser=await chromium.connectOverCDP('http://127.0.0.1:'+process.env.SUTRA_THINK_PORT);}catch{await new Promise(r=>setTimeout(r,200));}}
 assert(browser);const seen=new Set();
 for(let i=0;i<350;i++){
  const r=fs.existsSync(file)?JSON.parse(fs.readFileSync(file)):{};if(r.error)throw Error(r.error);if(r.passed){console.log('PASS native thinking API for parent and child after model switch; no proposed API flags');return;}
  await new Promise(r=>setTimeout(r,200));
 }
 throw Error('Native thinking test timed out');
})().catch(e=>{console.error(e);process.exitCode=1;});
