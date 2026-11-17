"""Karma domain: scoped work and evidence admission, budgets and hardware."""
from kernel_boundary import KernelBoundary,measurement


class StaleHardwareSample(ValueError):
    """A valid admission sample has aged out and needs a new observation."""


class KarmaAdapter:
    name='karma'
    def __init__(self,boundary=None):
        self.boundary=boundary if boundary is not None else KernelBoundary()
        if not isinstance(self.boundary,KernelBoundary):raise ValueError('Expected a validated kernel boundary')
    def frame(self,profile,omega,sigma):
        return self.boundary.frame(self.name,profile,omega,sigma)
    def route(self,prompt,models,mode,signals):
        from model_routing import choose
        return choose(prompt,models,mode,signals)
    def counted_slots(self,used,limit):
        if type(used) is not int or type(limit) is not int or used<0 or limit<=0:
            raise ValueError('Budget counts must be nonnegative integers with a positive limit')
        return dict(self.frame('counted-slots-v1',0.,self.boundary.sigma_clause*used/limit),used=used,limit=limit)
    def task_transition(self,cursor_matches,target_matches,checkpoint_clear):
        signals=dict(cursor_matches=cursor_matches,target_matches=target_matches,checkpoint_clear=checkpoint_clear)
        if any(type(value) is not bool for value in signals.values()):
            raise ValueError('Task transition signals must be boolean')
        if not cursor_matches or not target_matches:
            deviation=3*self.boundary.k
        elif not checkpoint_clear:
            deviation=2*self.boundary.k
        else:
            deviation=0.
        return dict(self.frame('task-focus-v1',deviation,0.),**signals)
    def evidence_admission(self, *, target_status, evidence_status, recovery_possible, budget_exhausted=False):
        """Map host-verified target, evidence and recovery facts into STP."""
        if target_status not in ('resolved', 'missing', 'invalid'):
            raise ValueError('Unknown evidence target status')
        if evidence_status not in ('available', 'missing', 'stale', 'invalid'):
            raise ValueError('Unknown evidence availability status')
        if type(recovery_possible) is not bool or type(budget_exhausted) is not bool:
            raise ValueError('Evidence admission requires boolean recovery and exhaustion signals')

        if target_status == 'invalid' or evidence_status == 'invalid':
            omega = 3 * self.boundary.k
        elif target_status == 'resolved' and evidence_status == 'available':
            omega = 0.
        elif recovery_possible:
            omega = 2 * self.boundary.k
        else:
            omega = 3 * self.boundary.k
        sigma = self.boundary.sigma_clause if budget_exhausted else 0.
        return dict(self.frame('evidence-admission-v1', omega, sigma),
                    target_status=target_status, evidence_status=evidence_status,
                    recovery_possible=recovery_possible, budget_exhausted=budget_exhausted)

    def allocations(self,used,limits,cost=None):
        cost=cost or {}
        if not limits or set(used)!=set(limits) or set(cost)-set(limits):raise ValueError('Allocation dimensions differ')
        for key in limits:
            measurement(limits[key],key+' limit',positive=True)
            measurement(used[key],key+' used',nonnegative=True)
            measurement(cost.get(key,0),key+' requested',nonnegative=True)
        sigma=max(used[k]/limits[k] for k in limits)
        fractions={k:cost.get(k,0)/max(limits[k]-used[k],1e-9) for k in limits}
        result=self.frame('task-allocations-v1',self.boundary.k*max(fractions.values()),self.boundary.sigma_clause*sigma)
        return dict(result,used=dict(used),limits=dict(limits),reserved_next=dict(cost),
            limiting_dimension=max(limits,key=lambda k:used[k]/limits[k]) if sigma>=1 else max(fractions,key=fractions.get),
            token_policy='reserve model-packet input plus output; new completed calls settle to runtime counts; missing counts and historical calls retain reservations')
    def storage(self,used_bytes,limit_bytes,reserved_bytes=0):
        for key,value in (('used_bytes',used_bytes),('limit_bytes',limit_bytes),('reserved_bytes',reserved_bytes)):
            if type(value) is not int or value<0:raise ValueError(key+' must be a nonnegative integer')
        if not limit_bytes:raise ValueError('Storage limit must be positive')
        return dict(self.frame('storage-allocation-v1',self.boundary.k*reserved_bytes/max(1,limit_bytes-used_bytes),self.boundary.sigma_clause*used_bytes/limit_bytes),
                    used_bytes=used_bytes,limit_bytes=limit_bytes,reserved_next_bytes=reserved_bytes)
    def hardware(self,sample,now):
        age=measurement(now,'now')-measurement(sample['wall_time'],'wall_time')
        if age<0:raise ValueError('Hardware sample is future-dated')
        portable=sample.get('profile')=='portable-memory-v1'
        if portable:
            memory=measurement(sample['available_bytes'],'available_bytes',nonnegative=True)
            total=measurement(sample['physical_memory_bytes'],'physical_memory_bytes',positive=True)
            if memory>total:raise ValueError('Invalid portable memory measurement')
            omega=3*self.boundary.k if memory<4*1024**3 else 0.
        else:
            thermal=sample['thermal_state'];pressure=sample['memory_pressure_raw']
            if type(thermal) is not int or thermal not in range(4):raise ValueError('Invalid thermal state')
            if type(pressure) is not int or pressure not in (1,2,4):raise ValueError('Invalid memory pressure')
            free=measurement(sample['free_bytes'],'free_bytes',nonnegative=True)
            inactive=measurement(sample['inactive_bytes'],'inactive_bytes',nonnegative=True)
            memory=measurement(free+inactive,'reclaimable memory',nonnegative=True)
            omega=max({0:0.,1:0.,2:2*self.boundary.k,3:3*self.boundary.k}[thermal],{1:0.,2:2*self.boundary.k,4:3*self.boundary.k}[pressure],3*self.boundary.k if memory<4*1024**3 else 0.)
        if age>8:raise StaleHardwareSample('Hardware sample is stale')
        result=self.frame('portable-memory-v1' if portable else 'hardware-v1',omega,0.)
        result.update(available=True,age_seconds=round(age,2),sample=sample,admitted=result['decision']=='ALLOW',
            reclaimable_estimate_gib=round(memory/1024**3,2),cpu_policy='advisory only',temperature_celsius=None,
            thermal_label='unavailable' if portable else ('nominal','fair','serious','critical')[sample['thermal_state']],
            thermal_source='unavailable' if portable else 'NSProcessInfo.thermalState',
            temperature_status='unavailable',
            temperature_reason='Celsius unavailable: this portable monitor has no validated temperature sensor.' if portable else 'Celsius unavailable: NSProcessInfo reports thermal pressure, not degrees. No validated CPU/GPU sensor is configured.',
            reason='normal' if result['decision']=='ALLOW' else 'thermal or memory pressure requires resource recovery')
        if portable:result.update(thermal_policy='unavailable; no thermal decision',reason='measured memory available; thermal and pressure signals unavailable' if result['admitted'] else 'low available memory requires resource recovery')
        from temperature_signals import summarize
        result['temperatures']=summarize(sample.get('temperatures'),now)
        result['temperature_status']='available' if result['temperatures']['available'] else 'unavailable'
        if result['temperatures']['available']:result['temperature_reason']='CPU/GPU values are measured AppleSMC sensor maxima. OS thermal pressure is shown separately.'
        return result
    def memory_allocation(self,incremental_bytes,available_bytes,headroom_bytes):
        for name,value in (('incremental',incremental_bytes),('available',available_bytes),('headroom',headroom_bytes)):
            measurement(value,name,nonnegative=True)
        usable=max(0,available_bytes-headroom_bytes)
        omega=self.boundary.k*incremental_bytes/max(1,usable)
        if not usable:omega=max(omega,2*self.boundary.k)
        return dict(self.frame('runtime-memory-v1',omega,0.),available_bytes=available_bytes,
                    system_reserve_bytes=headroom_bytes,usable_bytes=usable)
    def uncertain(self,store,session,profile_ref,sample):
        from calibration import evaluate
        return evaluate(store,session,self.name,profile_ref,sample)

def budget_decision(used,limit):return KarmaAdapter().counted_slots(used,limit)
