# Benchmark

This controlled benchmark measures the wall-clock effect of three independent
full-suite requests against native Go and TypeScript test runners.

It deliberately uses a one-second deterministic test body. This makes the
fixed cost of Python startup, Git source fingerprinting, toolchain detection,
locking, and cache lookup visible instead of hiding that cost behind a
long-running suite.

## Result

Measured on 2026-07-31 with three trials per language; the table reports the
median.

- Host: macOS on arm64
- Python: 3.14.6
- Go: 1.24.9
- Node.js: 26.3.1
- npm: 11.16.0
- Workload: three independent full-suite requests with a one-second test body

| Suite | Direct total | Test Once total | Underlying runs | Cache hits | Wall time saved | Reduction |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Go (`go test -count=1 ./...`) | 4.299s | 1.919s | 1 | 2 | 2.380s | 55.4% |
| TypeScript (`npm test --silent`) | 4.020s | 2.089s | 1 | 2 | 1.930s | 48.0% |

The direct baseline starts the native test runner three times. The Test Once
total includes every cache cost and starts the native runner once.

The avoided test work for `N` identical requests and a suite duration of `T`
is `(N - 1) × T`. For three sessions requesting a 20-minute suite, that is 40
minutes of test processes avoided. Actual wall time saved is lower by lookup
or single-flight waiting time and is reported by `test_once.py stats`.

## Reproduce

Requirements:

- Git
- Python 3.10 or newer
- Go
- Node.js with built-in TypeScript type stripping
- npm

From the repository root:

```bash
python3 benchmarks/run_benchmark.py
```

Change the workload without changing the benchmark implementation:

```bash
python3 benchmarks/run_benchmark.py \
  --iterations 3 \
  --trials 3 \
  --sleep-ms 1000 \
  --format json
```

Each trial uses a new temporary Git repository and Test Once cache. Go result
caching is disabled with `-count=1`. The benchmark makes no network requests
and removes its temporary repositories on completion.

To regenerate the ten-second README demo:

```bash
python3 -m pip install Pillow
python3 benchmarks/render_demo_gif.py
```

Pillow is a development-only dependency and is not required to install or run
Test Once.
