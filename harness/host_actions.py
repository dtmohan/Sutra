"""Bounded host actions: exact Python edits and sandboxed unittest snapshots."""
import ast
from collections import Counter
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
import sysconfig
from importlib.machinery import EXTENSION_SUFFIXES
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
    from repository_tools import source_size
    source_size(workspace,name)
    return Path(workspace).resolve()/name


def files_at(s, session, workspace, allow_missing=False):
    from repository_tools import read_bytes
    result={}
    for name in s.snapshot(session)['constraints']['allowed_paths']:
        raw=read_bytes(workspace,name,allow_missing=allow_missing)
        if raw is None:continue
        result[name]={'sha256':hashlib.sha256(raw).hexdigest(),'text':raw.decode('utf-8')}
    return result


def imports(tree):
    roots=set()
    for n in ast.walk(tree):
        if isinstance(n,ast.Import):roots.update(a.name.split('.')[0] for a in n.names)
        if isinstance(n,ast.ImportFrom) and n.module and n.level==0:roots.add(n.module.split('.')[0])
    return roots


def dynamic_imports(tree):
    roots,unresolved=set(),Counter()
    modules={'importlib':'import_module','builtins':'__import__'}
    aliases={name:operation for name,operation in modules.items()}
    functions={'__import__'}
    for node in ast.walk(tree):
        if isinstance(node,ast.Import):
            aliases.update({item.asname or item.name:modules[item.name] for item in node.names if item.name in modules})
        elif isinstance(node,ast.ImportFrom) and node.level==0 and node.module in modules:
            functions.update(item.asname or item.name for item in node.names if item.name==modules[node.module])
    for node in ast.walk(tree):
        if not isinstance(node,ast.Call):continue
        fn=node.func
        direct=isinstance(fn,ast.Name) and fn.id in functions
        attribute=(isinstance(fn,ast.Attribute) and isinstance(fn.value,ast.Name)
                   and aliases.get(fn.value.id)==fn.attr)
        if not (direct or attribute):continue
        name=node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg=='name'),None)
        if isinstance(name,ast.Constant) and isinstance(name.value,str):
            root=name.value.split('.',1)[0]
            if root.isidentifier():
                roots.add(root)
                continue
        unresolved[ast.dump(node)]+=1
    return roots,unresolved


def stdlib_modules():
    """Discover runtime stdlib names without importing project or site packages."""
    if hasattr(sys, 'stdlib_module_names'):
        return set(sys.stdlib_module_names)
    names = set(sys.builtin_module_names)
    paths = sysconfig.get_paths()
    roots = {Path(paths[key]) for key in ('stdlib', 'platstdlib') if paths.get(key)}
    roots.update(p / 'lib-dynload' for p in tuple(roots))
    shared = sysconfig.get_config_var('DESTSHARED')
    if shared:
        roots.add(Path(shared))
    for root in roots:
        if not root.is_dir():
            continue
        for item in root.iterdir():
            if item.name in ('site-packages', 'dist-packages', '__pycache__'):
                continue
            if item.is_dir() and (item / '__init__.py').is_file():
                names.add(item.name)
            elif item.is_file():
                for suffix in (*EXTENSION_SUFFIXES, '.py', '.pyc'):
                    if item.name.endswith(suffix):
                        names.add(item.name[:-len(suffix)])
                        break
    return names


def proposed_edit(workspace, constraints, action, payload, *, read_scope=None):
    from repository_tools import read_bytes
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
    before=read_bytes(workspace,name).decode('utf-8')
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
        allowed=stdlib_modules()|{Path(p).parts[0].split('.')[0] for p in constraints['allowed_paths']}
        old_dynamic,old_unresolved=dynamic_imports(a)
        new_dynamic,new_unresolved=dynamic_imports(b)
        if new_unresolved-old_unresolved:
            raise InvalidUpdate('New dynamic import lacks a bounded dependency name')
        added=(imports(b)|new_dynamic)-(imports(a)|old_dynamic)-allowed
        if added and read_scope is not None:
            from repository_tools import inventory
            names=inventory(Path(workspace),read_scope,automatic=True)
            allowed.update(Path(p).parts[0].split('.')[0] for p in names if p.endswith('.py'))
        if added-allowed:raise InvalidUpdate('New external or out-of-scope import conflicts with dependency constraint')
    return {'target':name,'before':before,'after':after,'before_hash':sha(before),'after_hash':sha(after),
            'diff':''.join(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile=name,tofile=name))}


