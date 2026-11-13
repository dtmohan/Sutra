// One fresh compiler Program per request. Only supplied project files and the
// trusted compiler's standard libraries are visible; no project config/plugins.
'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const input=JSON.parse(fs.readFileSync(0,'utf8')),ts=require(input.typescript);
const prefix='/karma-source/',files=new Map(Object.entries(input.files).map(([p,t])=>[prefix+p,t]));
const libdir=path.dirname(input.typescript).replaceAll('\\','/'),libs=new Map();
function read(name){
 name=name.replaceAll('\\','/');
 if(files.has(name))return files.get(name);
 if(path.dirname(name)===libdir&&/^lib\.[a-z0-9.]+\.d\.ts$/.test(path.basename(name))){
  if(!libs.has(name))libs.set(name,fs.readFileSync(name,'utf8'));
  return libs.get(name);
 }
 return undefined;
}
const options={noEmit:true,strict:true,allowJs:true,checkJs:true,target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext,moduleResolution:ts.ModuleResolutionKind.Bundler,skipLibCheck:true,types:[]};
const configDiagnostics=[];
// Config is explicit snapshot data. Never expand includes, extends, plugins or packages.
for(const [name,text] of Object.entries(input.configs||{})){
 const parsed=ts.parseConfigFileTextToJson(name,text);
 if(parsed.error)throw Error('Invalid explicit TypeScript configuration');
 const config=parsed.config,raw=config.compilerOptions||{},base=path.posix.resolve(prefix,path.posix.dirname(name),raw.baseUrl||'.');
 if(base!==prefix.slice(0,-1)&&!base.startsWith(prefix))throw Error('Config baseUrl escapes supplied snapshot');
 if(config.extends)configDiagnostics.push({path:name,line:1,code:0,category:'Warning',message:'extends is not loaded; supply needed compiler options explicitly.'});
 const selected={};
 for(const key of ['jsx','esModuleInterop','allowSyntheticDefaultImports','strictNullChecks','allowImportingTsExtensions'])if(key in raw)selected[key]=raw[key];
 const converted=ts.convertCompilerOptionsFromJson(selected,prefix);
 if(converted.errors.length)throw Error('Unsupported config compiler options');
 Object.assign(options,converted.options,{baseUrl:base});
 if(raw.paths){
  if(typeof raw.paths!=='object'||Object.keys(raw.paths).length>64)throw Error('Invalid path aliases');
  for(const [key,targets] of Object.entries(raw.paths)){
   if(!Array.isArray(targets)||targets.length>8||key.length>180)throw Error('Invalid path aliases');
   for(const target of targets){
    if(typeof target!=='string'||target.includes('\\')||target.length>180||path.posix.isAbsolute(target)||!path.posix.resolve(base,target).startsWith(prefix))throw Error('Path alias escapes snapshot');
   }
  }
  options.paths=raw.paths;
 }
}
const host={getSourceFile:(name,language)=>{const text=read(name);return text===undefined?undefined:ts.createSourceFile(name,text,language,true);},
 getDefaultLibFileName:()=>path.join(libdir,'lib.es2022.d.ts'),writeFile:()=>{throw Error('emit forbidden');},getCurrentDirectory:()=>prefix,
 getDirectories:()=>[],fileExists:n=>files.has(n)||path.dirname(n)===libdir&&/^lib\.[a-z0-9.]+\.d\.ts$/.test(path.basename(n))&&fs.existsSync(n),
 readFile:read,useCaseSensitiveFileNames:()=>true,getCanonicalFileName:n=>n,getNewLine:()=> '\n',directoryExists:n=>[...files.keys()].some(p=>p.startsWith(n.replace(/\/$/,'')+'/'))};
