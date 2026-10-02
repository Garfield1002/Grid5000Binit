"""Opcode -> iced-x86 CpuidFeature names."""
from iced_x86 import CpuidFeature, Decoder

_NAMES = {v: k for k, v in vars(CpuidFeature).items() if k.isupper() and isinstance(v, int)}
# Baseline features every x86-64 CPU has; never required from a node.
BASELINE = {"INTEL8086", "INTEL186", "INTEL286", "INTEL386", "INTEL486", "X64"}
ALIASES = {"FPU": "X87", "FPU287": "X87", "FPU387": "X87", "FPU287XL": "X87"}


def normalize(name: str) -> str:
    return ALIASES.get(name.upper(), name.upper())


def opcode_features(opcode_hex: str) -> set[str] | None:
    """Features required by the first instruction in opcode_hex; None if undecodable."""
    code = bytes.fromhex(opcode_hex)
    ins = Decoder(64, code, ip=0).decode()
    if ins.is_invalid:
        return None
    out = set()
    for f in ins.cpuid_features():
        n = _NAMES[f]
        if n not in BASELINE:
            out.add(normalize(n))
    return out


def eligible(required, node_features) -> bool:
    return {normalize(r) for r in required} <= {normalize(f) for f in node_features}
