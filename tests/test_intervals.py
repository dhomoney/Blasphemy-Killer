from blasphemy_killer.match import Match, build_intervals


def m(start: float, end: float) -> Match:
    return Match(phrase="x", text="x", start=start, end=end)


def test_padding_applied():
    out = build_intervals([m(5.0, 6.0)], pad_before=0.15, pad_after=0.15)
    assert out == [(4.85, 6.15)]


def test_clamped_at_zero_and_duration():
    out = build_intervals([m(0.05, 9.95)], pad_before=0.2, pad_after=0.2, clamp_end=10.0)
    assert out == [(0.0, 10.0)]


def test_overlapping_intervals_merged():
    out = build_intervals([m(1.0, 2.0), m(1.8, 3.0)], pad_before=0.0, pad_after=0.0)
    assert out == [(1.0, 3.0)]


def test_near_intervals_merged_within_gap():
    out = build_intervals([m(1.0, 2.0), m(2.1, 3.0)], pad_before=0.0, pad_after=0.0, merge_gap=0.2)
    assert out == [(1.0, 3.0)]


def test_distant_intervals_not_merged():
    out = build_intervals([m(1.0, 2.0), m(5.0, 6.0)], pad_before=0.0, pad_after=0.0)
    assert out == [(1.0, 2.0), (5.0, 6.0)]


def test_unsorted_input_sorted_output():
    out = build_intervals([m(5.0, 6.0), m(1.0, 2.0)], pad_before=0.0, pad_after=0.0)
    assert out == [(1.0, 2.0), (5.0, 6.0)]


def test_empty():
    assert build_intervals([], pad_before=0.15, pad_after=0.15) == []
