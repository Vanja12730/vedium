# Contributing to Vedium

Thank you for helping improve Vedium. Bug reports, testing, documentation, and
code contributions are welcome.

## Development workflow

1. Create a focused branch and keep each change reasonably small.
2. Run `python tests/run_tests_suite.py` before opening a pull request.
3. Explain the user-facing effect, testing performed, and any privacy or
   security implications in the pull request.
4. Add a contributor sign-off to every commit with `git commit -s`; under this
   policy, that sign-off records agreement to the contributor licence terms
   below.

## Contributor licence terms

Vedium is publicly distributed under GPL-3.0-only. The project may also offer
separate commercial licences so organizations can integrate Vedium under terms
that are not compatible with the GPL. To preserve that option, every
contributor must agree to the terms below.

By intentionally submitting a contribution to Vedium, you certify that:

1. You created the contribution, or you have sufficient legal authority to
   submit it under these terms.
2. You grant the Vedium copyright holder and its successors a perpetual,
   worldwide, non-exclusive, royalty-free, irrevocable licence to reproduce,
   modify, prepare derivative works of, publicly display, publicly perform,
   sublicense, and distribute your contribution under GPL-3.0-only and under
   other licence terms, including commercial or proprietary terms.
3. You grant a perpetual, worldwide, non-exclusive, royalty-free, irrevocable
   patent licence for patent claims you control that are necessarily infringed
   by your contribution alone or by its combination with Vedium.
4. You retain ownership of your contribution and may use it elsewhere. Public
   Vedium releases containing it remain available under GPL-3.0-only.
5. You understand that the contribution and its project history will be public
   and may be redistributed indefinitely.

The `Signed-off-by` line records your agreement to these contributor licence
terms. Maintainers cannot accept a contribution without that sign-off.

This document is intended to keep project ownership clear and preserve both
open-source and commercial licensing options. A project owner planning a
substantial commercial transaction should have these terms reviewed by
qualified counsel in the relevant jurisdiction.

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
