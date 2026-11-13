'use strict';
// VS Code's history-sizing hint, separate from immutable harness budget charges.
// cl100k is an approximation for local models, with 15% headroom. Ollama receipts
// remain the measured counts. No prompts or tokenized text are cached.
let encoder;
function textCount(text){
  if(!text)return 0;
  if(!encoder){const {Tiktoken}=require('./vendor/tokenizer/lite.cjs');encoder=new Tiktoken(require('./vendor/tokenizer/cl100k_base.cjs'));}
  let count=0;
  // Bound BPE work for long unbroken strings; boundary splits can overcount.
  for(let i=0;i<text.length;i+=4096)count+=encoder.encode(text.slice(i,i+4096),[],[]).length;
  return Math.ceil(count*1.15);
}
function count(input){
  if(typeof input==='string')return textCount(input);
  let total=4; // Approximate role/message framing.
  for(const part of input?.content||[]){
    if(typeof part.value==='string')total+=textCount(part.value);
    else total+=textCount(JSON.stringify(part));
  }
  if(input?.name)total+=textCount(input.name);
  return total;
}
module.exports={count,textCount};
