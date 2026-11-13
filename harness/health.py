"""Optional native telemetry guard for the experiment's private server only."""
import json, pathlib, subprocess, threading, time

class HealthMonitor:
    def __init__(self, root):
        self.latest=None; self.phase='startup'; self.active=None
        self.proc=subprocess.Popen([str(root/'telemetry')],stdout=subprocess.PIPE,text=True)
        self.path=root/'health.jsonl'
        self.thread=threading.Thread(target=self.read,daemon=True); self.thread.start()
    def problem(self):
        d=self.latest
        if not d or time.monotonic()-d['received_monotonic']>5: return 'missing or stale telemetry'
        if d.get('memory_pressure_raw',4)>=4: return 'critical memory pressure'
        if d.get('thermal_state',3)>=3: return 'critical thermal state'
        # macOS caches can leave little strictly-free RAM under normal pressure.
        # Inactive is a reclaimable estimate, not guaranteed available capacity.
        if d.get('free_bytes',0)+d.get('inactive_bytes',0)<4*1024**3:
            return 'less than 4 GiB free plus inactive memory'
        return None
    def read(self):
        with self.path.open('w') as f:
            for line in self.proc.stdout:
                d=json.loads(line); d['received_monotonic']=time.monotonic(); d['phase']=self.phase
                self.latest=d; f.write(json.dumps(d)+'\n'); f.flush()
                if self.problem() and self.active:
                    try:
                        if self.active.sock: self.active.sock.shutdown(2)
                        self.active.close()
                    except OSError: pass
    def close(self):
        self.proc.terminate(); self.proc.wait(timeout=5); self.thread.join(timeout=5)
