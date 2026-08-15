export const meta = {
  name: 'issue-fleet',
  description: 'Work a set of GitHub issues in isolated worktrees, verify adversarially, then hand back an integration report',
  whenToUse: 'When several independent issues should be implemented in parallel. Pass issue numbers via args, e.g. args: [101, 102, 107].',
  phases: [
    { title: 'Triage', detail: 'read each issue and map the files it touches' },
    { title: 'Implement', detail: 'one agent per issue, each in its own git worktree' },
    { title: 'Verify', detail: 'adversarial review per change, looking for how it breaks' },
    { title: 'Report', detail: 'collect integration instructions for the orchestrator' },
  ],
}

const REPO = '/Users/toyoharukohyama/Documents/Creative_AI_Studio'
const PY = `${REPO}/venv/bin/python`

// Files where parallel work collides. Only the integrator touches these; agents
// that need wiring describe it instead of doing it. This is the rule that stops
// the auto-merge syntax errors we hit before.
const SHARED_FILES = [
  'bootstrap/factories.py',
  'apps/api/main.py',
  'core/models/loader.py',
  'core/models/__init__.py',
  'generators/*/__init__.py',
  'core/quality/__init__.py',
  'docs/next-tasks.md',
  '.env.example',
  'README.md',
]

const issues = (Array.isArray(args) ? args : [args]).filter(Boolean)
if (issues.length === 0) {
  throw new Error('Pass issue numbers via args, e.g. {args: [101, 102, 107]}')
}

const CONTRACT = `
Repository: ${REPO}

**Read \`docs/agent-harness.md\` first and follow it.** It is the contract for this
work: the verification gate, the shared-file ownership rules, and the prohibitions
(no commits, no \`pip install -r requirements.txt\`, no weight downloads).

Environment facts you must not re-derive:
- Python is \`${PY}\`; dependencies are already installed.
- Node deps are installed; use \`npm --prefix apps/web\`.
- ffmpeg comes from imageio-ffmpeg. There is NO system ffmpeg.
- No image model weights and no working TTS backend. You cannot validate anything
  that needs them — say so rather than claiming you did.

Shared files you must NOT edit (describe the wiring you need instead):
${SHARED_FILES.map((file) => `  - ${file}`).join('\n')}
`

phase('Triage')
const TRIAGE_SCHEMA = {
  type: 'object',
  properties: {
    issues: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          number: { type: 'integer' },
          title: { type: 'string' },
          summary: { type: 'string' },
          files_to_change: { type: 'array', items: { type: 'string' } },
          touches_shared_files: { type: 'array', items: { type: 'string' } },
          needs_ui_review: { type: 'boolean' },
          blocked_by_environment: { type: 'boolean' },
          notes: { type: 'string' },
        },
        required: ['number', 'title', 'summary', 'files_to_change', 'needs_ui_review', 'blocked_by_environment'],
      },
    },
    conflicts: {
      type: 'array',
      description: 'Pairs of issue numbers that would edit the same file',
      items: {
        type: 'object',
        properties: {
          a: { type: 'integer' },
          b: { type: 'integer' },
          shared_paths: { type: 'array', items: { type: 'string' } },
        },
        required: ['a', 'b', 'shared_paths'],
      },
    },
  },
  required: ['issues', 'conflicts'],
}

const triage = await agent(
  `${CONTRACT}

Triage these GitHub issues so they can be implemented in parallel: ${issues.join(', ')}.

For each issue:
1. Read it with \`gh issue view <number>\`.
2. Read the code it refers to and determine the concrete files that must change.
3. Decide whether it needs the AGENTS.md UI review (any change under apps/web).
4. Decide whether it can be verified in THIS environment, given no model weights
   and no TTS backend. Set blocked_by_environment accordingly.

Then report every pair of issues whose files_to_change overlap. Be precise: an
overlap means the same path, not the same directory. Do not modify anything.`,
  { label: 'triage', phase: 'Triage', schema: TRIAGE_SCHEMA },
)

const plan = triage?.issues ?? []
const conflicts = triage?.conflicts ?? []
if (conflicts.length > 0) {
  log(`conflicts detected: ${conflicts.map((c) => `#${c.a}~#${c.b}`).join(', ')} — these are reported, not auto-serialized`)
}
log(`triaged ${plan.length} issues; ${plan.filter((i) => i.blocked_by_environment).length} blocked by environment`)

const IMPLEMENT_SCHEMA = {
  type: 'object',
  properties: {
    number: { type: 'integer' },
    completed: { type: 'boolean' },
    files_changed: { type: 'array', items: { type: 'string' } },
    public_signatures: { type: 'array', items: { type: 'string' } },
    verification: {
      type: 'object',
      properties: {
        backend: { type: 'string' },
        frontend: { type: 'string' },
        build: { type: 'string' },
      },
    },
    wiring_needed: {
      type: 'array',
      description: 'Edits to shared files the integrator must make',
      items: { type: 'string' },
    },
    could_not_do: { type: 'array', items: { type: 'string' } },
    diff: { type: 'string', description: 'Full unified diff of the change' },
  },
  required: ['number', 'completed', 'files_changed', 'verification', 'could_not_do', 'diff'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    number: { type: 'integer' },
    verdict: { type: 'string', enum: ['ship', 'fix_first', 'reject'] },
    tests_rerun_output: { type: 'string' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          claim: { type: 'string' },
          failure_scenario: { type: 'string' },
          file: { type: 'string' },
        },
        required: ['severity', 'claim', 'failure_scenario'],
      },
    },
    acceptance_criteria_unmet: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
  },
  required: ['number', 'verdict', 'findings', 'acceptance_criteria_unmet', 'summary'],
}