def project_changes(workspace, constraints, edits, payload, read_scope):
    targets = [item['target'] for item in edits]
    if (constraints.get('project_patch_allowed') is not True or read_scope != ['.']
            or len(targets) < 2 or sorted(targets) != constraints['allowed_paths']
            or constraints.get('network_allowed') is not False
            or constraints.get('dependencies_allowed') is not False):
        raise InvalidUpdate('Patch must change each approved Python target exactly once in an isolated worktree')
    return [proposed_edit(workspace, dict(constraints, edits_allowed=True), edit,
                          payload, read_scope=read_scope) for edit in sorted(edits, key=lambda item: item['target'])]


def project_plan(workspace, constraints, action, payload, read_scope):
    from project_worktree import plan
    changes = project_changes(workspace, constraints, action['edits'], payload, read_scope)
    result = plan(workspace, sorted(action['edits'], key=lambda item: item['target']))
    def hashes(rows):
        return [(item['target'], item['before_hash'], item['after_hash']) for item in rows]
    if hashes(result['changes']) != hashes(changes):
        raise InvalidUpdate('Worktree patch differs from the checked source versions')
    return result


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
                    current=valid,assertions_verified=False,transition=(result.get('provenance') or {}).get('transition'),output_tail=result.get('output_tail','')[-700:] if not any(r['kind']=='run_tests' for r in recent) else '')
            if action['kind']=='worktree_patch':
                item.update(target=', '.join(action['targets']), returncode=result.get('returncode'),
                            assertions_verified=False, output_tail=result.get('output_tail','')[-700:])
            recent.append(item)
    return {'authority':'host_process_observations','assertions_verified':False,'actions_used':used,'pending_actions':pending,'recent':list(reversed(recent)),'tests':tests}


def outcome_grade(status):
    return {'process_failed':'red','process_exited_zero':'green'}.get(status,'inconclusive')


def action_history(s, session):
    rows=s.db.execute('SELECT a.record_json,r.record_json FROM host_actions a JOIN host_action_receipts r ON a.id=r.action_id WHERE a.session=? ORDER BY a.rowid DESC',(session,)).fetchall()
    return [(json.loads(a),json.loads(r)['result']) for a,r in rows]


def run_provenance(history, target, test_paths, versions, status, execution=None):
    """Host-derived context for one test observation, read from receipts only.

    history holds (action, result) pairs for this session's receipted actions,
    newest first. A failing run followed by an exit-zero run over byte-identical
    test files is a stronger observation than an exit code alone, because the
    change in outcome cannot come from an edited test. It still does not prove
    that assertions ran, so assertions_verified stays False everywhere.
    """
    edited=sorted({a['target'] for a,r in history if a['kind']=='edit' and r.get('status')=='applied' and a['target'] in test_paths})
    prior=next(((a,r) for a,r in history if a['kind']=='run_tests' and a['target']==target),None)
    now=outcome_grade(status)
    out={'version':2,'target_sha256':versions.get(target),'test_paths':sorted(test_paths),
         'execution':execution,'test_paths_edited_in_task':edited,'prior_run':None,
         'transition':'inconclusive' if now=='inconclusive' else 'first_run'}
    if prior:
        action,result=prior;before=result.get('artifact_versions') or {};was=outcome_grade(result.get('status'))
        provenance=result.get('provenance') or {}
        comparable=bool(execution and provenance.get('execution')==execution and
                        provenance.get('version')==2 and provenance.get('test_paths')==sorted(test_paths))
        tests_unchanged=comparable and all(p in before and p in versions and before[p]==versions[p] for p in test_paths)
        sources_changed=any(before.get(k)!=versions.get(k) for k in set(before)|set(versions) if k not in test_paths)
        out['prior_run']={'action_id':action['id'],'status':result.get('status'),'comparable':comparable,
                          'tests_unchanged':tests_unchanged,'sources_changed':sources_changed}
        if not comparable or 'inconclusive' in (was,now):out['transition']='inconclusive'
        elif not tests_unchanged:out['transition']='tests_changed'
        elif (was,now)==('red','green') and not sources_changed:out['transition']='red_to_green_without_source_change'
        else:out['transition']=was+'_to_'+now
    return out


