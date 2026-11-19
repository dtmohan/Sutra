"""Shared numeric validation only. Domain policy belongs to the two adapters."""
from dataclasses import dataclass
import math
import time
from numbers import Real
from stp_canonical.kernel import STPKernel as CanonicalKernel

def measurement(value, name, positive=False, nonnegative=False):
    if isinstance(value,bool) or not isinstance(value,Real):raise ValueError(name+' must be a real number')
    try:number=float(value)
    except (ValueError,OverflowError):raise ValueError(name+' must be finite') from None
    if not math.isfinite(number):raise ValueError(name+' must be finite')
    if positive and number<=0:raise ValueError(name+' must be positive')
    if nonnegative and number<0:raise ValueError(name+' must be nonnegative')
    return number

@dataclass(frozen=True)
class KernelBoundary:
    k:float=.15
    sigma_clause:float=1.
    def __post_init__(self):
        object.__setattr__(self,'k',measurement(self.k,'k',positive=True))
        object.__setattr__(self,'sigma_clause',measurement(self.sigma_clause,'sigma_clause',positive=True))
        if not math.isfinite(2*self.k):raise ValueError('2*k must be finite')
    def evaluate(self,omega,sigma):
        omega=measurement(omega,'omega',nonnegative=True)
        sigma=measurement(sigma,'sigma')
        kernel=CanonicalKernel(self.k,self.sigma_clause)
        started=time.perf_counter_ns()
        result=kernel.evaluate(omega,sigma)
        elapsed=time.perf_counter_ns()-started
        from accounting_metrics import record
        record(None,'kernel',elapsed,decision=result.decision.value)
        return result
    def frame(self,adapter,profile,omega,sigma):
        v=self.evaluate(omega,sigma)
        return {'adapter':adapter,'profile':profile,'profile_version':1,'omega':v.omega,
                'sigma':v.sigma,'k':v.k,'sigma_clause':self.sigma_clause,'decision':v.decision.value}

def frame(adapter,profile,omega,sigma,*,k=.15,sigma_clause=1.):
    return KernelBoundary(k,sigma_clause).frame(adapter,profile,omega,sigma)
