'use strict';
// Exercise the shipped model-manager webview with native-runtime fixture states.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.KARMA_PLAYWRIGHT||'playwright');
const root=path.resolve(__dirname,'..');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.KARMA_BROWSER?{executablePath:process.env.KARMA_BROWSER}:{})});
 try{
  const page=await browser.newPage({viewport:{width:1060,height:1000}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  let html=fs.readFileSync(path.join(root,'media/panel.html'),'utf8').replaceAll('NONCE_VALUE','test-nonce');
  html=html.replace('<script nonce="test-nonce" src="USAGE_URI"></script>',()=>'<script nonce="test-nonce">'+fs.readFileSync(path.join(root,'media/token_usage.js'),'utf8')+'</script>');
  html=html.replace('<script nonce="test-nonce" src="SCRIPT_URI"></script>',()=>'<script nonce="test-nonce">window.sent=[];function acquireVsCodeApi(){return {postMessage:m=>sent.push(m),setState(){},getState:()=>({})}}\n'+fs.readFileSync(path.join(root,'media/panel.js'),'utf8')+'</script>');
  await page.setContent(html);
  const send=data=>page.evaluate(value=>window.dispatchEvent(new MessageEvent('message',{data:value})),data);
  await send({type:'show-section',section:'models'});
  const state={available:true,busy:false,runtime_state:'ready',selected_profile:'native-1',start_on_startup:true,
   message:'Native Strata is ready. High keeps the selected profile.',
   profiles:[{id:'native-1',name:'Sutra High · 32K',model:'qwen-native',context_tokens:32768,selected:true},
    {id:'native-2',name:'<img src=x onerror=alert(1)>',model:'other-native',context_tokens:8192}],
   models:[{id:'disk-1',name:'Installed native model',context_tokens:32768}]};
  await send({type:'strata-state',data:state});
  await page.locator('#strata-profile').scrollIntoViewIfNeeded();
  assert.equal(await page.getByRole('combobox',{name:'Native runtime profile'}).inputValue(),'native-1');
  assert(await page.getByLabel('Start the selected native profile when VS Code opens').isChecked());
  await page.locator('#strata-profile').selectOption('native-2');
  await page.getByRole('button',{name:'Load selected',exact:true}).click();
  assert.deepEqual(await page.evaluate(()=>sent.at(-1)),{op:'strata_load',id:'native-2'});
  assert.equal(await page.locator('#strata-manager').getAttribute('aria-busy'),'true');
  assert(await page.locator('#strata-load').isDisabled());
  await send({type:'strata-state',data:{...state,busy:true,runtime_state:'starting',message:'Loading native weights…'}});
  assert.equal(await page.locator('#strata-runtime-state').textContent(),'Loading');
  assert(await page.locator('#strata-autostart').isDisabled());
  await send({type:'strata-state',data:state});
  assert.equal(await page.locator('#strata-profile').inputValue(),'native-2');
  assert.equal(await page.locator('#strata-manager img').count(),0);
  await send({type:'busy',value:true});assert(await page.locator('#strata-load').isDisabled());
  await send({type:'busy',value:false});assert(await page.locator('#strata-load').isEnabled());
  await page.locator('#strata-autostart').uncheck();
  assert.deepEqual(await page.evaluate(()=>sent.at(-1)),{op:'strata_autostart',enabled:false});
  await send({type:'strata-state',data:{...state,start_on_startup:false}});
  await page.locator('#strata-manager details').evaluate(element=>element.open=true);
  await page.getByRole('button',{name:'Add installed model…',exact:true}).click();
  assert.deepEqual(await page.evaluate(()=>sent.at(-1)),{op:'strata_add_config'});
  await send({type:'strata-state',data:state});
  await page.locator('#strata-profile').focus();
  await page.keyboard.press('Tab');
  assert(await page.locator('#strata-load').evaluate(element=>element===document.activeElement));
  const output=process.env.KARMA_UI_OUTPUT;
  if(output){fs.mkdirSync(output,{recursive:true});await page.screenshot({path:path.join(output,'Strata-model-manager.png'),fullPage:true});}
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Native manager fits a narrow VS Code panel');
  assert.deepEqual(errors,[]);
  console.log('PASS native model-manager browser keyboard, startup preference, loading states, safe labels and narrow layout');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