def execution_identity(s, session, constraints):
    """Stable host identity for comparing process observations, not test proof.

    Snapshot-specific paths are deliberately excluded. Runtime bytes, standard
    library build, runner, sandbox policy implementation, limits and task epoch
    are included. Any changed or missing identity makes a comparison inconclusive.
    """
    runtime=test_runtime().resolve()
    def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    command=[str(runtime),'-I','-B','-c',
             'import sys,sysconfig,json; print(json.dumps([sys.version,sys.implementation.name,sysconfig.get_config_vars("SOABI","CONFIG_ARGS")]))']
    build=subprocess.check_output(command,timeout=5,text=True).strip()
    origin=s.current_contract(session)
    return {'contract_hash':origin['contract_hash'],'epoch':origin['epoch'],'logic_hash':s.logic_hash,
            'runtime_sha256':digest(runtime),'runtime_build':build,
            'runner_sha256':digest(Path(__file__).with_name('unittest_entry.py')),
            'sandbox_implementation_sha256':digest(__file__),
            'platform':sys.platform,'os_release':__import__('platform').release(),'test_timeout_ms':constraints['test_timeout_ms'],
            'environment':{'isolated':True,'bytecode':False,'LANG':'en_US.UTF-8',
                           'network_allowed':False,'project_writes_allowed':False}}


def receipt(s, action, result):
    if action['kind']=='run_tests' and 'provenance' not in result:
        # Recovery cannot reconstruct a runtime identity for an interrupted run.
        result['provenance']=run_provenance(action_history(s,action['session']),action['target'],
            action.get('test_paths',[]),action.get('artifact_versions',{}),result.get('status'),None)
    with s.db:
        s.db.execute('BEGIN IMMEDIATE')
        row=s.db.execute('SELECT seq,id FROM host_action_receipts WHERE session=? ORDER BY seq DESC LIMIT 1',(action['session'],)).fetchone()
        seq,previous=row if row else (0,'GENESIS')
        record={'action_id':action['id'],'session':action['session'],'seq':seq+1,'previous_hash':previous,
                'action_hash':sha('action-v1:'+canonical(action)),'contract_hash':action['contract_hash'],
                'logic_hash':action['logic_hash'],'result':result,'completed_at':time.time()}
        if action['kind']=='worktree_patch':
            record.update(kind=action['kind'], base_commit=action['base_commit'],
                          targets=action['targets'], plan_digest=action['plan_digest'])
        digest=sha('action-receipt-v1:'+canonical(record))
        s.db.execute('INSERT INTO host_action_receipts VALUES(?,?,?,?,?)',(action['id'],action['session'],seq+1,digest,canonical(record)))
        s._audit('HOST_ACTION_RECEIPT',{'id':digest,'record':record})
        from orchestration import settle
        settle(s,action.get('orchestration'),digest,result.get('elapsed_seconds'))
        if action.get('workflow_id'):
            from orchestration import transition
            transition(s,action['workflow_id'],'DONE','ACTION_SETTLED',{'action_id':action['id'],'receipt':digest,'status':result.get('status'),'assertions_verified':False})
            from execution_sessions import finish
            finish(s, action['workflow_id'])
    return dict(record,id=digest)


