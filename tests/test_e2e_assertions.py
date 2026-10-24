"""The per-host checks of the end-to-end batch run, on synthetic farm records."""

from tests.e2e.run_batch import Request, max_overlap, spacing_violations, turn_violations


def _requests(*spans: tuple[float, float]) -> list[Request]:
    return [
        Request(path=f"/p/{n}.html", start=start, end=end) for n, (start, end) in enumerate(spans)
    ]


def test_overlap_counts_requests_in_flight_at_once() -> None:
    # Two at a time at most; a request that starts exactly when another ends does not overlap it.
    requests = _requests((0.0, 1.0), (0.5, 1.5), (1.0, 2.0), (1.5, 2.5))

    assert max_overlap(requests) == 2


def test_overlap_finds_a_third_request_in_flight() -> None:
    requests = _requests((0.0, 1.0), (0.2, 1.2), (0.9, 1.1), (3.0, 4.0))

    assert max_overlap(requests) == 3


def test_overlap_of_no_requests_is_zero() -> None:
    assert max_overlap([]) == 0


def test_spacing_accepts_starts_at_least_the_gap_apart() -> None:
    # Order of the records does not matter: the starts are sorted first.
    requests = _requests((0.40, 0.5), (0.0, 0.1), (0.20, 0.9))

    assert spacing_violations(requests, min_gap=0.18) == []


def test_spacing_reports_each_pair_of_starts_too_close() -> None:
    requests = _requests((0.0, 0.1), (0.25, 0.3), (0.30, 0.4), (0.60, 0.7))

    violations = spacing_violations(requests, min_gap=0.18)

    assert [(first.start, second.start) for first, second in violations] == [(0.25, 0.30)]


ROBOTS = "/robots.txt"
TDMREP = "/.well-known/tdmrep.json"
AI_TXT = "/ai.txt"


def test_files_fetched_one_by_one_before_their_page_pass() -> None:
    requests = [
        Request(ROBOTS, 0.000, 0.001, 404),
        Request(TDMREP, 0.001, 0.002, 404),
        Request(AI_TXT, 0.002, 0.003, 404),
        Request("/p/1.html", 0.004, 0.150),
        Request("/p/2.html", 0.204, 0.300),  # the next turn
    ]

    assert turn_violations(requests) == []


def test_files_fetched_at_once_are_reported() -> None:
    requests = [
        Request(ROBOTS, 0.000, 0.002, 404),
        Request(TDMREP, 0.001, 0.003, 404),
        Request(AI_TXT, 0.003, 0.004, 404),
        Request("/p/1.html", 0.005, 0.150),
    ]

    assert turn_violations(requests) == [f"{ROBOTS} and {TDMREP} overlap"]


def test_a_page_sent_before_its_last_file_ended_is_reported() -> None:
    requests = [
        Request(ROBOTS, 0.000, 0.001, 404),
        Request(TDMREP, 0.001, 0.002, 404),
        Request(AI_TXT, 0.002, 0.010, 404),
        Request("/p/1.html", 0.005, 0.150),
    ]

    assert turn_violations(requests) == [f"/p/1.html started before {AI_TXT} ended"]


def test_a_page_of_another_turn_may_overlap_the_files() -> None:
    # /p/0.html started in an earlier turn, before the files: the limit of 2 allows that.
    requests = [
        Request("/p/0.html", 0.000, 0.150),
        Request(ROBOTS, 0.010, 0.011, 404),
        Request(TDMREP, 0.011, 0.012, 404),
        Request(AI_TXT, 0.012, 0.013, 404),
        Request("/p/1.html", 0.014, 0.150),
    ]

    assert turn_violations(requests) == []
