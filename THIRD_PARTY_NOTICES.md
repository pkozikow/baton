# Third-party notices

Baton's Python runtime uses only the standard library. The `baton web` UI ships two
browser libraries, vendored unmodified under `baton/web/static/vendor/`. Their versions,
source URLs and SHA-256 checksums are recorded in
[`VENDORED.json`](baton/web/static/vendor/VENDORED.json).

| Library | Version | License | Full license texts |
| --- | --- | --- | --- |
| [markdown-it](https://github.com/markdown-it/markdown-it) | 15.0.3 | MIT | [`markdown-it.LICENSE.txt`](baton/web/static/vendor/markdown-it.LICENSE.txt) |
| [Mermaid](https://github.com/mermaid-js/mermaid) | 11.17.2 | MIT | [`mermaid.LICENSE.txt`](baton/web/static/vendor/mermaid.LICENSE.txt) |

Each distributed file bundles further packages. The license files above reproduce the
license of every bundled package, all permissive: MIT, ISC, BSD-2-Clause, BSD-3-Clause
and Apache-2.0. DOMPurify, bundled in Mermaid, is dual licensed (MPL-2.0 OR Apache-2.0)
and is used under Apache-2.0.

Mermaid is pinned to the 11.x line because every 12.x release bundles elkjs, which is
licensed EPL-2.0 OR GPL-3.0-or-later. Before changing either version, repeat the
bundled-license check against the new release's source map and update the files above.