def project_receipt(s, action_id, workflow=None, expected_receipt=None):
    row = s.db.execute(
        'SELECT a.record_json,a.digest,r.id,r.record_json FROM host_actions a '
        'JOIN host_action_receipts r ON r.action_id=a.id AND r.session=a.session WHERE a.id=?',
        (action_id,)).fetchone()
    if not row:
        return None
    raw, action_digest, digest, record = row
    action, result = json.loads(raw), json.loads(record)
    if action['kind'] != 'worktree_patch':
        return None
    if (sha('action-v1:'+raw) != action_digest or result['action_hash'] != action_digest
            or sha('action-receipt-v1:'+record) != digest
            or (workflow is not None and action.get('workflow_id') != workflow)
            or (expected_receipt is not None and digest != expected_receipt)
            or not s.audit_valid()):
        raise InvalidUpdate('Isolated worktree receipt no longer matches its action commitment')
    return dict(result,id=digest)


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
    if action['kind']=='edit':
        from native_access import read_scope
        return proposed_edit(workspace,constraints,action,payload,read_scope=read_scope(s,session))
    if action['kind']=='worktree_patch':
        from native_access import read_scope
        from orchestration import action_flow
        if not action_flow(s,iid):
            raise InvalidUpdate('Project patches require their native action workflow')
        plan = project_plan(workspace, constraints, action, payload, read_scope(s,session))
        return dict(kind='worktree_patch', changes=plan['changes'], base_commit=plan['base_commit'],
                    runner='Python unittest discovery', isolation='Detached Git worktree with network denied',
                    test_profile='python-unittest', test_target=plan['test_target'], files=plan['files'],
                    source_bytes=plan['source_bytes'], timeout_ms=constraints['test_timeout_ms'],
                    plan_digest=sha(canonical(plan)))
    if action['kind']=='run_tests':return {'target':action['target'],'runner':'Python unittest','isolation':'scoped read-only snapshot; network denied',
                                         'snapshot_files':sorted(constraints['allowed_paths']),
                                         'snapshot_note':'Only these granted files are available. Include required project modules in the task grant before running tests.',
                                         'timeout_ms':constraints['test_timeout_ms']}
    raise InvalidUpdate('Proposal does not request an executable action')


def replace_file(workspace, change):
    # Directory FDs and O_NOFOLLOW prevent a path component from redirecting the
    # write to a symlink target. Atomic replacement follows a last preimage check.
    from repository_tools import MAX_FILE, normalized, read_source, source_directory, source_stream
    name=normalized(change['target']);parts=Path(name).parts
    tmp='.karma-'+uuid.uuid4().hex+'.tmp'
    with source_directory(workspace,parts[:-1]) as fd:
        try:
            with source_stream(fd,parts[-1]) as stream:raw,st=read_source(stream,name,MAX_FILE)
            if hashlib.sha256(raw).hexdigest()!=change['before_hash']:raise InvalidUpdate('Edit preimage changed before write')
            temp_fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
            with os.fdopen(temp_fd,'wb') as f:f.write(change['after'].encode());os.fchmod(f.fileno(),stat.S_IMODE(st.st_mode));f.flush();os.fsync(f.fileno())
            now=os.stat(parts[-1],dir_fd=fd,follow_symlinks=False)
            if ((now.st_dev,now.st_ino,now.st_mtime_ns,now.st_size,now.st_nlink)
                    !=(st.st_dev,st.st_ino,st.st_mtime_ns,st.st_size,st.st_nlink)):
                raise InvalidUpdate('File changed during edit preparation')
            os.replace(tmp,parts[-1],src_dir_fd=fd,dst_dir_fd=fd)
            with source_stream(fd,parts[-1]) as stream:raw,_=read_source(stream,name,MAX_FILE)
            return hashlib.sha256(raw).hexdigest()
        finally:
            try:os.unlink(tmp,dir_fd=fd)
            except FileNotFoundError:pass


def test_runtime():
    framework=Path(sys.prefix)/'Resources/Python.app/Contents/MacOS/Python'
    return framework if framework.exists() else Path(sys.executable).resolve()


def sandbox_profile(snapshot,scratch,runner):
    from sandbox_process import profile
    return profile(test_runtime(),('/Library/Developer/CommandLineTools',sys.base_prefix,snapshot),
                   (runner,), (scratch,))


