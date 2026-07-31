# Real-world case study

This case study checks whether Test Once still helps when large repositories
already have native build or test caches. It is deliberately separate from the
controlled benchmark: these numbers describe two exact public snapshots on one
machine, not a universal performance claim.

## Selection

Repositories had to exceed 80,000 GitHub stars at measurement time, provide an
official full-suite command, and run locally without browsers, containers, or
external services.

- [Syncthing at `bcef5c5`](https://github.com/syncthing/syncthing/commit/bcef5c5bc68dddfb68a3d341f41fad44c11fb52e): 87,112 stars and 140 Go test files. Its [CI workflow](https://github.com/syncthing/syncthing/blob/bcef5c5bc68dddfb68a3d341f41fad44c11fb52e/.github/workflows/build-syncthing.yaml) runs `go run build.go test`.
- [Microsoft/TypeScript at `b465fdb`](https://github.com/microsoft/TypeScript/commit/b465fdbfe175304d9b977da137b2c178ae1091d3): 110,015 stars, 81,368 tracked files, and 106,371 passing tests. Its [contributor guide](https://github.com/microsoft/TypeScript/blob/b465fdbfe175304d9b977da137b2c178ae1091d3/CONTRIBUTING.md#running-the-tests) documents the full parallel suite used by `npm test`.

Star counts were captured on 2026-07-31 and will change.

## Environment

- macOS 26.5.2 on arm64
- 18 logical CPU cores and 48 GiB RAM
- Syncthing: Go 1.25.0
- TypeScript: Node.js 22.22.0 and npm 8.19.4, matching its repository metadata
- Dependencies and language toolchains were downloaded before timing

## Method

For each clean commit:

1. Run the official suite through Test Once and record both the underlying
   command duration and full wrapper wall time.
2. Start two independent Test Once processes against the same key and record
   their hit wall times.
3. Run the official command directly once in the now-warm repository to expose
   native caches.
4. Model three direct requests as `underlying first run + 2 × observed native
   warm rerun`. Model Test Once as `first wrapper + hit 1 + hit 2`.

This is a conservative correction to the misleading `first run × 3` baseline.
It still uses a single observed native warm rerun, so treat the result as a case
study rather than a statistically sampled benchmark.

## Result

| Repository | Underlying first run | Test Once first wall | Native warm rerun | Test Once hits | Direct three requests | Test Once three requests | Net saved |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Syncthing | 60.71s | 60.99s | 2.63s | 0.21s, 0.22s | 65.97s | 61.42s | 4.55s (6.9%) |
| Microsoft/TypeScript | 140.69s | 142.75s | 72.53s | 1.25s, 1.15s | 285.75s | 145.15s | 140.60s (49.2%) |

In both repositories, Test Once recorded three requests, one underlying run,
two hits, zero failures, and zero unstable snapshots. The passing result was
bound to a clean exact commit.

The data was collected with v0.2.1 and directly motivated the v0.2.2 statistics
model. The cache key, locking, and reuse path did not change in v0.2.2.

## Interpretation

Syncthing's Go test cache reduced a normal warm rerun from about one minute to
2.63 seconds. Test Once still avoided starting the suite and resolved in about
0.22 seconds, but the absolute three-request saving was only 4.55 seconds.

TypeScript reused build and ESLint work while still running all 106,371 tests.
Its warm direct rerun took 72.53 seconds; Test Once resolved in about 1.2
seconds, saving 140.60 seconds across three requests.

The general source, command, environment, and toolchain fingerprint cost was
small in both cases. The dominant variable was whether the native runner
already cached the entire passing test result.

## Limits

- One host and one warm direct observation per repository
- Local macOS results, not CI runners
- No flaky-test or external-system workload
- No claim that star count predicts test-suite behavior
- Warm calibration intentionally performs one extra full-suite run and is for
  measurement, not normal reuse
