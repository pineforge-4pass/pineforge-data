# Pre-manifest pineforge-data 0.3.0

Unlike the synthetic parent fixtures, these are the actual published 0.3.0 wheel
and sdist. The base64 files preserve the bytes as text; tests decode them without
extracting or executing their contents. CORE-TOP downloaded the PyPI files and
read back the GitHub release assets on 2026-10-19. These copies came from that
local `data-030/readback` directory, without another network request.

| Distribution | Bytes | SHA-256 |
| --- | ---: | --- |
| `pineforge_data-0.3.0-py3-none-any.whl` | 56354 | `609b3e9976ae55fc290c725731cf1dd0266c9b3eb6f3353122c8bcdc05e497bc` |
| `pineforge_data-0.3.0.tar.gz` | 101723 | `f29a2bd50f70ff46ebdf31f987260dec3123600ab5bf493741bbbd4e21aaacdc` |

`identity.json` transcribes the shared bootstrap form independently of the
implementation constants. `distribution-identity.json` is the literal canonical
JSON, including the final newline, whose SHA-256 is its `deployment_id_sha256`:
`12ffb9b3d3cd270604b06f558ae73a9eb0ca1e2f82a84d45728232091d3e3307`.

The bootstrap verification tests replace transport functions only. Hashing,
inventory checks, tag checks, receipt writing and validator checks all run their
real implementations. No published package code is imported or executed.
