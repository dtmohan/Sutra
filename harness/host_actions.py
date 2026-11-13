"""Bounded host actions: exact Python edits and sandboxed unittest snapshots."""
import ast
import difflib
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from context_manager import InvalidUpdate, canonical, sha
from karma_adapter import budget_decision
from monitoring import require_resources, resource_state
from paging import PageFault


def initialize(s):
    s.db.executescript('''
      CREATE TABLE IF NOT EXISTS host_actions(id TEXT PRIMARY KEY, session TEXT, invocation_id TEXT UNIQUE,
        record_json TEXT NOT NULL, digest TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS host_action_receipts(action_id TEXT PRIMARY KEY, session TEXT, seq INTEGER,
        id TEXT UNIQUE, record_json TEXT NOT NULL, UNIQUE(session,seq));
    ''')
    for table in ('host_actions','host_action_receipts'):
        for op in ('UPDATE','DELETE'):
            s.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable action record'); END")
    s.db.commit()


def source_path(workspace, name):
    from vscode_bridge import safe_path
    target=safe_path(workspace,name);walk=Path(workspace).resolve()
    for component in Path(name).parts:
        walk=walk/component
        if walk.is_symlink():raise InvalidUpdate('Symlink source paths are not supported')
    if not target.is_file() or not stat.S_ISREG(target.stat().st_mode):raise InvalidUpdate('Action requires an existing regular file')
    if target.stat().st_nlink!=1:raise InvalidUpdate('Hard-linked source files are not supported')
    return target


def files_at(s, session, workspace, allow_missing=False):
    result={}
    for name in s.snapshot(session)['constraints']['allowed_paths']:
        from vscode_bridge import safe_path
        target=safe_path(workspace,name)
        if allow_missing and not target.exists() and not target.is_symlink():continue
        p=source_path(workspace,name);raw=p.read_bytes()
        if len(raw)>64000:raise InvalidUpdate('Scoped file exceeds 64 KB')
        result[name]={'sha256':hashlib.sha256(raw).hexdigest(),'text':raw.decode('utf-8')}
    return result


def imports(tree):
    roots=set()
    for n in ast.walk(tree):
        if isinstance(n,ast.Import):roots.update(a.name.split('.')[0] for a in n.names)
        if isinstance(n,ast.ImportFrom) and n.module and n.level==0:roots.add(n.module.split('.')[0])
    return roots


def proposed_edit(workspace, constraints, action, payload):
    name=action['target'];old=action['old'];new=action['new']
    if constraints.get('edits_allowed') is not True:raise InvalidUpdate('Code edits are not allowed by the contract')
    if name not in constraints['allowed_paths'] or not name.endswith('.py'):raise InvalidUpdate('Edit target must be an in-scope Python file')
    if name in constraints.get('test_paths',[]):
        # Tests may be edited when explicitly paged in; they receive the same
        # diff/version checks. Passing tests alone never establish correctness.
        pass
    if not old or old==new or len(old)>2400 or len(new)>2400:raise InvalidUpdate('Edit needs a nonempty, changed, bounded exact replacement')
    if not any(p.get('path')==name and old in p['text'] for p in payload['pages']):
        raise InvalidUpdate('Replacement preimage is absent from the delivered source page')
    path=source_path(workspace,name);before=path.read_bytes().decode('utf-8')
    if before.count(old)!=1:raise InvalidUpdate('Replacement preimage must occur exactly once')
    observed=payload.get('source_versions',{}).get(name)
    if observed!=sha(before):raise InvalidUpdate('Source changed since proposal delivery')
    # Preserve the original newline convention, including multiline replacements.
    if '\r\n' in before and '\n' not in before.replace('\r\n',''):
        new=new.replace('\r\n','\n').replace('\n','\r\n')
    after=before.replace(old,new,1)
    if len(after.encode())>64000:raise InvalidUpdate('Edited file exceeds 64 KB')
    try:a=ast.parse(before);b=ast.parse(after)
    except SyntaxError as exc:raise InvalidUpdate('Python syntax check failed: '+str(exc)) from None
    if constraints.get('dependencies_allowed') is False:
        stdlib=Path(os.__file__).parent
        allowed=set(sys.builtin_module_names)|{p.stem for p in stdlib.iterdir()}|{Path(p).parts[0].split('.')[0] for p in constraints['allowed_paths']}
        if imports(b)-imports(a)-allowed:raise InvalidUpdate('New external import conflicts with dependency constraint')
    return {'target':name,'before':before,'after':after,'before_hash':sha(before),'after_hash':sha(after),
            'diff':''.join(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile=name,tofile=name))}


