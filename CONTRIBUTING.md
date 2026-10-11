# Contributing to Vedium

Thank you for helping improve Vedium. Bug reports, testing, documentation, and
code contributions are welcome.

## Development workflow

1. Create a focused branch and keep each change reasonably small.
2. Run the offline test suites before opening a pull request. Every
   `tests\*_tests.py` file is a standalone script that exits non-zero on
   failure and needs nothing but the packages in `requirements.txt` -- no API
   key, no model, no network:

   ```powershell
   Get-ChildItem tests\*_tests.py | ForEach-Object {
       py -3.12 $_.FullName
       if ($LASTEXITCODE -ne 0) { throw "$($_.Name) FAILED" }
   }
   ```

   Run each suite in its own process, as above: they share module-level state
   and are not isolated from one another.

   The other scripts in `tests\` are opt-in and not part of that run --
   `settings_ui_test.py` and `reflection_ui_test.py` open real windows,
   `run_tests_suite.py` needs a live cloud API key, and
   `local_model_benchmark.py` needs a local GGUF model and llama-server.
3. Explain the user-facing effect, testing performed, and any privacy or
   security implications in the pull request.
4. Sign off every commit with `git commit -s`. The sign-off certifies the
   [Developer Certificate of Origin](https://developercertificate.org/): you
   wrote the change, or otherwise have the right to submit it.

## Licence of contributions

Vedium is licensed under GPL-3.0-only. By submitting a contribution, you agree
that it is licensed under the same terms.

## Third-party material

Do not submit code, model weights, images, fonts, or other material copied from
another project unless its licence is compatible with Vedium and you clearly
identify its source and licence in the pull request. Never commit API keys,
credentials, private user data, model files, generated journals, or local
configuration files.

## Privacy and security

Vedium processes text captured from applications, so changes involving the
keyboard listener, clipboard, local journals, cloud requests, or credentials
receive additional scrutiny. Keep data collection minimal, document every
external transmission, and preserve local-only behavior unless the user
explicitly enables a remote backend.
