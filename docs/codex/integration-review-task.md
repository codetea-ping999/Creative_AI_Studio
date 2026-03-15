# Task
Run integration review for bootstrap tasks 01-09.

## Goal
Review the outputs of the initial bootstrap tasks and identify inconsistencies before end-to-end validation.

## Scope
- generation schema
- job schema
- model manifest
- sqlite bootstrap
- job repository
- base generator
- image generator stub
- fastapi bootstrap
- prompt form UI

## Inputs
- docs/checklists/integration-checklist.md
- docs/architecture.md
- docs/domain-model.md
- docs/api-contract.md

## Requirements
- Review naming consistency
- Review schema consistency
- Review import/module consistency
- Review storage consistency
- Review generator consistency
- Review API/UI contract consistency
- Produce a structured findings report

## Constraints
- Do not implement large feature changes
- Do not redesign the architecture
- Focus on review and minimal necessary corrections only if critical

## Deliverables
1. Review summary
2. Findings list
3. Follow-up fix tasks
4. Pass/partial/fail judgment

## Acceptance Criteria
- All checklist sections are evaluated
- Findings are concrete and actionable
- A follow-up fix list is produced