def execution_context(s, session, workspace):
    if not s.db.execute("SELECT 1 FROM sqlite_master WHERE name='host_actions'").fetchone():return {'actions_used':0,'recent':[],'tests':'unverified'}
    origin=s.origin(session);recent=[]
    try:current={k:v['sha256'] for k,v in files_at(s,session,workspace,allow_missing=True).items()}
    except (OSError,ValueError):current={}
    rows=s.db.execute('SELECT a.record_json,r.record_json FROM host_actions a LEFT JOIN host_action_receipts r ON a.id=r.action_id WHERE a.session=? ORDER BY a.rowid DESC',(session,)).fetchall()
    tests='unverified';used=0;pending=0
    for raw,receipt_raw in rows:
        action=json.loads(raw)
        if action['contract_hash']!=origin['contract_hash']:continue
        used+=1
        result=json.loads(receipt_raw)['result'] if receipt_raw else {'status':'pending'}
        if not receipt_raw:pending+=1
        valid=result.get('artifact_versions')==current
        if tests=='unverified' and action['kind']=='run_tests':
            status=result.get('status','unknown')
            # Historical "passed" receipts predate the trust-boundary fix.
            tests='legacy_unverified' if status=='passed' else (status+'_current' if valid else 'stale_'+status)
        if len(recent)<3:
            item={'id':action['id'],'kind':action['kind'],'target':action['target'],'status':result.get('status'),
                  'model':action['model'],'before_hash':action.get('before_hash'),'after_hash':result.get('after_hash')}
            if action['kind']=='run_tests':item.update(returncode=result.get('returncode'),tests=result.get('tests'),
                    current=valid,assertions_verified=False,output_tail=result.get('output_tail','')[-700:] if not any(r['kind']=='run_tests' for r in recent) else '')
            recent.append(item)
    return {'authority':'host_process_observations','assertions_verified':False,'actions_used':used,'pending_actions':pending,'recent':list(reversed(recent)),'tests':tests}


def receipt(s, action, result):
    with s.db:
        s.db.execute('BEGIN IMMEDIATE')
        row=s.db.execute('SELECT seq,id FROM host_action_receipts WHERE session=? ORDER BY seq DESC LIMIT 1',(action['session'],)).fetchone()
        seq,previous=row if row else (0,'GENESIS')
        record={'action_id':action['id'],'session':action['session'],'seq':seq+1,'previous_hash':previous,
                'action_hash':sha('action-v1:'+canonical(action)),'contract_hash':action['contract_hash'],
                'logic_hash':action['logic_hash'],'result':result,'completed_at':time.time()}
        digest=sha('action-receipt-v1:'+canonical(record))
        s.db.execute('INSERT INTO host_action_receipts VALUES(?,?,?,?,?)',(action['id'],action['session'],seq+1,digest,canonical(record)))
        s._audit('HOST_ACTION_RECEIPT',{'id':digest,'record':record})
        from orchestration import settle
        settle(s,action.get('orchestration'),digest,result.get('elapsed_seconds'))
        if action.get('workflow_id'):
            from orchestration import transition
            transition(s,action['workflow_id'],'DONE','ACTION_SETTLED',{'action_id':action['id'],'receipt':digest,'status':result.get('status'),'assertions_verified':False})
    return dict(record,id=digest)


