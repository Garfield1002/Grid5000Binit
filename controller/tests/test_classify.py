from controller.classify import (CRASH, DEFINED_STATE, EXCEPTION_MISMATCH, SKIPPED,
                                 UNDEF_FLAGS_ONLY, classify, undefined_mask)

CF, PF, AF, ZF, SF, OF = (1 << b for b in (0, 2, 4, 6, 7, 11))


def st(**kw):
    return {"rax": 1, "rbx": 2, "flag": 0x2, **kw}


def test_mask():
    assert undefined_mask(["AF", "OF"]) == AF | OF
    assert undefined_mask([]) == 0


def test_undef_flags_only():
    c, k = classify("mismatch", st(), None, st(flag=0x2 | AF), None, ["AF", "SF"])
    assert (c, k) == (UNDEF_FLAGS_ONLY, ["flag"])


def test_defined_flag_differs():
    c, _ = classify("mismatch", st(), None, st(flag=0x2 | CF | AF), None, ["AF"])
    assert c == DEFINED_STATE


def test_no_undefined_flags_means_defined():
    c, _ = classify("mismatch", st(), None, st(flag=0x2 | AF), None, [])
    assert c == DEFINED_STATE


def test_register_and_undef_flag_is_defined_state():
    c, k = classify("mismatch", st(), None, st(rax=9, flag=0x2 | AF), None, ["AF"])
    assert c == DEFINED_STATE and k == ["flag", "rax"]


def test_missing_key_is_diff():
    g = st()
    del g["rbx"]
    c, k = classify("mismatch", st(), None, g, None, ["AF"])
    assert c == DEFINED_STATE and k == ["rbx"]


def test_nonstatus_flag_bit_is_defined():
    c, _ = classify("mismatch", st(), None, st(flag=0x2 | 0x400), None, ["AF", "CF", "PF", "ZF", "SF", "OF"])
    assert c == DEFINED_STATE


def test_exception():
    assert classify("mismatch", st(), None, None, "GP", [])[0] == EXCEPTION_MISMATCH
    assert classify("mismatch", None, "UD", st(), None, [])[0] == EXCEPTION_MISMATCH
    assert classify("mismatch", None, "UD", None, "GP", [])[0] == EXCEPTION_MISMATCH


def test_crash_skipped():
    assert classify("crash", st(), None, None, None, [])[0] == CRASH
    assert classify("skipped", st(), None, None, None, [])[0] == SKIPPED


def test_fxsave_ignores_uncaptured_keys():
    from controller.classify import classify, DEFINED_STATE, UNDEF_FLAGS_ONLY
    exp = {"xmm0": "aa", "ymm0": "bb", "zmm1": 5, "k1": 3, "flag": 2}
    got = {"xmm0": "aa", "ymm0": "00", "zmm1": 0, "flag": 2}
    assert classify("mismatch", exp, None, got, None, [])[0] == DEFINED_STATE
    assert classify("mismatch", exp, None, got, None, [], "fxsave") == ("defined_state", [])
    got2 = {**got, "flag": 18}
    assert classify("mismatch", exp, None, got2, None, ["AF"], "fxsave")[0] == UNDEF_FLAGS_ONLY
    assert classify("mismatch", exp, None, {**got, "xmm0": "ab"}, None, [], "fxsave")[1] == ["xmm0"]
