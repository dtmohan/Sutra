"""SMRITI kernel — the one static component.

K(omega, sigma) -> Decision. Context-free, deterministic, self-applicable.
The kernel does not know it is grading memory. It receives dimensionless
ratios and returns the arithmetically correct output. The locus of error
is always in the instantiation, never in the computation.

Three-Gap band (within-instance): omega <= k -> ALLOW
                                  k < omega <= 2k -> BRIDGE
                                  omega > 2k -> FIN
Compound (between-level): |sigma| >= sigma_clause -> COMPOUND_FIN
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum


class Decision(str, Enum):
    ALLOW = "ALLOW"
    BRIDGE = "BRIDGE"
    FIN = "FIN"
    COMPOUND_FIN = "COMPOUND_FIN"


@dataclass(frozen=True)
class KernelOutput:
    decision: Decision
    omega: float
    sigma: float
    k: float


class STPKernel:
    """Static governor. Never subclass with domain logic; adapt outside."""

    def __init__(self, k: float = 0.15, sigma_clause: float = 1.0):
        # BOOTSTRAP constants (RQ4 discipline): conservative priors, to be
        # replaced by empirical derivation once the audit log accumulates
        # enough natural closes. They carry their own expiry.
        self.k = k
        self.sigma_clause = sigma_clause

    def evaluate(self, omega: float, sigma: float) -> KernelOutput:
        omega = max(0.0, float(omega))
        if abs(sigma) >= self.sigma_clause:
            return KernelOutput(Decision.COMPOUND_FIN, omega, sigma, self.k)
        if omega <= self.k:
            return KernelOutput(Decision.ALLOW, omega, sigma, self.k)
        if omega <= 2 * self.k:
            return KernelOutput(Decision.BRIDGE, omega, sigma, self.k)
        return KernelOutput(Decision.FIN, omega, sigma, self.k)
