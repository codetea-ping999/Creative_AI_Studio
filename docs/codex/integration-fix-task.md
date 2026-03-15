# Task
Fix inconsistencies found during bootstrap integration review.

## Goal
Resolve the concrete issues found in the integration review without expanding project scope.

## Inputs
- Integration review findings
- docs/checklists/integration-checklist.md
- docs/architecture.md
- docs/domain-model.md
- docs/api-contract.md

## Requirements
- Fix naming inconsistencies
- Fix schema mismatches
- Fix import/module path issues
- Fix API/UI payload mismatches
- Keep changes minimal and scoped

## Constraints
- Do not add unrelated features
- Do not redesign the core architecture
- Do not change file layout unless required by consistency issues

## Deliverables
- Updated files
- A short fix summary
- Remaining known issues if any

## Acceptance Criteria
- Review findings are resolved or explicitly documented
- The project remains aligned with the architecture docs
- The codebase is ready for end-to-end validation
