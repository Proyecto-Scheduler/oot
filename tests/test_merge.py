from oot.git.merge import merge3


def test_clean_merge_keeps_both_sides():
    r = merge3(base=b"a\nb\nc\n", kernel=b"A\nb\nc\n", patch=b"a\nb\nC\n")
    assert r.conflicts == 0
    assert r.content == b"A\nb\nC\n"


def test_patch_already_applied_returns_kernel_unchanged():
    r = merge3(base=b"a\n", kernel=b"b\n", patch=b"b\n")
    assert r.conflicts == 0
    assert r.content == b"b\n"


def test_overlapping_change_is_a_conflict_with_markers():
    r = merge3(base=b"a\nb\n", kernel=b"A\nb\n", patch=b"X\nb\n")
    assert r.conflicts == 1
    assert r.content is not None
    assert b"<<<<<<< kernel" in r.content
    assert b">>>>>>> patch" in r.content


def test_non_utf8_text_merges_as_bytes():
    r = merge3(base=b"\xe1\nx\nb\n", kernel=b"\xe1\nx\nB\n", patch=b"\xe9\nx\nb\n")
    assert r.conflicts == 0
    assert r.content == b"\xe9\nx\nB\n"


def test_binary_file_is_a_conflict_without_content():
    r = merge3(base=b"a\0b", kernel=b"a\0c", patch=b"a\0d")
    assert r.conflicts > 0
    assert r.content is None
