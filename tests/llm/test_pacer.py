"""Pacer unit tests on a fake clock: header-driven and local-window pacing."""
import pytest

from careerops.llm.pacing import Pacer, estimate_tokens, parse_duration


@pytest.mark.parametrize("text, secs", [("1m26.4s", 86.4), ("2.085s", 2.085), ("250ms", 0.25), ("7.66s", 7.66),
                                        ("2h3m", 7380.0), ("1h", 3600.0), ("12", 12.0), ("1m", 60.0)])
def test_parse_duration(text, secs):
    assert parse_duration(text) == pytest.approx(secs)


@pytest.mark.parametrize("bad", [None, "", "soon", "5x", "1m2"])
def test_parse_duration_rejects_what_it_cannot_read(bad):
    assert parse_duration(bad) is None


def test_estimate_is_chars_over_3_5_rounded_up():
    assert estimate_tokens("a" * 350) == 101 and estimate_tokens("a" * 350, "b" * 350) == 201


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def test_headers_say_wait_until_the_token_window_resets_then_free():
    c = Clock(); p = Pacer(8000, 30, c)
    p.received({"x-ratelimit-remaining-tokens": "500", "x-ratelimit-reset-tokens": "7.5s"})
    assert p.wait_needed(400) == 0                        # fits in what is left
    assert p.wait_needed(1200) == pytest.approx(7.5)
    c.now += 3
    assert p.wait_needed(1200) == pytest.approx(4.5)
    c.now += 5
    assert p.wait_needed(1200) == 0                       # window reset: full budget again


def test_headers_override_the_local_window():
    c = Clock(); p = Pacer(1000, None, c)
    p.sent(900)                                           # local estimate alone would say "wait"
    p.received({"x-ratelimit-remaining-tokens": "7000", "x-ratelimit-reset-tokens": "1s"})
    assert p.wait_needed(900) == 0


def test_local_window_frees_exactly_the_tokens_needed():
    c = Clock(); p = Pacer(8000, None, c)
    for dt, tok in ((0, 2500), (10, 2500), (20, 2500)):
        c.now = 100 + dt; p.sent(tok)
    c.now = 130
    assert p.wait_needed(500) == 0                        # 7500 + 500 = 8000, just fits
    assert p.wait_needed(2500) == pytest.approx(30)       # needs the first 2500 out: at t=160
    assert p.wait_needed(5000) == pytest.approx(40)       # needs two out: the second leaves at t=170
    c.now = 161
    assert p.wait_needed(2500) == 0


def test_a_call_bigger_than_the_whole_budget_waits_for_an_empty_window_not_forever():
    c = Clock(); p = Pacer(1000, None, c)
    p.sent(600)
    assert p.wait_needed(5000) == pytest.approx(60)       # capped to the budget: the one 600 call must leave


def test_rpm_window_and_daily_request_headers():
    c = Clock(); p = Pacer(None, 2, c)
    p.sent(1); c.now += 5; p.sent(1)
    assert p.wait_needed(1) == pytest.approx(55)
    q = Pacer(None, None, c)
    q.received({"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "10m"})
    assert q.wait_needed(1) == pytest.approx(600)


def test_unreadable_or_absent_headers_change_nothing():
    c = Clock(); p = Pacer(8000, 30, c)
    p.received({"x-ratelimit-remaining-tokens": "lots"}); p.received({})
    assert p.remaining_tokens is None and p.wait_needed(100) == 0
