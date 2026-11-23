from lifecycle import timeout as operation_timeout
"""Bounded syntactic call facts. Never import or execute the inspected project."""
import ast,hashlib,json,os
from pathlib import Path
from context_manager import InvalidUpdate,sha,canonical

PROFILE='lexical-calls-v1'

def identity(prefix,*parts):return prefix+sha(canonical(parts))[:14]

def _callee_expression(text, node, formatted):
    """Use source spelling when the AST byte span describes this callee."""
    try:
        spelling = ast.get_source_segment(text, node)
    except UnicodeDecodeError:
        spelling = None
    # Some Python parsers report offsets into the temporary expression used
    # to parse an f-string substitution. A slice can decode and still be wrong.
    if spelling and formatted:
        try:
            candidate = ast.parse(spelling, mode='eval').body
        except SyntaxError:
            spelling = None
        else:
            if ast.dump(candidate) != ast.dump(node):
                spelling = None
    # This is a display label. Source evidence still quotes the physical line.
    return spelling or ast.unparse(node)

def python_facts(path,text):
 tree=ast.parse(text,filename=path);lines=text.replace('\r\n','\n').replace('\r','\n').split('\n')
 symbols=[];calls=[];scopes={};bindings=[];call_spans={}
 def loc(node):return {'line':node.lineno,'column':node.col_offset,'quote':lines[node.lineno-1].strip()[:200]}
 module=identity('n',path,'<module>',1,0)
 symbols.append(dict(id=module,name='<module>',scope='',kind='module',line=1,column=0,quote=''))
 scopes[module]=None
 class Visitor(ast.NodeVisitor):
  current=module;qual=[];conditional=0;formatted=0
  def bind(self,name,kind,node,**kw):bindings.append(dict(scope=self.current,name=name,kind=kind,**loc(node),**kw))
  def function(self,node):
   name='.'.join(self.qual+[node.name]);nid=identity('n',path,name,node.lineno,node.col_offset)
   symbols.append(dict(id=nid,name=name,scope=self.current,kind='function',**loc(node)))
   self.bind(node.name,'symbol' if not node.decorator_list and not self.conditional else 'unknown',node,target=nid)
   for default in list(node.args.defaults)+[x for x in node.args.kw_defaults if x]:self.visit(default)
   for dec in node.decorator_list:self.visit(dec)
   outer=self.current;old_cond=self.conditional;scopes[nid]=outer;self.current=nid;self.qual.append(node.name);self.conditional=0
   for a in list(node.args.posonlyargs)+list(node.args.args)+list(node.args.kwonlyargs)+[x for x in (node.args.vararg,node.args.kwarg) if x]:self.bind(a.arg,'parameter',a)
   for stmt in node.body:self.visit(stmt)
   self.qual.pop();self.current=outer;self.conditional=old_cond
  visit_FunctionDef=function;visit_AsyncFunctionDef=function
  def visit_ClassDef(self,node):
   self.bind(node.name,'unknown',node)
   outer=self.current;cid=identity('n',path,'.'.join(self.qual+[node.name]),node.lineno,node.col_offset)
   symbols.append(dict(id=cid,name='.'.join(self.qual+[node.name]),scope=outer,kind='class',**loc(node)))
   scopes[cid]=outer;self.current=cid;self.qual.append(node.name)
   for stmt in node.body:self.visit(stmt)
   self.qual.pop();self.current=outer
  def visit_Lambda(self,node):
   # A separate unknown scope prevents attributing lambda calls to its outer API.
   outer=self.current;nid=identity('n',path,'<lambda>',node.lineno,node.col_offset)
   symbols.append(dict(id=nid,name='<lambda>',scope=outer,kind='lambda',**loc(node)));scopes[nid]=outer;self.current=nid
   for a in list(node.args.posonlyargs)+list(node.args.args)+list(node.args.kwonlyargs):self.bind(a.arg,'parameter',a)
   self.visit(node.body);self.current=outer
  def visit_ImportFrom(self,node):
   for a in node.names:
    self.bind(a.asname or a.name,'import' if not self.conditional and a.name!='*' else 'unknown',node,module=node.module or '',level=node.level,symbol=a.name)
   if any(a.name=='*' for a in node.names):scopes[self.current]='opaque'
  def visit_Import(self,node):
   for a in node.names:self.bind(a.asname or a.name.split('.')[0],'import' if not self.conditional and (a.asname or '.' not in a.name) else 'unknown',node,module=a.name,level=0,symbol='')
  def visit_Name(self,node):
   if isinstance(node.ctx,(ast.Store,ast.Del)):self.bind(node.id,'unknown',node)
  def visit_ExceptHandler(self,node):
   if node.name:self.bind(node.name,'unknown',node)
   self.generic_visit(node)
  def visit_Global(self,node):scopes[self.current]='opaque'
  visit_Nonlocal=visit_Global
  def conditional_visit(self,node):
   self.conditional+=1;self.generic_visit(node);self.conditional-=1
  visit_If=conditional_visit;visit_For=conditional_visit;visit_AsyncFor=conditional_visit;visit_While=conditional_visit;visit_Try=conditional_visit
  def visit_FormattedValue(self,node):
   self.formatted+=1;self.generic_visit(node);self.formatted-=1
  def visit_Call(self,node):
   f=node.func;base=f.id if isinstance(f,ast.Name) else (f.value.id if isinstance(f,ast.Attribute) and isinstance(f.value,ast.Name) else '')
   attr=f.attr if isinstance(f,ast.Attribute) and isinstance(f.value,ast.Name) else ''
   if isinstance(f,ast.Name) and f.id in ('eval','exec','globals','locals'):scopes[self.current]='opaque'
   span=(node.lineno,node.col_offset,node.end_lineno,node.end_col_offset)
   # Distinct substitutions can have the same parser-reported byte span.
   occurrence=call_spans.get(span,0);call_spans[span]=occurrence+1
   site=identity('c',path,*span,occurrence) if occurrence else identity('c',path,*span)
   calls.append(dict(id=site,caller=self.current,scope=self.current,expression=_callee_expression(text,f,self.formatted),base=base,attr=attr,**loc(node)))
   self.generic_visit(node)
 v=Visitor();v.visit(tree)
 return {'symbols':symbols,'bindings':bindings,'scopes':scopes,'calls':calls}

