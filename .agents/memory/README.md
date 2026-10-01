# Canonical Agent Memory

Memory records reusable, evidence-backed engineering knowledge. It is not a chronological log and it is not a substitute for the primary source it summarizes.

Every v0.1 memory uses YAML frontmatter with at least:

```yaml
schema_version: 1
id: FP-XXX
type: failure-pattern
status: validated
scope: []
created_at: YYYY-MM-DD
last_verified_at: YYYY-MM-DD
confidence: high | medium | low
evidence: []
validated_by: []
supersedes: []
superseded_by: []
review_after: YYYY-MM-DD | null
```

Mutable implementation facts should have a review date. If `review_after` has passed, treat the memory as `review_due`; it may provide context but must not be the sole authority for architecture, merge gates, or safety claims until revalidated.

Failure-pattern memories live under `failure-patterns/`. v0.1 deliberately starts with the five lessons promoted from PR #415.