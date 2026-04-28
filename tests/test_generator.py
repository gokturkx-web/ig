from ig_scanner.scanner import generate_combinations, ALPHABETS


def test_alnum_no_special():
    out = list(generate_combinations(2, "alnum"))
    # 36 * 36 = 1296
    assert len(out) == 36 * 36
    assert out[0] == "aa"
    assert out[-1] == "99"
    assert all("." not in u and "_" not in u for u in out)


def test_full_excludes_leading_or_trailing_special():
    out = list(generate_combinations(3, "full"))
    for u in out:
        assert u[0] not in "._"
        assert u[-1] not in "._"
        assert ".." not in u
    # Hepsi 3 karakter
    assert all(len(u) == 3 for u in out)


def test_alphabets_present():
    for name in ("full", "alnum", "alpha", "digits"):
        assert name in ALPHABETS