def extract(path,text,*,item_budget=None):
 # Small API evidence packets retain their original bound. Whole-source maps
 # use the host's page allocation, independent of the model's context window.
 if item_budget is not None and (type(item_budget) is not int or not 1<=item_budget<=16384):raise InvalidUpdate('Invalid parser work-unit allocation')
 if Path(path).suffix=='.py':data=python_facts(path,text)
 elif Path(path).suffix in ('.js','.jsx','.ts','.tsx'):
  node=os.environ.get('KARMA_NODE');ts=os.environ.get('KARMA_TYPESCRIPT')
  if not node or not ts:raise InvalidUpdate('JavaScript/TypeScript API indexing requires the VS Code parser runtime')
  from sandbox_process import parser_output
  request={'path':path,'text':text}
  status,output,error=parser_output(node,Path(__file__).with_name('api_extract.js'),ts,request,
                                    seconds=operation_timeout(10),output_limit=16*1024**2)
  if status:raise InvalidUpdate('API parser rejected source: '+error[:300])
  data=json.loads(output)
 else:raise InvalidUpdate('API profile supports Python, JavaScript and TypeScript files')
 cap=128 if item_budget is None else item_budget
 if len(data['calls'])>cap or len(data['symbols'])>cap or len(data['bindings'])>4*cap:
  raise InvalidUpdate('API index bound exceeded: narrow the file before mapping' if item_budget is None else f'Static graph work-unit allocation exceeded ({cap} nodes/calls; {4*cap} bindings)')
 data.update(path=path,sha256=sha(text),profile=PROFILE)
 for group in ('symbols','bindings','calls'):
  for item in data[group]:item['evidence_id']=identity('e',path,data['sha256'],group,item)
 return data
