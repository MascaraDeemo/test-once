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

5. Explain the cache-soundness impact in the pull request.

Keep dependencies minimal. Test Once currently uses only the Python standard library and Git.

Use GitHub Issues for bug reports, feature proposals, and support questions.
