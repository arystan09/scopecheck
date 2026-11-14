# License and dependency review

Code/documentation: [MIT](../LICENSE), using the [OSI license text](https://opensource.org/license/mit).
Own synthetic fixtures: [CC0 dedication and provenance](../datasets/LICENSE.md).
AI assistance is disclosed in development.md. These licenses do not grant rights
to third-party dependencies or imply model-service access.

[Installed dependency inventory](../artifacts/dependency-licenses.json) records
59 distributions from the frozen local environment, including development tools.
It is metadata evidence, not a full image SBOM. Review found mostly MIT/BSD/Apache
terms, plus PSF-2.0, MPL-2.0 (certifi/pathspec) and LGPL-3.0-only
(psycopg/psycopg-binary). Do not describe the entire image as MIT-only. Preserve
upstream notices/licenses; review MPL source and LGPL redistribution obligations
before distributing binaries. No third-party dependency source was modified here.

Psycopg binary bundles native client libraries; its Python metadata alone does not
inventory those or OS packages. See [upstream installation documentation](https://www.psycopg.org/psycopg3/docs/basic/install.html).
Container/native-library license verification remains pending the actual built
image. Runtime metadata was reviewed; no legal compliance certification is claimed.
The build backend (hatchling) and external Docker/uv/PG/age/Caddy/AWS tools are
outside the installed-environment inventory and retain their own licenses.
