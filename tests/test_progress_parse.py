"""ffmpeg `-progress` parsing, from canned output: no ffmpeg needed."""

from blasphemy_killer.media import ProgressParser


def _feed(parser: ProgressParser, text: str) -> list[float]:
    out = []
    for line in text.splitlines(keepends=True):
        fraction = parser.feed(line)
        if fraction is not None:
            out.append(fraction)
    return out


BLOCKS = """\
frame=0
out_time_us=N/A
out_time_ms=N/A
out_time=N/A
progress=continue
frame=120
out_time_us=2500000
out_time_ms=2500000
out_time=00:00:02.500000
progress=continue
frame=480
out_time_us=5000000
out_time_ms=5000000
out_time=00:00:05.000000
progress=continue
out_time_us=10000000
progress=end
"""


def test_each_block_counts_once_and_end_is_one():
    assert _feed(ProgressParser(10.0), BLOCKS) == [0.25, 0.5, 1.0]


def test_overshoot_is_clamped_and_never_goes_back():
    text = "out_time_us=12000000\nout_time_us=3000000\nprogress=end\n"
    assert _feed(ProgressParser(10.0), text) == [1.0]


def test_falls_back_to_out_time_ms_then_out_time():
    assert _feed(ProgressParser(10.0), "out_time_ms=4000000\n") == [0.4]
    assert _feed(ProgressParser(100.0), "out_time=00:01:15.000000\n") == [0.75]


def test_unknown_duration_reports_only_the_end():
    assert _feed(ProgressParser(0.0), "out_time_us=5000000\nprogress=end\n") == [1.0]


def test_noise_is_ignored():
    text = "\n=\nbitrate=N/A\nspeed=12.3x\nout_time_us=garbage\nprogress=continue\n"
    assert _feed(ProgressParser(10.0), text) == []
