"""Fresh local HTTP calls, full traces, conservative context admission."""
import http.client,json,pathlib,time
from paging import Budget
from legacy_model import EXTRACTOR, PATCH_SCHEMA, source_units, resolve_operations

class OutputLimitExceeded(RuntimeError):
    """A completed transport reported exhaustion, not a usable final answer."""

class StructuredOutputRejected(RuntimeError):
    """The backend rejected the generated proposal; bounded repair is possible."""


def transport_observation(record):
    """Bounded host transport facts; no inferred queue state or token counts."""
    state=record.get('transport') if isinstance(record,dict) else None
    if not isinstance(state,dict) or type(state.get('version')) is not int or state['version']!=1:return None
    phase=state.get('phase');headers=state.get('headers_received')
    if phase not in ('connect','response_headers','stream','decode','complete') or type(headers) is not bool:return None
    counts={key:state.get(key) for key in ('frames_received','content_bytes','reasoning_bytes')}
    if any(type(value) is not int or not 0<=value<=2**53-1 for value in counts.values()):return None
    if phase in ('connect','response_headers') and (headers or any(counts.values())):return None
    if phase in ('stream','decode','complete') and not headers:return None
    if not counts['frames_received'] and (counts['content_bytes'] or counts['reasoning_bytes']):return None
    return dict(version=1,phase=phase,headers_received=headers,**counts)


class LocalModel:
    model_interface = 1
    transport_deadline_cap = 360
    def __init__(self, log_dir, port=11435, model='qwen3:4b', health=None, budget=Budget(), thinking=False):
        self.log_dir=pathlib.Path(log_dir); self.log_dir.mkdir(parents=True,exist_ok=True)
        self.port=port; self.model=model; self.health=health; self.budget=budget; self.thinking=thinking
    def dispatch_gate(self, root, call_id, system, payload, schema):
        from runtime_memory import dispatch
        return dispatch(self, root, call_id, system, payload, schema)
    def messages(self, system, payload):
        return [{'role':'system','content':system},
                {'role':'user','content':json.dumps(payload,sort_keys=True,separators=(',',':'))}]
    def output_schema(self, payload, host_schema):
        return host_schema
    def call(self, call_id, system, payload, schema):
        from invocation_gateway import consume_dispatch
        from model_packet import build
        try:
            packet = build(system, payload, schema)
        except (KeyError, TypeError, ValueError):
            from invocation_gateway import check_dispatch
            check_dispatch(call_id, self.model, system, payload, schema)
            raise
        consume_dispatch(call_id, self.model, system, payload, schema, packet=packet)
        from reasoning_policy import check_provider
        check_provider(self, payload)
        charge=self.budget.check(system,payload,schema)
        if self.health:
            if self.health.problem(): raise RuntimeError(self.health.problem())
            self.health.phase=call_id
        from decoder_schema import for_decoder
        wire_schema = for_decoder(packet['schema'] if packet else self.output_schema(payload, schema), getattr(self, 'decoder_compatibility', None))
        body={'model':self.model,'messages':packet['messages'] if packet else self.messages(system,payload),
              'stream':True,'think':self.thinking,'format':wire_schema,'keep_alive':getattr(self,'keep_alive','5m'),
              'options':{'num_ctx':getattr(self,'runtime_context_tokens',self.budget.context_limit),'num_predict':self.budget.response_reserve,'temperature':0,'seed':42}}
        sensor=None
        try:
            from logit_sensor import Sensor
            sensor=Sensor(self.log_dir); body.update(sensor.request())
        except Exception:
            sensor=None  # Diagnostics must not prevent an otherwise governed call.
        transport={'version':1,'phase':'connect','headers_received':False,'frames_received':0,
                   'content_bytes':0,'reasoning_bytes':0}
        record={'call_id':call_id,'request':body,'conservative_input_charge':charge,'transport':transport,
                'decoder_compatibility':getattr(self,'decoder_compatibility',None)}
        from lifecycle import timeout
        from invocation_gateway import transport_seconds, transport_timeout
        from runtime_memory import mark_transport_started
        encoded_body = json.dumps(body)
        call_seconds=transport_seconds()
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=timeout(call_seconds)); started=time.monotonic()
        if self.health: self.health.active=c
        try:
            c.connect()
            mark_transport_started(self, call_id)
            c.request('POST','/api/chat',encoded_body,{'Content-Type':'application/json'})
            transport['phase']='response_headers'
            r=c.getresponse()
            transport.update(headers_received=True,phase='stream')
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
                remaining=transport_timeout(started)
                if c.sock: c.sock.settimeout(remaining)
                raw=r.readline(65537)
                if not raw: break
                if len(raw)>65536: raise RuntimeError('Model stream frame exceeds transport limit')
                chunk=json.loads(raw)
                transport['frames_received']+=1
                if chunk.get('error'): raise RuntimeError(str(chunk['error'])[:300])
                message=chunk.get('message',{})
                text=message.get('content',''); thought=message.get('thinking','')
                if not isinstance(text,str) or not isinstance(thought,str):
                    raise RuntimeError('Invalid model stream text')
                total+=len(text.encode())+len(thought.encode())
                transport['content_bytes']+=len(text.encode())
                transport['reasoning_bytes']+=len(thought.encode())
                if total>byte_limit: raise RuntimeError('Model stream exceeds transport allocation')
                content.append(text); reasoning.append(thought)
                if sensor:
                    try:sensor.observe(chunk)
                    except Exception:pass
                if activity and thought: activity.thinking(thought)
                if chunk.get('done') is True:
                    self.runtime_completed=True
                    response=dict(chunk); response.pop('logprobs',None)
                    response['message']={'role':'assistant','content':''.join(content),'thinking':''.join(reasoning)}
                    record['response']=response
                    break
            if response is None: raise RuntimeError('Incomplete generation: stream ended without final usage')
            if response.get('done_reason')=='length':
                raise OutputLimitExceeded('Incomplete generation: output allowance exhausted before a complete answer; '
                                          'reasoning and answer share the output allowance')
            from token_usage import count
            input_tokens = count(response.get('prompt_eval_count'))
            output_tokens = count(response.get('eval_count'))
            if output_tokens is not None and output_tokens > self.budget.response_reserve:
                raise OutputLimitExceeded('Actual generation exceeds the admitted output allocation')
            if input_tokens is not None and input_tokens+self.budget.response_reserve>body['options']['num_ctx']:
                raise RuntimeError('Actual prompt exceeds admitted context budget')
            transport['phase']='decode'
            answer=json.loads(response['message']['content']); record['answer']=answer; record['ok']=True
            transport['phase']='complete'
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
