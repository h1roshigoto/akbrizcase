# SYNTHETIC ONLY — schema/evidence fixture

`manifest.template.json` and its `.bin`/JSON artifacts are invented **unit-test
expected states**. They contain no actual human approvals, manufacturer content,
real prices, rights, signature or target DB export. The one-pixel image and its
VERIFIED observation are a test expectation, not a claim that these fixture bytes
were processed by the runtime image pipeline. Test-only approval records contain
no invented human names or dates.

The corpus here tests parsing, checksum binding, denominator and negative outcome
validation. It is **not an FTS acceptance corpus**, and its hypothetical test
measurements cannot establish search quality. The actual workflow rehearsal uses
the unchanged, locked MVP-047 20+20 corpus and real PostgreSQL 16.

The copied branching YAML is the existing `content/trees/valid/branching.yaml`;
its IDs are placeholders. Inventory paths are schema expectations. Existing
MVP-045 integration tests rebind those IDs in guarded fresh databases, publish
through the normal services and exercise every actual path and dependency loss.
A workflow receipt lists stages and a source tree; offline JSON does not prove
execution, so imported receipts remain INCONCLUSIVE. Only the live runner can
report PASS after its successful stages and restore. It does not bind this
template to an ephemeral database. The optional git_blob checks the exact public
branching YAML commit/path/blob; private YAML without it has unverified provenance.

Do not import this fixture into production, relabel it as real, or copy its
attestations into real approvals. Read the [operator contract](../../../docs/MVP-049-pilot-content.md).
The rehearsal copies it into a private 0700 namespace with 0600 files before
running preflight. Real packets and reports always remain outside Git and CI.