def run_tests_snapshot(directory,files,target,timeout_ms,root=None):
    snapshot=directory/'snapshot';scratch=directory/'tmp';snapshot.mkdir();scratch.mkdir()
    for name,item in files.items():
        p=snapshot/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(item['text'].encode('utf-8'))
    runner=Path(__file__).parent/'unittest_entry.py';profile=sandbox_profile(snapshot,scratch,runner)
    (directory/'sandbox.sb').write_text(profile,encoding='utf-8')
    command=['/usr/bin/sandbox-exec','-f',str(directory/'sandbox.sb'),str(test_runtime()),'-I','-B',str(runner),str(snapshot),target]
    env={'PATH':str(test_runtime().parent),'HOME':str(scratch),'TMPDIR':str(scratch),'LANG':'en_US.UTF-8'}
    started=time.monotonic();data=bytearray();reason=None
    deadline=started+timeout_ms/1000
    next_resource_check=started+1
    proc=subprocess.Popen(command,cwd=snapshot,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
    sel=selectors.DefaultSelector();sel.register(proc.stdout,selectors.EVENT_READ)
    try:
        while sel.get_map():
            now=time.monotonic()
            if root is not None and next_resource_check<=now<deadline:
                health=resource_state(root,refresh=True,max_wait=deadline-now)
                now=time.monotonic()
                next_resource_check=now+1
                if not health['admitted']:reason='resource_wait'
            if now>=deadline:reason='timeout'
            if reason:
                os.killpg(proc.pid,signal.SIGKILL);break
            for key,_ in sel.select(min(.1,deadline-now)):
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


def execute(s,session,iid,workspace,root,resource_check=True,*,plan_digest=None):
    from lifecycle import timeout
    from storage_policy import require_space
    timeout(120);require_space(root)
    initialize(s)
    prior=s.db.execute('SELECT id FROM host_actions WHERE invocation_id=?',(iid,)).fetchone()
    if prior:
        row=s.db.execute('SELECT id,record_json FROM host_action_receipts WHERE action_id=?',(prior[0],)).fetchone()
        if not row:raise InvalidUpdate('Action is pending or interrupted; it will not be replayed automatically')
        result = project_receipt(s,prior[0])
        if result:
            if plan_digest!=result['plan_digest']:
                raise InvalidUpdate('Worktree receipt belongs to another approval')
            return dict(result,replayed_receipt=True)
        return dict(json.loads(row[1]),id=row[0],replayed_receipt=True)
    if s.db.execute('SELECT 1 FROM host_actions a LEFT JOIN host_action_receipts r ON a.id=r.action_id WHERE a.session=? AND r.action_id IS NULL',(session,)).fetchone():
        raise InvalidUpdate('An earlier action is pending; inspect it before executing another action')
    env,payload,output=verified_proposal(s,session,iid,workspace)
    action=output['action'];kind=action['kind'];constraints=s.snapshot(session)['constraints']
    if kind not in ('edit','run_tests','worktree_patch'):raise InvalidUpdate('No executable action in this proposal')
    if kind in ('run_tests','worktree_patch') and sys.platform!='darwin':
        raise InvalidUpdate('Sandboxed test execution is currently supported only on macOS')
    if kind=='edit' and os.name=='nt':raise InvalidUpdate('Atomic code edits are currently supported only on macOS/Linux; Windows supports read-only reviews')
    hardware=require_resources(root) if resource_check else {'profile':'unit-test-fixture'}
    files=files_at(s,session,workspace);versions={k:v['sha256'] for k,v in files.items()}
    if versions!=payload.get('source_versions'):raise InvalidUpdate('Scoped file versions changed since proposal')
    from native_access import read_scope
    change=proposed_edit(workspace,constraints,action,payload,read_scope=read_scope(s,session)) if kind=='edit' else None
    if kind=='run_tests' and (constraints.get('tests_allowed') is not True or action['target'] not in constraints.get('test_paths',[])):
        raise InvalidUpdate('Test execution is not permitted for this target')
    from orchestration import admit,action_flow,transition,scope
    workflow=action_flow(s,iid)
    plan=None
    if kind=='worktree_patch':
        from project_worktree import MAX_SCRATCH_BYTES, STAGING_SECONDS
        if not workflow or not isinstance(plan_digest,str):
            raise InvalidUpdate('Approve the exact worktree patch and project test plan')
        plan=project_plan(workspace,constraints,action,payload,read_scope(s,session))
        if sha(canonical(plan))!=plan_digest:
            raise InvalidUpdate('Worktree snapshot or patch changed after approval')
        require_space(root,plan['source_bytes']*3+2*1024**2+MAX_SCRATCH_BYTES)
        seconds=constraints['test_timeout_ms']/1000+STAGING_SECONDS
        if timeout(seconds) < seconds:
            raise InvalidUpdate('The bridge cannot complete the approved worktree action in its remaining time')
    target=plan['test_target'] if plan else action['target']
    paths=[item['target'] for item in plan['changes']] if plan else [target]
    cost={'actions':1,'seconds':constraints.get('test_timeout_ms',10000)/1000 if kind=='run_tests' else 1}
    if plan:
        cost.update(operations=1,source_bytes=plan['source_bytes'],seconds=seconds)
    origin=s.current_contract(session);aid=uuid.uuid4().hex
    with s.db:
        s.db.execute('BEGIN IMMEDIATE')
        used=sum(json.loads(r[0])['contract_hash']==origin['contract_hash'] for r in s.db.execute('SELECT record_json FROM host_actions WHERE session=?',(session,)))
        measured=budget_decision(used,constraints['max_actions'])
        if measured['decision']!='ALLOW':
            s._audit('ACTION_DENIED',{'session':session,'budget_kernel':measured});s.db.commit()
            raise InvalidUpdate('Action budget exhausted: '+measured['decision'])
        commit={'id':aid,'session':session,'invocation_id':iid,'model':env['model'],'kind':kind,'target':target,
                'contract_hash':origin['contract_hash'],'epoch':origin['epoch'],'logic_hash':s.logic_hash,
                'budget_kernel':measured,'resource_kernel':hardware,'artifact_versions':versions,
                'test_paths':sorted(constraints.get('test_paths',[])) if kind=='run_tests' else [],
                'before_hash':change['before_hash'] if change else None,'proposed_after_hash':change['after_hash'] if change else None,
                'proposal_hash':sha(canonical(output)),'started_at':time.time()}
        if plan:
            commit.update(base_commit=plan['base_commit'],project_sha256=plan['project_sha256'],
                          plan_digest=plan_digest,targets=paths,task_cost=cost)
        if workflow:
            transition(s,workflow,'EXECUTING_ACTION','ACTION_DISPATCH',{'action_id':aid})
            commit['workflow_id']=workflow
        owner=(env.get('orchestration') or {}).get('node_id')
        with scope(owner):
            commit['orchestration']=admit(s,session,kind,aid,cost,paths=paths,resource=hardware)
        raw=canonical(commit);s.db.execute('INSERT INTO host_actions VALUES(?,?,?,?,?)',(aid,session,iid,raw,sha('action-v1:'+raw)))
        s._audit('HOST_ACTION_DISPATCH',commit)
    directory=Path(root)/'actions'/aid
    execution=None;started=time.monotonic()
    try:
        directory.mkdir(mode=0o700,parents=True)
        if kind=='edit':
            (directory/'before.py').write_bytes(change['before'].encode());(directory/'after.py').write_bytes(change['after'].encode());(directory/'change.diff').write_text(change['diff'])
            after=replace_file(workspace,change)
            if after!=change['after_hash']:raise InvalidUpdate('Edited file changed before receipt')
            s.observe_digest(session,'action-'+aid,action['target'],after,s.snapshot(session)['revision'])
            result={'status':'applied','after_hash':after,'backup':str(directory/'before.py')}
        elif plan:
            from project_worktree import run
            result=run(workspace,directory,plan,constraints['test_timeout_ms'],resource_root=root if resource_check else None)
        else:
            execution=execution_identity(s,session,constraints)
            result=run_tests_snapshot(directory,files,action['target'],constraints['test_timeout_ms'],root if resource_check else None)
            if execution_identity(s,session,constraints)!=execution:
                result['status']='execution_changed'
            current={k:v['sha256'] for k,v in files_at(s,session,workspace).items()}
            result['artifact_versions']=versions
            if current!=versions:result['status']='stale_sources'
            # Keep the observation in this receipt. Do not promote output to
            # record_test / tool-authority assertion pages or completion proof.
    except Exception as exc:
        result={'status':'failed_or_uncertain','error':type(exc).__name__+': '+str(exc)}
    if kind=='run_tests':
        result['artifact_versions']=versions
        result['provenance']=run_provenance(action_history(s,session),action['target'],constraints.get('test_paths',[]),versions,result['status'],execution)
    elif plan:
        result.update(assertions_verified=False,elapsed_seconds=round(time.monotonic()-started,3))
    return receipt(s,commit,result)