// Each issue runs implement -> verify independently. pipeline() means a fast
// issue reaches Verify while a slow one is still being implemented, and a
// failure in one issue drops only that issue.
phase('Implement')
const results = await pipeline(
  plan,
  (item) =>
    agent(
      `${CONTRACT}

Implement GitHub issue #${item.number}: ${item.title}

Triage says it touches: ${(item.files_to_change || []).join(', ') || '(determine yourself)'}
${item.needs_ui_review ? '\nThis changes the web UI, so the AGENTS.md review requirements apply.' : ''}
${item.blocked_by_environment ? '\nTriage flagged this as not fully verifiable here. Implement it anyway and be explicit about what you could not verify.' : ''}

Steps:
1. \`gh issue view ${item.number}\` and read the acceptance criteria. They are the definition of done.
2. Implement the change. Follow the conventions in CLAUDE.md and docs/agent-harness.md.
3. Add or update tests. For a bug fix, write the test so that it **fails before your
   fix and passes after** — state how you confirmed that.
4. Run your own tests, then the full gate:
   \`${PY} -m pytest -q\` and \`npm --prefix apps/web test\` and \`npm --prefix apps/web run build\`
5. Do NOT commit. Produce \`git diff\` and return it in the diff field.

You are in your own git worktree, so you cannot collide with the other agents.
If you need a shared file edited, put it in wiring_needed instead of editing it.`,
      { label: `impl:#${item.number}`, phase: 'Implement', schema: IMPLEMENT_SCHEMA, isolation: 'worktree' },
    ),
  (built, item) => {
    if (!built) return null
    return agent(
      `${CONTRACT}

Adversarially review the implementation of issue #${item.number}: ${item.title}

The implementing agent reported:
- completed: ${built.completed}
- files: ${(built.files_changed || []).join(', ')}
- could not do: ${(built.could_not_do || []).join('; ') || '(nothing reported)'}
- backend: ${built.verification?.backend || '(not reported)'}
- frontend: ${built.verification?.frontend || '(not reported)'}

Its diff:
\`\`\`diff
${(built.diff || '(no diff returned)').slice(0, 20000)}
\`\`\`

Your job is to find what is WRONG, not to confirm it works.

1. \`gh issue view ${item.number}\` and check every acceptance criterion against the
   diff. List any that are claimed but not actually met.
2. Apply the diff in your own worktree and re-run \`${PY} -m pytest -q\`. Record the tail.
3. For a bug fix: verify the new test genuinely fails without the fix. Revert just
   the source change, run the test, and confirm it goes red. A test that passes
   either way is worth nothing — report it as a high finding.
4. Hunt specifically for: assertions that cannot fail, silent \`except: pass\`,
   behavior changes outside the issue's scope, edits to shared files, tests
   rewritten to accommodate a bug rather than fix it, and claims of verification
   that the environment cannot support (no weights, no TTS, no system ffmpeg).

Default to skepticism. If you cannot convince yourself something holds, report it.
Return verdict "ship" only when every acceptance criterion is met and the tests are real.`,
      { label: `verify:#${item.number}`, phase: 'Verify', schema: VERDICT_SCHEMA, effort: 'high' },
    )
  },
)

phase('Report')
const verdicts = results.filter(Boolean)
const shippable = verdicts.filter((v) => v.verdict === 'ship').map((v) => v.number)
const needsWork = verdicts.filter((v) => v.verdict !== 'ship')

log(`ship: ${shippable.length ? shippable.map((n) => `#${n}`).join(', ') : 'none'}`)
if (needsWork.length) {
  log(`needs work: ${needsWork.map((v) => `#${v.number}(${v.verdict})`).join(', ')}`)
}

// Integration is deliberately NOT automated: committing, wiring shared files, and
// opening PRs stay with the orchestrator, who can see the whole picture.
return {
  triage,
  verdicts,
  shippable,
  needs_work: needsWork,
  shared_file_wiring: verdicts.map((v) => ({ number: v.number })),
  next_step:
    'Orchestrator: apply the shippable diffs, make any wiring_needed edits to shared files, run the full gate, then commit and open a PR.',
}
