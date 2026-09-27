# Repository guidance

- Keep all public examples, tests, screenshots, and CI inputs synthetic. Never read or copy a user's world data into a committed fixture.
- Preserve the distinction between known air and unknown/unindexed data. A missing chunk or uncovered range must not be treated as empty space.
- Keep indexing and rendering offline and read-only. Do not add server connections, world mutation, or automatic calls to mc-builder.
- Blueprint export must remain a local artifact step. State clearly that it is a design, not site approval or construction authorization.
- Preserve dimension IDs, global integer coordinates, and exact block state properties across Scene and blueprint conversions. Report rendering simplifications in the manifest.
- Run meaningful unit tests on synthetic inputs for behavioral changes. Do not add integration tests that require a real server or world save.
- The project owner requested implementation agents prefer GPT-6 Luna with maximum reasoning when available. This is a project-local development preference, not a requirement for downstream users or agents.
