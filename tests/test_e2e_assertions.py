"""The per-host checks of the end-to-end batch run, on synthetic farm records."""

from tests.e2e.run_batch import Request, file_load_violations, max_overlap, spacing_violations


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


def test_each_file_loaded_once_per_worker_without_overlap_passes() -> None:
    requests = [
        Request(ROBOTS, 0.000, 0.300, 404),
        Request(TDMREP, 0.300, 0.600, 404),
        Request(TDMREP, 0.650, 0.950, 404),  # the other worker's load, after the first ended
        Request(AI_TXT, 0.600, 0.900, 404),
        Request("/p/1.html", 0.700, 0.900),  # pages are not checked here
    ]

    assert file_load_violations(requests, max_loads=2) == []


def test_a_file_loaded_more_often_than_there_are_workers_is_reported() -> None:
    requests = [
        Request(ROBOTS, 0.0, 0.1, 404),
        Request(ROBOTS, 0.2, 0.3, 404),
        Request(ROBOTS, 0.4, 0.5, 404),
    ]

    assert file_load_violations(requests, max_loads=2) == [f"{ROBOTS} loaded 3 times"]


def test_two_loads_of_one_file_at_once_are_reported() -> None:
    requests = [
        Request(TDMREP, 0.300, 0.600, 404),
        Request(TDMREP, 0.500, 0.800, 404),
        Request(AI_TXT, 0.600, 0.900, 404),  # another file may overlap: another turn's
    ]

    assert file_load_violations(requests, max_loads=2) == [f"two loads of {TDMREP} overlap"]
