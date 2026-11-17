// Portable, measured memory/CPU profile. Missing thermal signals stay unknown.
'use strict';
const os=require('node:os'),fs=require('node:fs');
const {execFileSync}=require('node:child_process');
function darwinAvailable(text){
 const page=Number(text.match(/page size of (\d+) bytes/)?.[1]);
 const free=Number(text.match(/^Pages free:\s+(\d+)\./m)?.[1]);
 const inactive=Number(text.match(/^Pages inactive:\s+(\d+)\./m)?.[1]);
 if(!Number.isFinite(page)||page<=0||!Number.isFinite(free)||!Number.isFinite(inactive))throw new Error('Invalid vm_stat memory sample');
 return page*(free+inactive);
}
let previous;
function sample(){
 const cpus=os.cpus(),now=cpus.reduce((a,c)=>({idle:a.idle+c.times.idle,total:a.total+Object.values(c.times).reduce((x,y)=>x+y,0)}),{idle:0,total:0});
 const cpu=previous&&now.total>previous.total?100*(1-(now.idle-previous.idle)/(now.total-previous.total)):null;previous=now;
 let available=os.freemem(),method='os.freemem';
 if(process.platform==='darwin'){
  try{available=darwinAvailable(execFileSync('/usr/bin/vm_stat',[],{encoding:'utf8',timeout:1000,maxBuffer:16384}));method='vm_stat.free+inactive';}
  catch{available=null;method='vm_stat.unavailable';}
 }
 if(process.platform==='linux')try{const m=fs.readFileSync('/proc/meminfo','utf8').match(/^MemAvailable:\s+(\d+) kB$/m);if(m){available=Number(m[1])*1024;method='proc.MemAvailable';}}catch{}
 return {profile:'portable-memory-v1',platform:process.platform,wall_time:Date.now()/1000,physical_memory_bytes:os.totalmem(),available_bytes:available,cpu_busy_pct:cpu,thermal_state:null,memory_pressure_raw:null,temperature_celsius:null,memory_measurement:method,unavailable:['thermal_state','memory_pressure','temperature_celsius']};
}
if(require.main===module){
 console.log(JSON.stringify(sample()));
 if(process.argv[2]!=='--once')setInterval(()=>console.log(JSON.stringify(sample())),1000);
}
module.exports={sample,darwinAvailable};
