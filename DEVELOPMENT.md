# Development Practices

## Scope and safety

- This repository is research and paper-trading only. Do not add live-order execution paths without an approved, separate production proposal.
- Treat source data, market contracts, and settlement rules as untrusted inputs. Reject ambiguous event or outcome mappings.
- Never commit credentials, account identifiers, captured private data, `.env` files, or private-key files. Use `.env.example` only for placeholders and keep key files outside the repository.
- Keep external providers behind connectors so ESPN or Kalshi can be replaced without changing model code.

## Workflow

1. Start from an up-to-date default branch and create a focused feature branch.
2. Create or update a Linear ticket before implementation. Write its acceptance criteria in Given/When/Then form, including validation and failure behavior.
3. State the user-visible outcome and acceptance tests in the pull request.
4. Keep changes small and preserve point-in-time data semantics; no feature may use information available after its prediction timestamp.
5. Add or update focused tests with every behavioral change.
6. Run the full test suite locally before requesting review.
7. Request a Claude Code review for each pull request, address actionable feedback, and record the review result in the pull request.
8. Use a pull request for merge; do not commit secrets or force-push shared branches.

## Quality gates

```powershell
python -m pytest
python -m sgr.cli --help
git diff --check
git status --short
```

Before requesting review, inspect `git status --short` and confirm that it has no
unexpected `.env`, PEM, key, certificate, or credential-export files. The ignore
rules are defense in depth; they do not make it acceptable to keep secrets in the
working tree or repository.

For new model or data features, also provide a reproducible fixture or snapshot and document:

- source and retrieval timestamp;
- the prediction/decision timestamp;
- data normalization and matching rules;
- model/version parameters; and
- rejection and error behavior.

## Review checklist

- [ ] Scope matches the issue and avoids unrelated refactors.
- [ ] Tests cover success, invalid input, and point-in-time/leakage behavior.
- [ ] API errors, schema drift, stale data, and ambiguous market mappings fail safely.
- [ ] Secrets are absent from the diff, fixtures, logs, and documentation.
- [ ] Secrets use `SecretStr` where retained in memory; error text, reprs, URLs, and query strings are redacted.
- [ ] Research claims are measurable and do not imply guaranteed outcomes.
- [ ] Claude Code review has been requested and actionable feedback is resolved.
