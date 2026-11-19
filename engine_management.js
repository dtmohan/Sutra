'use strict';
// Trusted extension-host entry to the Sutra-owned engine controller. The caller
// supplies approved settings; webviews and model output supply no commands.
const {execFile}=require('node:child_process');
const {isAbsolute,join}=require('node:path');

const MODEL=/^sutra:engine\/[a-z][a-z0-9_-]{0,63}$/;
const OPERATIONS=new Set(['catalog','status','load','unload','shutdown','setup']);
const clean=value=>typeof value==='string'&&value.length>0&&value.length<=4096&&
  !/[\x00-\x1f\x7f]/.test(value);
const absolute=value=>clean(value)&&isAbsolute(value);
const isEngineModel=value=>typeof value==='string'&&MODEL.test(value);

function create({bootstrapPython,python,registryPath,runtimeDir,models=[],extensionPath,
  environment=process.env,execute=execFile}={}){
  if(!clean(bootstrapPython)||!absolute(python)||!absolute(registryPath)||!absolute(runtimeDir)||
      !absolute(extensionPath)||!Array.isArray(models)||models.length>16||!models.every(isEngineModel)){
    throw new Error('The Sutra engine controller needs trusted local runtime settings.');
  }
  if(!environment||typeof environment!=='object'||Array.isArray(environment)){
    throw new Error('The Sutra engine controller environment is invalid.');
  }
  const assignments=[...new Set(models)];
  const helper=join(extensionPath,'harness','engine_control.py');
  const env={...environment,SUTRA_RUNTIME_DIR:runtimeDir,SUTRA_ENGINE_PYTHON:python,
    SUTRA_ENGINE_REGISTRY:registryPath,SUTRA_ENGINE_MODELS:JSON.stringify(assignments),
    SUTRA_ENGINE_ONLY:'1',PYTHONNOUSERSITE:'1'};
  // Inference must use the Sutra environment, never an inherited application's
  // Python bundle or modules supplied through the editor's startup shell.
  for(const key of Object.keys(env)){
    if(key==='VIRTUAL_ENV'||/^(?:DYLD_|LD_|STRATA_|PYTHON)/.test(key))delete env[key];
  }
  env.PYTHONNOUSERSITE='1';

  async function command(operation,ref,options={}){
    if(!OPERATIONS.has(operation))throw new Error('Unknown Sutra engine management operation.');
    if(operation==='load'){
      if(!isEngineModel(ref)||!assignments.includes(ref)){
        throw new Error('Choose a Sutra engine profile approved by the host.');
      }
    }else if(ref!==undefined){
      throw new Error('This engine operation does not accept a model reference.');
    }
    if(operation==='setup'&&options.approved!==true){
      throw new Error('Sutra engine dependency setup needs explicit user confirmation.');
    }
    const argv=['-B',helper,operation,...(operation==='load'?[ref]:[])];
    return new Promise((resolve,reject)=>{
      execute(bootstrapPython,argv,{env:{...env},shell:false,windowsHide:true,
        timeout:['load','setup'].includes(operation)?180000:15000,maxBuffer:524288},(error,stdout)=>{
        let result;
        try{
          if(typeof stdout!=='string'||Buffer.byteLength(stdout)>524288)throw new Error('Response bound');
          result=JSON.parse(stdout);
          if(!result||typeof result!=='object'||Array.isArray(result)||typeof result.ok!=='boolean'){
            throw new Error('Response shape');
          }
        }catch{
          if(error?.killed||error?.code==='ETIMEDOUT')return resolve({ok:false,status:'uncertain',
            error:'The Sutra engine operation timed out. Inspect runtime status before retrying.'});
          return reject(new Error('The Sutra engine controller did not return a valid bounded response.'));
        }
        // A resource pause may have a nonzero exit and is still a useful host
        // observation. Never replay a load after an ambiguous transport result.
        if(error&&result.ok!==false)return resolve({ok:false,status:'uncertain',
          error:'The Sutra engine operation was interrupted. Inspect runtime status before retrying.'});
        resolve(result);
      });
    });
  }
  return Object.freeze({command});
}

module.exports={create,isEngineModel};