def verified_proposal(s,session,iid,workspace):
    output=s.load_result(session,iid)
    row=s.db.execute('SELECT record_json FROM invocations WHERE id=? AND session=?',(iid,session)).fetchone()
    env=json.loads(row[0])
    if env['role']!='coder':raise InvalidUpdate('Only coder-role proposals can request host actions')
    payload=json.loads(s.db.execute('SELECT payload_json FROM deliveries WHERE id=?',(env['delivery_id'],)).fetchone()[0])
    from coding_role import validate
    validate(s,session,payload,output)
    return env,payload,output


def preview(s,session,iid,workspace):
    env,payload,output=verified_proposal(s,session,iid,workspace)
    action=output['action'];constraints=s.snapshot(session)['constraints']
    if action['kind']=='edit':return proposed_edit(workspace,constraints,action,payload)
    if action['kind']=='run_tests':return {'target':action['target'],'runner':'Python unittest','isolation':'scoped read-only snapshot; network denied',
                                         'timeout_ms':constraints['test_timeout_ms']}
    raise InvalidUpdate('Proposal does not request an executable action')


def replace_file(workspace, change):
    # Directory FDs and O_NOFOLLOW prevent a path component from redirecting the
    # write to a symlink target. Atomic replacement follows a last preimage check.
    parts=Path(change['target']).parts;fd=os.open(str(Path(workspace).resolve()),os.O_RDONLY|os.O_DIRECTORY)
    tmp='.karma-'+uuid.uuid4().hex+'.tmp'
    try:
        for part in parts[:-1]:
            next_fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=next_fd
        original=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
        with os.fdopen(original,'rb') as f:
            st=os.fstat(f.fileno());raw=f.read(64001)
        if st.st_nlink!=1 or hashlib.sha256(raw).hexdigest()!=change['before_hash']:raise InvalidUpdate('Edit preimage changed before write')
        temp_fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
        with os.fdopen(temp_fd,'wb') as f:f.write(change['after'].encode());os.fchmod(f.fileno(),stat.S_IMODE(st.st_mode));f.flush();os.fsync(f.fileno())
        now=os.stat(parts[-1],dir_fd=fd,follow_symlinks=False)
        if (now.st_ino,now.st_mtime_ns,now.st_size)!=(st.st_ino,st.st_mtime_ns,st.st_size):raise InvalidUpdate('File changed during edit preparation')
        os.replace(tmp,parts[-1],src_dir_fd=fd,dst_dir_fd=fd)
        return hashlib.sha256((Path(workspace)/change['target']).read_bytes()).hexdigest()
    finally:
        try:os.unlink(tmp,dir_fd=fd)
        except FileNotFoundError:pass
        os.close(fd)


def test_runtime():
    framework=Path(sys.prefix)/'Resources/Python.app/Contents/MacOS/Python'
    return framework if framework.exists() else Path(sys.executable).resolve()


def sandbox_profile(snapshot,scratch,runner):
    def lit(p):return json.dumps(str(Path(p).resolve()))
    runtime=test_runtime()
    # Root-directory read is needed by macOS dyld's openat bootstrap, not a
    # recursive root grant. No mach-lookup, fork, network or host writes allowed.
    roots=['/System','/usr','/Library/Developer/CommandLineTools',sys.base_prefix,snapshot,scratch]
    return '(version 1)(deny default)(allow file-read-metadata)(allow sysctl-read)'+\
        '(allow process-exec (literal '+lit(runtime)+'))'+\
        '(allow file-read* (literal "/") (literal "/dev/null") (literal "/dev/urandom") (literal '+lit(runner)+') '+\
        ' '.join('(subpath '+lit(p)+')' for p in roots)+')'+\
        '(allow file-write* (subpath '+lit(scratch)+'))'


