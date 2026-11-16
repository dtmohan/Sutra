"""Fresh local HTTP calls, full traces, conservative context admission."""
import http.client,json,pathlib,time
from paging import Budget
from legacy_model import EXTRACTOR, PATCH_SCHEMA, source_units, resolve_operations

class LocalModel:
    def __init__(self, log_dir, port=11435, model='qwen3:4b', health=None, budget=Budget(), thinking=False):
        self.log_dir=pathlib.Path(log_dir); self.log_dir.mkdir(parents=True,exist_ok=True)
        self.port=port; self.model=model; self.health=health; self.budget=budget; self.thinking=thinking
    def messages(self, system, payload):
        return [{'role':'system','content':system},
                {'role':'user','content':json.dumps(payload,sort_keys=True,separators=(',',':'))}]
    def output_schema(self, payload, host_schema):
        return host_schema
    def call(self, call_id, system, payload, schema):
        from invocation_gateway import consume_dispatch
        consume_dispatch(call_id, self.model, system, payload, schema)
        from reasoning_policy import check_provider
        check_provider(self, payload)
        charge=self.budget.check(system,payload,schema)
        if self.health:
            if self.health.problem(): raise RuntimeError(self.health.problem())
            self.health.phase=call_id
        from decoder_schema import for_decoder
        wire_schema = for_decoder(self.output_schema(payload, schema), getattr(self, 'decoder_compatibility', None))
        body={'model':self.model,'messages':self.messages(system,payload),
              'stream':True,'think':self.thinking,'format':wire_schema,'keep_alive':getattr(self,'keep_alive','5m'),
              'options':{'num_ctx':getattr(self,'runtime_context_tokens',self.budget.context_limit),'num_predict':self.budget.response_reserve,'temperature':0,'seed':42}}
        sensor=None
        try:
            from logit_sensor import Sensor
            sensor=Sensor(self.log_dir); body.update(sensor.request())
        except Exception:
            sensor=None  # Diagnostics must not prevent an otherwise governed call.
        record={'call_id':call_id,'request':body,'conservative_input_charge':charge,
                'decoder_compatibility':getattr(self,'decoder_compatibility',None)}
        from lifecycle import timeout
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=timeout(150)); started=time.monotonic()
        if self.health: self.health.active=c
        try:
            c.request('POST','/api/chat',json.dumps(body),{'Content-Type':'application/json'})
            r=c.getresponse()
            if r.status!=200: raise RuntimeError(f'HTTP {r.status}: {r.read(300).decode(errors="replace")}')
            from model_activity import ACTIVE
            activity=ACTIVE.get()
            if activity: activity.emit('control', requested=self.thinking,
                                       policy=payload.get('reasoning'))
            content=[]; reasoning=[]; total=0; response=None
            # Includes both reasoning and structured answer. This transport bound
            # does not replace num_predict or the gateway's reserved token budget.
            byte_limit=max(65536,self.budget.response_reserve*128)
            while True:
                remaining=150-(time.monotonic()-started)
                if remaining<=0: raise TimeoutError('Model transport deadline reached')
                if c.sock: c.sock.settimeout(timeout(remaining))
                raw=r.readline(65537)
                if not raw: break
                if len(raw)>65536: raise RuntimeError('Model stream frame exceeds transport limit')
                chunk=json.loads(raw)
                if chunk.get('error'): raise RuntimeError(str(chunk['error'])[:300])
                message=chunk.get('message',{})
                text=message.get('content',''); thought=message.get('thinking','')
                if not isinstance(text,str) or not isinstance(thought,str):
                    raise RuntimeError('Invalid model stream text')
                total+=len(text.encode())+len(thought.encode())
                if total>byte_limit: raise RuntimeError('Model stream exceeds transport allocation')
                content.append(text); reasoning.append(thought)
                if sensor:
                    try:sensor.observe(chunk)
                    except Exception:pass
                if activity and thought: activity.thinking(thought)
                if chunk.get('done'):
                    self.runtime_completed=True
                    response=dict(chunk); response.pop('logprobs',None)
                    response['message']={'role':'assistant','content':''.join(content),'thinking':''.join(reasoning)}
                    record['response']=response
                    break
            if response is None: raise RuntimeError('Incomplete generation: stream ended without final usage')
            if not response.get('done') or response.get('done_reason')=='length': raise RuntimeError('Incomplete generation')
            if response.get('prompt_eval_count',0)+self.budget.response_reserve>body['options']['num_ctx']:
                raise RuntimeError('Actual prompt exceeds admitted context budget')
            answer=json.loads(response['message']['content']); record['answer']=answer; record['ok']=True
        except Exception as exc:
            record['ok']=False; record['error']=str(exc); exc.call_record=record; raise
        finally:
            if self.health: self.health.active=None
            c.close(); record['elapsed_s']=time.monotonic()-started
            if sensor:
                try:
                    sensor.finish(self.log_dir,call_id,self.model,record,identity=getattr(self,'dispatch_identity',None))
                except Exception:pass
            from storage_policy import write_trace
            write_trace(self.log_dir,call_id,record)
        return answer,record
