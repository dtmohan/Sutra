"""Smriti domain: context capacity, page freshness, optional uncertain measurements."""
from kernel_boundary import KernelBoundary,measurement

class SmritiAdapter:
    name='smriti'
    def __init__(self,boundary=None):
        self.boundary=boundary if boundary is not None else KernelBoundary()
        if not isinstance(self.boundary,KernelBoundary):raise ValueError('Expected a validated kernel boundary')
    def frame(self,profile,omega,sigma):
        return self.boundary.frame(self.name,profile,omega,sigma)
    def freshness(self,age_seconds,ttl_seconds):
        age=measurement(age_seconds,'age_seconds',nonnegative=True)
        ttl=measurement(ttl_seconds,'ttl_seconds',positive=True)
        result=self.frame('page-freshness-v1',(2*self.boundary.k)*age/ttl,0.)
        # TTL is an exact expiry rule, independent of the kernel's band equality.
        result.update(age_seconds=age,ttl_seconds=ttl,expired=age>=ttl or result['decision']=='FIN')
        return result
    def context(self,input_charge,output_reserve,context_limit):
        for key,value in (('input_charge',input_charge),('output_reserve',output_reserve),('context_limit',context_limit)):
            if type(value) is not int or value<0:raise ValueError(key+' must be a nonnegative integer')
        if not context_limit:raise ValueError('context_limit must be positive')
        result=self.frame('context-capacity-v1',self.boundary.k*((input_charge+output_reserve)/context_limit),0.)
        result.update(input_charge=input_charge,output_reserve=output_reserve,context_limit=context_limit)
        return result
    def uncertain(self,store,session,profile_ref,sample):
        from calibration import evaluate
        return evaluate(store,session,self.name,profile_ref,sample)
