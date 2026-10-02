from controller.features import eligible, opcode_features
from controller.app import effective_features


def test_decode():
    assert opcode_features("4801d8") == set()                 # add rax, rbx
    assert opcode_features("c5f158c2") == {"AVX"}             # vaddpd xmm0,xmm1,xmm2
    assert "SSE2" in opcode_features("660f58c1")              # addpd
    assert opcode_features("d9c1") == {"X87"}                 # fxch / fld st(1), FPU alias
    assert opcode_features("06") is None                      # invalid in 64-bit mode


def test_eligible():
    assert eligible([], [])
    assert eligible(["AVX"], ["avx", "SSE"])
    assert not eligible(["AVX", "AVX2"], ["AVX"])


def test_effective_features():
    assert effective_features(["AVX", "SSE", "fpu"], ["AVX", "BMI1"]) == ["AVX"]
    assert effective_features(["AVX"], []) == ["AVX"]