const started=performance.now(),program=ts.createProgram([...files.keys()],options,host),checker=program.getTypeChecker();
const hash=x=>crypto.createHash('sha256').update(JSON.stringify(x)).digest('hex');
const logical=n=>n.getSourceFile().fileName.slice(prefix.length);
const loc=n=>{const s=n.getSourceFile(),p=s.getLineAndCharacterOfPosition(n.getStart(s));return {path:logical(n),line:p.line+1,column:p.character,text:s.text.split(/\r?\n/)[p.line].trim().slice(0,240)};};
const id=n=>'n'+hash([logical(n),n.getStart(n.getSourceFile()),n.kind]).slice(0,16);
const functions=new Map(),calls=[],diagnostics=[...configDiagnostics],moduleDependencies=[];
const rawDiagnostics=ts.getPreEmitDiagnostics(program);
function isFunction(n){return ts.isFunctionDeclaration(n)||ts.isFunctionExpression(n)||ts.isArrowFunction(n)||ts.isMethodDeclaration(n)||ts.isConstructorDeclaration(n)||ts.isGetAccessorDeclaration(n)||ts.isSetAccessorDeclaration(n);}
function name(n){
 const parts=[];for(let p=n;p&&!ts.isSourceFile(p);p=p.parent){
  if(isFunction(p))parts.unshift(p.name?.getText(p.getSourceFile())||(ts.isVariableDeclaration(p.parent)?p.parent.name.getText(p.getSourceFile()):'<anonymous@'+loc(p).line+'>'));
  else if(ts.isClassDeclaration(p)||ts.isClassExpression(p))parts.unshift(p.name?.text||'<class>');
 }
 return parts.join('.')||'<module>';
}
for(const s of program.getSourceFiles())if(files.has(s.fileName)){
 functions.set(id(s),{id:id(s),name:'<module>',kind:'module',...loc(s)});
 const visit=n=>{
  if(isFunction(n)&&n.body)functions.set(id(n),{id:id(n),name:name(n),kind:'function',...loc(n)});
  if(ts.isCallExpression(n))calls.push(n);
  if((ts.isImportDeclaration(n)||ts.isExportDeclaration(n))&&n.moduleSpecifier&&ts.isStringLiteral(n.moduleSpecifier)){
   const resolved=ts.resolveModuleName(n.moduleSpecifier.text,s.fileName,options,host).resolvedModule;
   moduleDependencies.push({source:s.fileName.slice(prefix.length),specifier:n.moduleSpecifier.text,target:resolved&&files.has(resolved.resolvedFileName)?resolved.resolvedFileName.slice(prefix.length):null,...loc(n)});
  }
  ts.forEachChild(n,visit);
 };visit(s);
}
const itemBudget=input.item_budget??1024;
if(!Number.isInteger(itemBudget)||itemBudget<1||itemBudget>16384)throw Error('Invalid parser work-unit allocation');
if(functions.size>itemBudget||calls.length>itemBudget)throw Error('Static graph work-unit allocation exceeded ('+itemBudget+' nodes/calls)');
for(const d of rawDiagnostics)if(d.file&&files.has(d.file.fileName)){
 const pos=d.file.getLineAndCharacterOfPosition(d.start||0);
 diagnostics.push({path:d.file.fileName.slice(prefix.length),line:pos.line+1,code:d.code,category:ts.DiagnosticCategory[d.category],message:ts.flattenDiagnosticMessageText(d.messageText,' ').slice(0,300)});
}
const diagnosticCount=diagnostics.length;
// Retain a bounded display sample; all raw binding diagnostics still constrain
// resolution below. A noisy dependency environment does not erase the file.
if(diagnostics.length>256)diagnostics.splice(256);
function dereference(symbol,seen=new Set()){
 if(!symbol||seen.has(symbol))return null;seen.add(symbol);
 if(symbol.flags&ts.SymbolFlags.Alias){
  for(const d of symbol.getDeclarations()||[]){
   let importNode=d;
   while(importNode.parent&&!ts.isImportDeclaration(importNode)&&!ts.isExportDeclaration(importNode))importNode=importNode.parent;
   if(rawDiagnostics.some(e=>e.file===d.getSourceFile()&&e.category===ts.DiagnosticCategory.Error&&e.start>=importNode.getStart()&&e.start<importNode.end))return null;
   for(let p=d;p&&!ts.isSourceFile(p);p=p.parent)if(p.isTypeOnly)return null;
  }
  return dereference(checker.getAliasedSymbol(symbol),seen);
 }
 const declarations=symbol.getDeclarations()||[];
 const bodies=declarations.filter(d=>isFunction(d)&&d.body);
 if(bodies.length===1)return bodies[0];
 if(declarations.length!==1)return null;
 const d=declarations[0];
 if(ts.isVariableDeclaration(d)&&d.initializer&&(d.parent.flags&ts.NodeFlags.Const)){
  if(ts.isArrowFunction(d.initializer)||ts.isFunctionExpression(d.initializer))return d.initializer;
  if(ts.isIdentifier(d.initializer))return dereference(checker.getSymbolAtLocation(d.initializer),seen);
 }
 return null;
}
const rows=calls.map(c=>{
 let containing=c.parent;while(containing&&!isFunction(containing)&&!ts.isSourceFile(containing))containing=containing.parent;
 let expr=c.expression;while(ts.isParenthesizedExpression(expr))expr=expr.expression;
 const symbol=checker.getSymbolAtLocation(ts.isPropertyAccessExpression(expr)?expr.name:expr);
 const declaration=dereference(symbol),projectTarget=declaration&&files.has(declaration.getSourceFile().fileName)&&functions.has(id(declaration));
 const badSource=rawDiagnostics.some(d=>d.file===c.getSourceFile()&&d.category===ts.DiagnosticCategory.Error&&d.start>=expr.getStart()&&d.start<expr.end);
 const target=projectTarget&&!badSource?id(declaration):null;
 let reason=target?'Compiler resolved a project declaration':badSource?'Compiler diagnostic on callee binding':declaration?'Declaration outside supplied project snapshot':'Dynamic, parameter, mutable or unsupported callee';
 return {id:'c'+hash([logical(c),c.getStart(c.getSourceFile()),c.end]).slice(0,16),source:id(containing),target,expression:expr.getText(c.getSourceFile()),relation:target?'static_call_reference':'unresolved',reason,...loc(c)};
});
const libraryHashes=Object.fromEntries([...libs].map(([p,t])=>[path.basename(p),crypto.createHash('sha256').update(t).digest('hex')]));
process.stdout.write(JSON.stringify({profile:'typescript-program-v2',compiler:ts.version,compiler_options:options,library_hashes:libraryHashes,nodes:[...functions.values()],calls:rows,module_dependencies:moduleDependencies,diagnostics,diagnostic_count:diagnosticCount,diagnostics_truncated:diagnosticCount>diagnostics.length,analysis_ms:performance.now()-started,limitations:['Static declaration references do not prove runtime dispatch or execution.','Only supplied modules and explicit safe config options resolve; no plugins, package loading or emit.','Binding errors invalidate affected calls; other diagnostics remain visible.']}));
