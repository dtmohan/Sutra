"""Direct owned subprocess tests; no model weights, Strata Control or Ollama."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from engine_process import (EngineBusy, EngineCancelled, EngineError, EngineProcess,
                            EngineProtocolError, EngineTimeout, parse_done)


FAKE_ENGINE = r'''#!/usr/bin/env python3
import json,os,sys,threading,time
scenario=os.environ.get('FAKE_SCENARIO','basic')
log=os.environ['FAKE_COMMANDS']
stop=threading.Event()
running=threading.Event()
write_lock=threading.Lock()
def emit(line):
 with write_lock: print(line,flush=True)
if scenario=='ready_timeout': time.sleep(10)
if scenario=='startup_error': emit('ERR fixture model missing');sys.exit(2)
if scenario=='startup_truncated': sys.stdout.write('READY 8192');sys.stdout.flush();sys.exit(0)
if scenario=='startup_oversized': sys.stdout.write('X'*70000+'\n');sys.stdout.flush();sys.exit(0)
if scenario=='stderr':
 os.write(2,b'old:' + b'x'*100000 + b':latest')
emit('INFO engine=fake-1 kv=int8')
emit('READY '+('4096' if scenario=='wrong_context' else '8192')+
     ('' if scenario=='no_stop' else ' stop hold'))
counter=0
active=None
def run(number,fields):
 maximum=int(fields[1]);ids=fields[-1].split(',')
 if scenario=='exit':os._exit(2)
 emit(('REUSED ' if scenario=='reused' else 'RESUME ')+'0');emit('PP '+str(len(ids))+' '+str(len(ids))+' 1.5 100.0')
 if scenario=='bad_token':emit('T -1');return
 if scenario=='truncated_generation':
  sys.stdout.write('T 99');sys.stdout.flush();os._exit(0)
 emitted=0
 slow=scenario in ('cancel','no_stop','no_done') or (scenario=='timeout' and number==1)
 count=maximum if slow else min(maximum,3)
 for i in range(count):
  if stop.is_set():break
  emit('T '+str(number*100+i));emitted+=1
  if scenario=='lp':emit('LP -0.5 100:-0.5 101:-1.2')
  if slow:time.sleep(.05)
 if scenario=='no_done':
  while True:time.sleep(.1)
 if scenario=='error_tail':emit('ERR fixture request refused');emit('DONE 1 '+str(len(ids))+' 1 2 stop');return
 generated=emitted+1 if scenario=='wrong_counts' else emitted
 prompt=len(ids)+1 if scenario=='wrong_prompt' else len(ids)
 elapsed='nan' if scenario=='nonfinite' else '2.5'
 finish='cancel' if stop.is_set() else 'length' if emitted==maximum else 'stop'
 running.clear()
 emit('DONE '+str(generated)+' '+str(prompt)+' 1.5 '+elapsed+' '+finish+' 0 0 0 0 0 0 0 0.0 '+str(len(ids))+' 0 chunk=16 experts_streamed=0')
for raw in sys.stdin:
 with open(log,'a') as handle:handle.write(json.dumps(raw.strip())+'\n')
 fields=raw.split()
 if fields and fields[0]=='GEN':
  if running.is_set():emit('ERR concurrent request');continue
  stop.clear();counter+=1
  running.set()
  active=threading.Thread(target=run,args=(counter,fields),daemon=True);active.start()
 elif fields==['STOP']:stop.set()
 else:emit('ERR unsupported command')
'''


@unittest.skipIf(os.name == 'nt', 'Executable fake engine uses a Unix shebang')
class EngineProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.executable = self.root / 'fake-engine'
        self.executable.write_text(FAKE_ENGINE)
        self.executable.chmod(0o755)
        self.commands = self.root / 'commands.jsonl'

    def engine(self, scenario='basic', **extra):
        profile = {'executable': str(self.executable), 'args': [], 'cwd': str(self.root),
                   'context_tokens': 8192, 'env': {'FAKE_SCENARIO': scenario,
                                                  'FAKE_COMMANDS': str(self.commands)},
                   'drain_timeout': .2, 'shutdown_timeout': .5, **extra}
        engine = EngineProcess(profile)
        self.addCleanup(engine.stop)
        return engine

    def recorded(self):
        return [json.loads(line) for line in self.commands.read_text().splitlines()] if self.commands.exists() else []

    def test_ready_and_generation_probe_are_distinct_and_idempotent(self):
        engine = self.engine()
        first = engine.start()
        self.assertTrue(first['ready']);self.assertFalse(first['generation_ready'])
        self.assertEqual(first['info']['engine'],'fake-1')
        probed = engine.start(probe_token_ids=[7,9])
        self.assertEqual(probed['pid'],first['pid']);self.assertTrue(probed['generation_ready'])
        engine.start(probe_token_ids=[7,9])
        self.assertEqual(len(self.recorded()),1)
        self.assertTrue(self.recorded()[0].startswith('GEN 1 '))

    def test_generation_stream_progress_sampling_and_native_metrics(self):
        engine = self.engine('lp');engine.start()
        tokens,progress=[],[]
        result=engine.generate([4,5,6],3,temperature=.4,top_p=.9,top_k=16,seed=42,
                               on_token=tokens.append,on_progress=progress.append,hold=(2,100))
        self.assertEqual(tokens,[100,101,102]);self.assertEqual(result['tokens'],tokens)
        self.assertEqual(result['metrics']['generated'],3)
        self.assertEqual(result['metrics']['prompt_read'],3)
        self.assertEqual(result['metrics']['chunk'],16)
        self.assertEqual(result['prompt_tokens'],3)
        self.assertEqual(progress[0]['read'],3)
        command=self.recorded()[0]
        for key in ('temperature=0.4','top_p=0.9','top_k=16','seed=42','hold=2:100','4,5,6'):
            self.assertIn(key,command)

    def test_metal_reused_frame_is_a_bounded_cache_observation(self):
        engine=self.engine('reused');engine.start(probe_token_ids=[1])
        result=engine.generate([2,3],1)
        self.assertEqual(result['reused_tokens'],0)
        self.assertEqual(result['tokens'],[200])

    def test_cancellation_sends_one_stop_and_drains_before_reuse(self):
        engine=self.engine('cancel');engine.start()
        cancel=threading.Event();seen=[]
        def token(value):seen.append(value);cancel.set()
        result=engine.generate([1,2],30,cancel_event=cancel,on_token=token)
        self.assertTrue(result['cancelled']);self.assertEqual(len(seen),1)
        self.assertEqual(result['metrics']['generated'],len(result['tokens']))
        self.assertEqual(self.recorded().count('STOP'),1)
        following=engine.generate([3],1)
        self.assertEqual(following['tokens'],[200])
        self.assertTrue(engine.metadata()['ready'])

    def test_timeout_has_reconciled_usage_and_keeps_a_drained_stream(self):
        engine=self.engine('timeout');engine.start()
        with self.assertRaises(EngineTimeout) as caught:
            engine.generate([1,2],30,timeout=.12)
        result=caught.exception.engine_result
        self.assertTrue(result['cancelled']);self.assertGreater(result['metrics']['generated'],0)
        self.assertEqual(engine.generate([3],1)['tokens'],[200])

    def test_callback_failure_stops_drains_and_preserves_original_error(self):
        engine=self.engine('cancel');engine.start()
        def fail(_):raise LookupError('consumer left')
        with self.assertRaisesRegex(LookupError,'consumer left') as caught:
            engine.generate([1],20,on_token=fail)
        self.assertEqual(caught.exception.engine_result['finish'],'callback_error')
        self.assertTrue(engine.metadata()['ready'])
        self.assertEqual(engine.generate([1],1)['tokens'],[200])

    def test_request_deadline_without_done_poisoned_child_cannot_be_reused(self):
        engine=self.engine('no_done');engine.start()
        cancel=threading.Event()
        with self.assertRaisesRegex(EngineProtocolError,'drain DONE'):
            engine.generate([1],20,cancel_event=cancel,on_token=lambda _:cancel.set())
        self.assertFalse(engine.metadata()['running'])
        self.assertTrue(engine.metadata()['poisoned'])
        with self.assertRaises(EngineError):engine.generate([1],1)

    def test_unadvertised_cancellation_support_never_reuses_stream(self):
        engine=self.engine('no_stop');engine.start()
        cancel=threading.Event()
        with self.assertRaisesRegex(EngineProtocolError,'safely cancel'):
            engine.generate([1],20,cancel_event=cancel,on_token=lambda _:cancel.set())
        self.assertFalse(engine.metadata()['running'])
        self.assertNotIn('STOP',self.recorded())

    def test_invalid_generation_frames_and_counts_end_only_owned_child(self):
        for scenario in ('wrong_counts','wrong_prompt','bad_token','nonfinite','error_tail','truncated_generation','exit'):
            with self.subTest(scenario=scenario):
                engine=self.engine(scenario);engine.start()
                with self.assertRaises(EngineError):engine.generate([1,2],3)
                self.assertFalse(engine.metadata()['running'])
                with self.assertRaises(EngineError):engine.generate([1],1)

    def test_startup_bounds_wrong_context_and_early_exit(self):
        for scenario in ('wrong_context','startup_error','startup_oversized','startup_truncated'):
            with self.subTest(scenario=scenario):
                engine=self.engine(scenario)
                with self.assertRaises(EngineError):engine.start(timeout=1)
                self.assertFalse(engine.metadata()['running'])
        engine=self.engine('ready_timeout')
        with self.assertRaises(EngineTimeout):engine.start(timeout=.08)
        self.assertFalse(engine.metadata()['running'])

    def test_cold_start_can_be_cancelled_by_a_hardware_monitor(self):
        engine=self.engine('ready_timeout');cancel=threading.Event()
        timer=threading.Timer(.04,cancel.set);timer.start()
        started=time.monotonic()
        try:
            with self.assertRaises(EngineCancelled):engine.start(timeout=2,cancel_event=cancel)
        finally:timer.cancel()
        self.assertLess(time.monotonic()-started,.7)
        self.assertFalse(engine.metadata()['running'])

    def test_stop_cancels_a_generation_and_waits_for_its_own_done(self):
        engine=self.engine('cancel');engine.start()
        seen=threading.Event();results=[]
        thread=threading.Thread(target=lambda:results.append(engine.generate([1],30,on_token=lambda _:seen.set())))
        thread.start();self.assertTrue(seen.wait(1))
        engine.stop();thread.join(timeout=1)
        self.assertFalse(thread.is_alive());self.assertTrue(results[0]['cancelled'])
        self.assertFalse(engine.metadata()['running'])
        self.assertEqual(self.recorded().count('STOP'),1)

    def test_stop_also_interrupts_cold_ready_wait(self):
        engine=self.engine('ready_timeout');errors=[]
        def start():
            try:engine.start(timeout=2)
            except BaseException as error:errors.append(error)
        thread=threading.Thread(target=start);thread.start()
        deadline=time.monotonic()+1
        while not engine.metadata()['running'] and time.monotonic()<deadline:time.sleep(.005)
        started=time.monotonic();engine.stop();thread.join(timeout=1)
        self.assertFalse(thread.is_alive());self.assertLess(time.monotonic()-started,.7)
        self.assertIsInstance(errors[0],EngineCancelled)

    def test_precancelled_start_and_generation_have_no_commands(self):
        engine=self.engine();cancel=threading.Event();cancel.set()
        with self.assertRaises(EngineCancelled):engine.start(cancel_event=cancel)
        self.assertFalse(engine.metadata()['owned'])
        engine.start();pid=engine.metadata()['pid']
        with self.assertRaises(ValueError):engine.start(probe_token_ids=[True])
        self.assertEqual(engine.metadata()['pid'],pid)
        with self.assertRaises(EngineCancelled):engine.start(cancel_event=cancel)
        self.assertEqual(engine.metadata()['pid'],pid)
        result=engine.generate([1],3,cancel_event=cancel)
        self.assertFalse(result['request_sent']);self.assertEqual(self.recorded(),[])

    def test_bounded_stderr_survives_without_deadlocking_start(self):
        engine=self.engine('stderr');result=engine.start()
        self.assertLessEqual(len(result['stderr_tail'].encode()),32768)
        # Reader completion may trail READY very briefly, so stop joins it first.
        engine.stop();self.assertTrue(engine.metadata()['stderr_tail'].endswith(':latest'))

    def test_concurrent_and_reentrant_generation_cannot_share_a_pipe(self):
        engine=self.engine('cancel');engine.start()
        seen=threading.Event();cancel=threading.Event();results=[]
        thread=threading.Thread(target=lambda:results.append(engine.generate([1],30,on_token=lambda _:seen.set(),cancel_event=cancel)))
        thread.start();self.assertTrue(seen.wait(1))
        with self.assertRaises(EngineBusy):engine.generate([2],1)
        cancel.set();thread.join(timeout=2);self.assertFalse(thread.is_alive())
        self.assertTrue(results[0]['cancelled'])
        nested=[]
        def callback(_):
            with self.assertRaises(EngineBusy):engine.generate([2],1)
            nested.append(True)
        engine.generate([1],1,on_token=callback)
        self.assertEqual(nested,[True])

    def test_stop_is_idempotent_and_does_not_end_an_unrelated_process(self):
        other=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'])
        try:
            engine=self.engine();engine.start();owned=engine._proc
            engine.stop();engine.stop()
            self.assertIsNotNone(owned.poll());self.assertIsNone(other.poll())
            loaded=engine.start(probe_token_ids=[1])
            self.assertTrue(loaded['generation_ready']);self.assertNotEqual(loaded['pid'],owned.pid)
        finally:
            other.terminate();other.wait(timeout=5)

    def test_profile_and_request_values_rejected_before_dispatch(self):
        for change in ({'executable':'relative'}, {'args':['--batch','2']}, {'env':{'BAD\nKEY':'x'}}, {'args':['--foo\nGEN 1 9']}):
            with self.subTest(change=change),self.assertRaises(ValueError):self.engine(**change)
        engine=self.engine();engine.start()
        for tokens,maximum,extra in (([True],1,{}),([1],True,{}),([1]*8192,1,{}),([1],1,{'seed':float('nan')})):
            with self.subTest(tokens=tokens[:1],maximum=maximum),self.assertRaises(ValueError):engine.generate(tokens,maximum,**extra)
        self.assertEqual(self.recorded(),[])


class DoneFrameTests(unittest.TestCase):
    def test_positional_and_extended_metrics(self):
        result=parse_done('DONE 2 3 1.5 2.5 stop 0 0 1 2 3 4 5 6.5 2 7 chunk=16 experts_streamed=8')
        self.assertEqual(result['generated'],2);self.assertEqual(result['file_mb'],6.5)
        self.assertEqual(result['prompt_read'],2);self.assertEqual(result['experts_streamed'],8)

    def test_bad_or_nonfinite_counts_never_become_usage(self):
        for line in ('DONE','DONE -1 2 1 2 stop','DONE 1 2 nan 2 stop','DONE 1 2 1 inf stop',
                     'DONE 1 2 1 2 nonfinite','DONE 1 2 1 2 stop chunk=-1'):
            with self.subTest(line=line),self.assertRaises(EngineProtocolError):parse_done(line)


if __name__=='__main__':unittest.main()
