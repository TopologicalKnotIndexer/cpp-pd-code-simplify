# C++ Zip-Random Time Analysis

This page records a C++-only timing experiment on PD codes sampled from
`tests/pd_code.zip`. The zip file itself is a local test fixture and is
not committed to the repository.

## Method

- Sample size: `30` PD-code files.
- Random seed: `20260709`.
- C++ executable: `build/bin/pd_simplify.exe`.
- Runtime options: `--max-paths -1 --reduction-round -1 --max-thread 16`.
- Per-case timeout: `120` seconds. Timed-out or errored cases are counted as failures and excluded from the scatter plot.
- Each point is one C++ CLI invocation, so the time includes process startup, parsing, preprocessing, simplification, and final JSON formatting.
- Generated at local time `2026-07-09 11:36:44` on `Windows-11-10.0.26100-SP0` with Python `3.13.1`.

## Results

![C++ zip-random time scatter](assets/cpp_zip_random_30_time_scatter.png)

| Metric | Value |
| --- | ---: |
| Sampled cases | 30 |
| Completed cases | 20 |
| Error or timeout cases | 10 |
| Failure rate | 33.3% |
| Crossing count range | 165 to 368 |
| Median crossing count | 232.5 |
| Total completed C++ time | 13.04 min |
| Mean completed time | 39.112 s |
| Median completed time | 27.486 s |
| Max completed time | 1.98 min |

Raw artifacts:

- [CSV rows](assets/cpp_zip_random_30_time_scatter.csv)
- [JSON results](assets/cpp_zip_random_30_time_scatter.json)
