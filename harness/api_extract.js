// Parse supplied text only: no project imports, module loading or execution.
const fs=require('node:fs'),crypto=require('node:crypto');
const input=JSON.parse(fs.readFileSync(0,'utf8')),ts=require(input.typescript);
const source=ts.createSourceFile(input.path,input.text,ts.ScriptTarget.Latest,true);
if(source.parseDiagnostics.length)throw new Error('Source has syntax errors');
const id=(...xs)=>'n'+crypto.createHash('sha256').update(JSON.stringify(xs)).digest('hex').slice(0,14);
const lines=input.text.split(/\r?\n/),symbols=[],bindings=[],calls=[],scopes={};
const loc=n=>{const p=source.getLineAndCharacterOfPosition(n.getStart(source));return {line:p.line+1,column:p.character,quote:lines[p.line].trim().slice(0,200)}};
const moduleId=id(input.path,'<module>');scopes[moduleId]=null;symbols.push({id:moduleId,name:'<module>',scope:'',kind:'module',line:1,column:0,quote:''});
let current=moduleId,qual=[],conditional=0;
function bind(name,kind,node,extra={}){bindings.push({scope:current,name,kind,...loc(node),...extra});}
function names(n){return ts.isIdentifier(n)?[n.text]:ts.isObjectBindingPattern(n)||ts.isArrayBindingPattern(n)?n.elements.flatMap(x=>x.name?names(x.name):[]):[];}
function walk(n){
 if(ts.isFunctionDeclaration(n)||ts.isFunctionExpression(n)||ts.isArrowFunction(n)||ts.isMethodDeclaration(n)){
  const variable=ts.isVariableDeclaration(n.parent)?n.parent:null;
  const own=n.name?.getText(source),name=own||(variable&&ts.isIdentifier(variable.name)?variable.name.text:'<anonymous@'+loc(n).line+'>');
  const sid=id(input.path,...qual,name,loc(n).line,loc(n).column),outer=current;
  symbols.push({id:sid,name:[...qual,name].join('.'),scope:outer,kind:'function',exported:!!(n.modifiers?.some(m=>m.kind===ts.SyntaxKind.ExportKeyword)||(variable?.parent?.parent?.modifiers?.some(m=>m.kind===ts.SyntaxKind.ExportKeyword))),...loc(n)});
  if(ts.isFunctionDeclaration(n)&&own)bind(own,conditional?'unknown':'symbol',n,{target:sid});
  if(variable&&ts.isIdentifier(variable.name))bind(variable.name.text,conditional?'unknown':'symbol',variable,{target:sid});
  scopes[sid]=outer;current=sid;qual.push(name);
  if(ts.isFunctionExpression(n)&&own)bind(own,'symbol',n,{target:sid});
  for(const p of n.parameters)for(const name of names(p.name))bind(name,'parameter',p);
  const old=conditional;conditional=0;if(n.body)walk(n.body);conditional=old;qual.pop();current=outer;return;
 }
 if(ts.isClassDeclaration(n)){if(n.name)bind(n.name.text,'unknown',n);const outer=current,sid=id(input.path,n.name?.text||'<class>',loc(n).line);symbols.push({id:sid,name:n.name?.text||'<class>',scope:outer,kind:'class',...loc(n)});scopes[sid]=outer;current=sid;qual.push(n.name?.text||'<class>');ts.forEachChild(n,walk);qual.pop();current=outer;return;}
 if(ts.isImportDeclaration(n)&&n.importClause){
  const clause=n.importClause,module=n.moduleSpecifier.text;
  if(clause.name)bind(clause.name.text,'unknown',n); // default/re-export resolution is deliberately unsupported
  if(clause.namedBindings){if(ts.isNamespaceImport(clause.namedBindings))bind(clause.namedBindings.name.text,clause.isTypeOnly?'unknown':'import',n,{module,level:0,symbol:''});
   else for(const e of clause.namedBindings.elements)bind(e.name.text,clause.isTypeOnly||e.isTypeOnly?'unknown':'import',n,{module,level:0,symbol:e.propertyName?.text||e.name.text});}return;
 }
 if(ts.isVariableDeclaration(n)&&!(n.initializer&&(ts.isArrowFunction(n.initializer)||ts.isFunctionExpression(n.initializer))))for(const name of names(n.name))bind(name,'unknown',n);
 if(ts.isBinaryExpression(n)&&n.operatorToken.kind>=ts.SyntaxKind.FirstAssignment&&n.operatorToken.kind<=ts.SyntaxKind.LastAssignment&&ts.isIdentifier(n.left))bind(n.left.text,'unknown',n);
 if((ts.isPrefixUnaryExpression(n)||ts.isPostfixUnaryExpression(n))&&ts.isIdentifier(n.operand)&&[ts.SyntaxKind.PlusPlusToken,ts.SyntaxKind.MinusMinusToken].includes(n.operator))bind(n.operand.text,'unknown',n);
 if(ts.isCallExpression(n)){
  const f=n.expression,base=ts.isIdentifier(f)?f.text:ts.isPropertyAccessExpression(f)&&ts.isIdentifier(f.expression)?f.expression.text:'',attr=ts.isPropertyAccessExpression(f)&&ts.isIdentifier(f.expression)?f.name.text:'';
  if(base==='eval')scopes[current]='opaque';
  calls.push({id:id(input.path,'call',loc(n).line,loc(n).column).replace(/^n/,'c'),caller:current,scope:current,expression:f.getText(source),base,attr,...loc(n)});
 }
 const branch=ts.isIfStatement(n)||ts.isForStatement(n)||ts.isForOfStatement(n)||ts.isForInStatement(n)||ts.isWhileStatement(n)||ts.isTryStatement(n)||ts.isSwitchStatement(n);
 if(branch)conditional++;ts.forEachChild(n,walk);if(branch)conditional--;
}
walk(source);process.stdout.write(JSON.stringify({symbols,bindings,scopes,calls,parser:'typescript-'+ts.version}));
