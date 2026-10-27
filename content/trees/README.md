# SYNTHETIC ONLY — diagnostic format corpus

These files demonstrate the strict MVP-027 format. They contain no manufacturer
instructions, technical diagnosis, expert approval or publication permission.

`valid/` means structurally valid YAML/DAG, not importable into an arbitrary DB.
The section/item UUIDs are placeholders. An operator must supply existing
synthetic equipment, section and item IDs in a separate test file; changing bytes
changes its checksum. Do not seed or import these files into production.

- `linear-v1.yaml`, `linear-v2.yaml`: two editions of one stable key/model.
- `branching.yaml`: result and safe specialist handoff branches.
- `handoff.yaml`: terminal-only specialist handoff without technical claims.
- `invalid/`: duplicate keys, unsafe tag, anchor/alias, cycle and hidden action.

See [MVP-027](../../docs/MVP-027.md) for limits, CLI and draft semantics.
