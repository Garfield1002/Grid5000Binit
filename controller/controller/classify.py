"""Mismatch classification. Pure functions, no DB access.

Flag representation (x86db): state["flag"] is the integer EFLAGS/RFLAGS value;
instruction_undefined_flags.flag holds names OF SF ZF AF CF PF, mapped to the
architectural EFLAGS bit positions below. All other keys are compared exactly.
"""

FLAG_BITS = {"CF": 0, "PF": 2, "AF": 4, "ZF": 6, "SF": 7, "OF": 11}

UNDEF_FLAGS_ONLY = "undef_flags_only"
DEFINED_STATE = "defined_state"
EXCEPTION_MISMATCH = "exception_mismatch"
CRASH = "crash"
SKIPPED = "skipped"
CLASSES = (UNDEF_FLAGS_ONLY, DEFINED_STATE, EXCEPTION_MISMATCH, CRASH)


def undefined_mask(flags) -> int:
    m = 0
    for f in flags or ():
        m |= 1 << FLAG_BITS[f.strip().upper()]
    return m


def diff_keys(expected: dict | None, got: dict | None) -> list[str]:
    expected = expected or {}
    got = got or {}
    return sorted(k for k in set(expected) | set(got) if expected.get(k, _MISSING) != got.get(k, _MISSING))


_MISSING = object()


def uncaptured_in_fxsave(key: str) -> bool:
    """Keys the kernel cannot capture in FXSAVE mode: ymmN, zmmN, kN, opmask*."""
    for p in ("ymm", "zmm", "k"):
        if key.startswith(p) and key[len(p):].isdigit():
            return True
    return key.startswith("opmask")


def strip_uncaptured(state: dict | None) -> dict | None:
    if state is None:
        return None
    return {k: v for k, v in state.items() if not uncaptured_in_fxsave(k)}


def classify(status, expected_state, expected_exc, got_state, got_exc, undefined_flags,
             save_mode=None):
    """Return (class, differing_keys). In fxsave mode the wide vector/opmask keys
    were not captured and are excluded from the comparison (nothing else is)."""
    if save_mode == "fxsave":
        expected_state = strip_uncaptured(expected_state)
        got_state = strip_uncaptured(got_state)
    if status == "crash":
        return CRASH, []
    if status == "skipped":
        return SKIPPED, []
    if (expected_exc or None) != (got_exc or None):
        return EXCEPTION_MISMATCH, []
    keys = diff_keys(expected_state, got_state)
    if keys == ["flag"]:
        e, g = expected_state["flag"], got_state["flag"]
        if (int(e) ^ int(g)) & ~undefined_mask(undefined_flags) == 0:
            return UNDEF_FLAGS_ONLY, keys
    return DEFINED_STATE, keys
