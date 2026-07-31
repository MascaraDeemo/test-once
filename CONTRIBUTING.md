# Contributing

Contributions are welcome.

## Before opening a pull request

1. Keep the cache conservative: an uncertain result must be a miss, never a false hit.
2. Preserve the public CLI and hook behavior unless the change is intentionally documented.
3. Add or update integration coverage through the CLI or hook boundary.
4. Run:

   ```bash
   python3 -m unittest discover -s tests -v
   ```

5. When changing benchmark claims, reproduce them with:

   ```bash
   python3 benchmarks/run_benchmark.py
   ```

   Distinguish forced-fresh benchmark work from native warm-cache behavior.
   Do not present `nominal_test_seconds_avoided` as observed wall time; use an
   explicit warm calibration or a directly timed baseline.

6. Explain the cache-soundness impact in the pull request.

Keep dependencies minimal. Test Once currently uses only the Python standard library and Git.
Pillow is an optional development-only dependency used to regenerate
`assets/demo.gif`; it is not imported by the plugin.

Use GitHub Issues for bug reports, feature proposals, and support questions.