def run_tests_snapshot(directory,files,target,timeout_ms,root=None):
    snapshot=directory/'snapshot';scratch=directory/'tmp';snapshot.mkdir();scratch.mkdir()
    for name,item in files.items():
        p=snapshot/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(item['text'].encode('utf-8'))
    runner=Path(__file__).parent/'unittest_entry.py';profile=sandbox_profile(snapshot,scratch,runner)
    (directory/'sandbox.sb').write_text(profile)
    command=['/usr/bin/sandbox-exec','-f',str(directory/'sandbox.sb'),str(test_runtime()),'-I','-B',str(runner),str(snapshot),target]
    env={'PATH':str(test_runtime().parent),'HOME':str(scratch),'TMPDIR':str(scratch),'LANG':'en_US.UTF-8'}
    started=time.monotonic();data=bytearray();reason=None
    proc=subprocess.Popen(command,cwd=snapshot,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
    sel=selectors.DefaultSelector();sel.register(proc.stdout,selectors.EVENT_READ)
    try:
        while sel.get_map():
            elapsed=time.monotonic()-started
            if elapsed>timeout_ms/1000:reason='timeout'
            if root is not None and not resource_state(root)['admitted']:reason='resource_wait'
            if reason:
                os.killpg(proc.pid,signal.SIGKILL);break
            for key,_ in sel.select(.1):
                chunk=os.read(key.fileobj.fileno(),8192)
                if not chunk:sel.unregister(key.fileobj);continue
                data.extend(chunk)
                if len(data)>65536:reason='output_limit';os.killpg(proc.pid,signal.SIGKILL);break
        proc.wait(timeout=3)
    finally:
        if proc.poll() is None:os.killpg(proc.pid,signal.SIGKILL);proc.wait(timeout=3)
        sel.close();proc.stdout.close()
    output=bytes(data[:65536]).decode('utf-8','replace');(directory/'test-output.txt').write_text(output)
    report={}
    for line in output.splitlines():
        if line.startswith('KARMA_TEST_RESULT '):
            try:
                claimed=json.loads(line[len('KARMA_TEST_RESULT '):])
                if isinstance(claimed,dict):report=claimed
            except ValueError:pass
    # The imported code shares the runner process. Neither its JSON nor exit 0
    # proves assertions ran. Even a separate pipe/nonce cannot establish that.
    return {'status':reason or ('process_exited_zero' if proc.returncode==0 else 'process_failed'),
            'returncode':proc.returncode,'reported_result':report,'assertions_verified':False,
            'evidence_kind':'untrusted_test_process_output',
            'elapsed_seconds':round(time.monotonic()-started,3),'output_tail':output[-3000:],
            'output_sha256':sha(output),'sandbox_profile_sha256':sha(profile),'runner_sha256':hashlib.sha256(runner.read_bytes()).hexdigest(),
            'network_allowed':False,'project_writes_allowed':False,'command':command}


def execute(s,session,iid,workspace,root,resource_check=True):
    from lifecycle import timeout
    from storage_policy import require_space
    timeout(120);require_space(root)
    initialize(s)
    prior=s.db.execute('SELECT id FROM host_actions WHERE invocation_id=?',(iid,)).fetchone()
    if prior:
        row=s.db.execute('SELECT id,record_json FROM host_action_receipts WHERE action_id=?',(prior[0],)).fetchone()
        if not row:raise InvalidUpdate('Action is pending or interrupted; it will not be replayed automatically')
        return dict(json.loads(row[1]),id=row[0],replayed_receipt=True)
    if s.db.execute('SELECT 1 FROM host_actions a LEFT JOIN host_action_receipts r ON a.id=r.action_id WHERE a.session=? AND r.action_id IS NULL',(session,)).fetchone():
        raise InvalidUpdate('An earlier action is pending; inspect it before executing another action')
    env,payload,output=verified_proposal(s,session,iid,workspace)
    action=output['action'];kind=action['kind'];constraints=s.snapshot(session)['constraints']
    if kind not in ('edit','run_tests'):raise InvalidUpdate('No executable action in this proposal')
    if kind=='run_tests' and sys.platform!='darwin':raise InvalidUpdate('Sandboxed test execution is currently supported only on macOS')
    if kind=='edit' and os.name=='nt':raise InvalidUpdate('Atomic code edits are currently supported only on macOS/Linux; Windows supports read-only reviews')
    hardware=require_resources(root) if resource_check else {'profile':'unit-test-fixture'}
    files=files_at(s,session,workspace);versions={k:v['sha256'] for k,v in files.items()}
    if versions!=payload.get('source_versions'):raise InvalidUpdate('Scoped file versions changed since proposal')
    change=proposed_edit(workspace,constraints,action,payload) if kind=='edit' else None
    if kind=='run_tests' and (constraints.get('tests_allowed') is not True or action['target'] not in constraints.get('test_paths',[])):
        raise InvalidUpdate('Test execution is not permitted for this target')
    origin=s.current_contract(session);aid=uuid.uuid4().hex
    with s.db:
        s.db.execute('BEGIN IMMEDIATE')
        used=sum(json.loads(r[0])['contract_hash']==origin['contract_hash'] for r in s.db.execute('SELECT record_json FROM host_actions WHERE session=?',(session,)))
        measured=budget_decision(used,constraints['max_actions'])
        if measured['decision']!='ALLOW':
            s._audit('ACTION_DENIED',{'session':session,'budget_kernel':measured});s.db.commit()
            raise InvalidUpdate('Action budget exhausted: '+measured['decision'])
        commit={'id':aid,'session':session,'invocation_id':iid,'model':env['model'],'kind':kind,'target':action['target'],
                'contract_hash':origin['contract_hash'],'epoch':origin['epoch'],'logic_hash':s.logic_hash,
                'budget_kernel':measured,'resource_kernel':hardware,'artifact_versions':versions,
                'before_hash':change['before_hash'] if change else None,'proposed_after_hash':change['after_hash'] if change else None,
                'proposal_hash':sha(canonical(output)),'started_at':time.time()}
        from orchestration import admit,action_flow,transition
        workflow=action_flow(s,iid)
        if workflow:
            transition(s,workflow,'EXECUTING_ACTION','ACTION_DISPATCH',{'action_id':aid})
            commit['workflow_id']=workflow
        commit['orchestration']=admit(s,session,kind,aid,{'actions':1,'seconds':constraints.get('test_timeout_ms',10000)/1000 if kind=='run_tests' else 1},paths=[action['target']],resource=hardware)
        raw=canonical(commit);s.db.execute('INSERT INTO host_actions VALUES(?,?,?,?,?)',(aid,session,iid,raw,sha('action-v1:'+raw)))
        s._audit('HOST_ACTION_DISPATCH',commit)
    directory=Path(root)/'actions'/aid;directory.mkdir(parents=True)
    try:
        if kind=='edit':
            (directory/'before.py').write_bytes(change['before'].encode());(directory/'after.py').write_bytes(change['after'].encode());(directory/'change.diff').write_text(change['diff'])
            after=replace_file(workspace,change)
            if after!=change['after_hash']:raise InvalidUpdate('Edited file changed before receipt')
            s.observe_file(session,'action-'+aid,action['target'],str(Path(workspace)/action['target']),s.snapshot(session)['revision'])
            result={'status':'applied','after_hash':after,'backup':str(directory/'before.py')}
        else:
            result=run_tests_snapshot(directory,files,action['target'],constraints['test_timeout_ms'],root if resource_check else None)
            current={k:v['sha256'] for k,v in files_at(s,session,workspace).items()}
            result['artifact_versions']=versions
            if current!=versions:result['status']='stale_sources'
            # Keep the observation in this receipt. Do not promote output to
            # record_test / tool-authority assertion pages or completion proof.
    except Exception as exc:
        result={'status':'failed_or_uncertain','error':type(exc).__name__+': '+str(exc)}
    return receipt(s,commit,result)
