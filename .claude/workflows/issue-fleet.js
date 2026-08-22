export const meta = {
  name: 'issue-fleet',
  description: 'Work a set of GitHub issues in isolated worktrees, verify adversarially, then hand back an integration report',
  whenToUse: 'When several independent issues should be implemented in parallel. Pass issue numbers via args, e.g. args: [103, 104, 105].',
  phases: [
    { title: 'Triage', detail: 'read each issue, map its files, report overlaps' },
    { title: 'Implement', detail: 'one agent per non-overlapping cluster, each in its own git worktree' },
    { title: 'Verify', detail: 'adversarial review per cluster, looking for how it breaks' },
    { title: 'Report', detail: 'collect integration instructions for the orchestrator' },
  ],
}

const REPO = '/Users/toyoharukohyama/Documents/Creative_AI_Studio'
const PY = `${REPO}/venv/bin/python`

// A patch returned through a structured-output string field does not survive the
// trip: measured on run wf_08239cc6-8cb, one diff came back truncated mid-hunk and
// both came back HTML-escaped (`&lt;` for `<`), so neither applied. Worse, the
// agent worktrees are cleaned up when the workflow ends, so the work was only
// recoverable because the corruption happened to be repairable. Agents now write
// the patch to a durable path OUTSIDE their worktree and return that path; the
// diff field is kept only as a human-readable excerpt. Each handoff gets a
// mktemp-created subdirectory so a retry or a concurrent fleet cannot overwrite
// an artifact from another run.
const PATCH_ROOT = '/tmp/creative-ai-studio-harness-patches'
// The run ID is part of the expected path, so a well-formed artifact from an
// earlier fleet cannot be mistaken for this run's handoff.
const artifactNonce = Math.random().toString(36).slice(2, 10).padEnd(8, '0')
const ARTIFACT_RUN = `run-${Date.now().toString(36)}-${artifactNonce}`

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

const normalizePath = (value) => String(value ?? '').trim().replace(/^(\.\/)+/, '')
const normalizedPaths = (values) =>
  [...new Set((Array.isArray(values) ? values : []).map(normalizePath).filter(Boolean))].sort()
const sortedNumbers = (values) =>
  [...new Set((Array.isArray(values) ? values : []).filter(Number.isInteger))].sort((a, b) => a - b)
const sameValues = (left, right) =>
  left.length === right.length && left.every((value, index) => value === right[index])
const hasText = (value) => typeof value === 'string' && value.trim().length > 0
const matchesSharedFile = (path, pattern) => {
  if (!pattern.includes('*')) return path === pattern
  const [prefix, suffix] = pattern.split('*')
  return path.startsWith(prefix) && path.endsWith(suffix)
}
const isSharedFile = (path) => SHARED_FILES.some((pattern) => matchesSharedFile(normalizePath(path), pattern))
const hasExpectedArtifactPath = (patchPath, cluster) => {
  if (!hasText(patchPath)) return false
  const prefix = `${PATCH_ROOT}/${ARTIFACT_RUN}-issue-fleet-${sortedNumbers(cluster).join('-')}.`
  const suffix = '/change.patch'
  if (!patchPath.startsWith(prefix) || !patchPath.endsWith(suffix)) return false
  const artifactName = patchPath.slice(prefix.length, -suffix.length)
  return /^[A-Za-z0-9]{6,}$/.test(artifactName)
}

const requestedArgs = (Array.isArray(args) ? args : [args]).filter(Boolean)
const issues = sortedNumbers(requestedArgs)
if (issues.length === 0) {
  throw new Error('Pass issue numbers via args, e.g. {args: [103, 104, 105]}')
}
if (issues.length !== requestedArgs.length) {
  throw new Error('Pass each issue number exactly once as an integer, e.g. {args: [103, 104, 105]}')
}

// Measured, not assumed: a fresh worktree is a clean checkout of origin/main, so
// every gitignored artifact is absent. Stating "deps are installed" here is how an
// agent ends up either running `npm install` or reporting a gate it never ran.
const CONTRACT = `
Repository: ${REPO}
You are working in your OWN git worktree, branched from origin/main. Your worktree
root is your cwd — stay inside it. Never cd into ${REPO} itself: other people have
uncommitted work there.

**Read \`docs/agent-harness.md\` first and follow it.** It is the contract for this
work: the verification gate, the shared-file ownership rules, and the prohibitions
(no commits, no \`pip install -r requirements.txt\`, no weight downloads).

Environment facts you must not re-derive:
- Python is \`${PY}\` — the main repo's venv interpreter. Run it from your worktree
  root and it executes against YOUR code. This is verified; use it as-is.
- Your worktree has NO \`venv/\` and NO \`apps/web/node_modules/\` (both are
  gitignored, so a fresh worktree lacks them). Do NOT run \`pip install\` or
  \`npm install\`.
- The frontend gate applies ONLY if you changed something under \`apps/web/\`.
  - If you did, symlink the deps first, from your worktree root:
    \`ln -s ${REPO}/apps/web/node_modules apps/web/node_modules\`
    then run \`npm --prefix apps/web test\` and \`npm --prefix apps/web run build\`.
  - If you changed nothing under \`apps/web/\`, report frontend and build as
    "not applicable — no apps/web changes". Do NOT claim you ran them.
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
  { label: 'triage', phase: 'Triage', schema: TRIAGE_SCHEMA, isolation: 'worktree' },
)

const plan = triage?.issues ?? []
const reportedConflicts = triage?.conflicts ?? []
if (plan.length === 0) {
  throw new Error('Triage returned no issues; nothing to implement.')
}
const triagedNumbers = sortedNumbers(plan.map((item) => item.number))
if (triagedNumbers.length !== plan.length || !sameValues(triagedNumbers, issues)) {
  throw new Error(
    `Triage issue set must exactly match requested issues: requested ${issues.join(', ')}, received ${triagedNumbers.join(', ') || 'none'}`,
  )
}

// Two agents editing the same file in separate worktrees do not collide on disk —
// they collide at integration, where their diffs have to be reconciled by hand.
// So issues that share a file are given to ONE agent as a cluster instead of being
// split across two. Derive conflict edges from the paths it already supplied, but
// retain valid model-reported edges too: an incomplete file map must not split a
// cluster that triage explicitly identified as conflicting.
const planNumbers = new Set(plan.map((item) => item.number))
const pathOwners = new Map()
for (const item of plan) {
  for (const file of normalizedPaths(item.files_to_change)) {
    if (!pathOwners.has(file)) pathOwners.set(file, [])
    pathOwners.get(file).push(item.number)
  }
}
const conflictByPair = new Map()
for (const [path, owners] of pathOwners) {
  const uniqueOwners = sortedNumbers(owners)
  for (let first = 0; first < uniqueOwners.length; first += 1) {
    for (let second = first + 1; second < uniqueOwners.length; second += 1) {
      const [a, b] = [uniqueOwners[first], uniqueOwners[second]]
      const key = `${a}:${b}`
      if (!conflictByPair.has(key)) conflictByPair.set(key, { a, b, shared_paths: [] })
      conflictByPair.get(key).shared_paths.push(path)
    }
  }
}
for (const reportedConflict of reportedConflicts) {
  const numbers = sortedNumbers([reportedConflict?.a, reportedConflict?.b])
  if (numbers.length !== 2 || !numbers.every((number) => planNumbers.has(number))) continue
  const [a, b] = numbers
  const key = `${a}:${b}`
  if (!conflictByPair.has(key)) conflictByPair.set(key, { a, b, shared_paths: [] })
  conflictByPair.get(key).shared_paths.push(...normalizedPaths(reportedConflict.shared_paths))
}
const conflicts = [...conflictByPair.values()].map((conflict) => ({
  ...conflict,
  shared_paths: normalizedPaths(conflict.shared_paths),
}))
const reportedConflictKeys = new Set(
  reportedConflicts
    .map((conflict) => sortedNumbers([conflict.a, conflict.b]))
    .filter((numbers) => numbers.length === 2)
    .map((numbers) => `${numbers[0]}:${numbers[1]}`),
)
const missingReportedConflicts = conflicts.filter((conflict) => !reportedConflictKeys.has(`${conflict.a}:${conflict.b}`))

// Clusters are connected components over the deterministically derived pairs.
const parent = new Map(plan.map((item) => [item.number, item.number]))
const find = (n) => {
  let root = n
  while (parent.get(root) !== root) root = parent.get(root)
  let cursor = n
  while (parent.get(cursor) !== cursor) {
    const next = parent.get(cursor)
    parent.set(cursor, root)
    cursor = next
  }
  return root
}
const union = (a, b) => {
  if (!parent.has(a) || !parent.has(b)) return
  const [ra, rb] = [find(a), find(b)]
  if (ra !== rb) parent.set(Math.max(ra, rb), Math.min(ra, rb))
}
for (const conflict of conflicts) union(conflict.a, conflict.b)

const clusterMap = new Map()
for (const item of plan) {
  const root = find(item.number)
  if (!clusterMap.has(root)) clusterMap.set(root, [])
  clusterMap.get(root).push(item)
}
const clusters = [...clusterMap.values()].map((items) => ({
  items,
  numbers: items.map((i) => i.number).sort((x, y) => x - y),
  sharedPaths: [
    ...new Set(
      conflicts
        .filter((c) => items.some((i) => i.number === c.a) && items.some((i) => i.number === c.b))
        .flatMap((c) => c.shared_paths || []),
    ),
  ].sort(),
}))

log(
  `triaged ${plan.length} issues into ${clusters.length} clusters: ` +
    clusters.map((c) => c.numbers.map((n) => `#${n}`).join('+')).join(', '),
)
if (conflicts.length > 0) {
  log(`file overlaps: ${conflicts.map((c) => `#${c.a}~#${c.b} (${(c.shared_paths || []).join(', ')})`).join('; ')}`)
}
if (missingReportedConflicts.length) {
  log(
    `triage omitted overlaps derived from files_to_change: ${missingReportedConflicts
      .map((c) => `#${c.a}~#${c.b}`)
      .join(', ')}`,
  )
}
const blocked = plan.filter((i) => i.blocked_by_environment).map((i) => `#${i.number}`)
if (blocked.length) log(`blocked by environment: ${blocked.join(', ')}`)

const IMPLEMENT_SCHEMA = {
  type: 'object',
  properties: {
    numbers: { type: 'array', items: { type: 'integer' } },
    completed: { type: 'array', items: { type: 'integer' }, description: 'Issue numbers actually finished' },
    files_changed: { type: 'array', items: { type: 'string' } },
    public_signatures: { type: 'array', items: { type: 'string' } },
    verification: {
      type: 'object',
      properties: {
        backend: { type: 'string' },
        frontend: { type: 'string' },
        build: { type: 'string' },
        red_before_fix: { type: 'string', description: 'How you confirmed each new test fails without the fix' },
      },
      required: ['backend', 'frontend', 'build', 'red_before_fix'],
    },
    wiring_needed: {
      type: 'array',
      description: 'Edits to shared files the integrator must make',
      items: { type: 'string' },
    },
    could_not_do: { type: 'array', items: { type: 'string' } },
    handoff: {
      type: 'object',
      description: 'Measured metadata for the authoritative patch artifact',
      properties: {
        patch_path: { type: 'string', minLength: 1 },
        patch_bytes: { type: 'integer', minimum: 1 },
        patch_sha256: { type: 'string', pattern: '^[a-f0-9]{64}$' },
        base_commit: { type: 'string', pattern: '^[a-f0-9]{40,64}$' },
        changed_files: { type: 'array', minItems: 1, items: { type: 'string', minLength: 1 } },
      },
      required: ['patch_path', 'patch_bytes', 'patch_sha256', 'base_commit', 'changed_files'],
    },
    diff_excerpt: {
      type: 'string',
      description: 'First ~4000 chars of the diff, for humans only. Never the transport.',
    },
  },
  required: ['numbers', 'completed', 'files_changed', 'verification', 'could_not_do', 'handoff'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    numbers: { type: 'array', items: { type: 'integer' } },
    verdict: { type: 'string', enum: ['ship', 'fix_first', 'reject'] },
    tests_rerun_output: { type: 'string', minLength: 1 },
    tests_rerun_passed: {
      type: 'boolean',
      description: 'True only when the verifier actually re-ran the required gate and it passed',
    },
    red_proof_confirmed: {
      type: 'string',
      minLength: 1,
      description: 'What you observed when you reverted the source fix and re-ran the new tests',
    },
    red_proof_is_assertion_failure: {
      type: 'boolean',
      description: 'True only when the reproduced red proof failed through an assertion, not an ImportError or setup failure',
    },
    handoff: {
      type: 'object',
      description: 'Verifier-observed artifact metadata and patch-application result',
      properties: {
        patch_path: { type: 'string', minLength: 1 },
        patch_bytes: { type: 'integer', minimum: 1 },
        patch_sha256: { type: 'string', pattern: '^[a-f0-9]{64}$' },
        base_commit: { type: 'string', pattern: '^[a-f0-9]{40,64}$' },
        changed_files: { type: 'array', minItems: 1, items: { type: 'string', minLength: 1 } },
        apply_check: { type: 'boolean' },
      },
      required: ['patch_path', 'patch_bytes', 'patch_sha256', 'base_commit', 'changed_files', 'apply_check'],
    },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          issue_number: { type: 'integer' },
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
  required: [
    'numbers',
    'verdict',
    'tests_rerun_output',
    'tests_rerun_passed',
    'red_proof_confirmed',
    'red_proof_is_assertion_failure',
    'handoff',
    'findings',
    'acceptance_criteria_unmet',
    'summary',
  ],
}

// Each cluster runs implement -> verify independently. pipeline() means a fast
// cluster reaches Verify while a slow one is still being implemented, and a
// failure in one cluster drops only that cluster.
phase('Implement')
const results = await pipeline(
  clusters,
  (cluster) =>
    agent(
      `${CONTRACT}

Implement ${cluster.items.length > 1 ? 'these GitHub issues' : 'this GitHub issue'}, in this order:
${cluster.items.map((i) => `  - #${i.number}: ${i.title}\n    triage says it touches: ${(i.files_to_change || []).join(', ') || '(determine yourself)'}`).join('\n')}
${cluster.items.length > 1 ? `\nThese were grouped together because they edit the same files (${cluster.sharedPaths.join(', ') || 'overlapping paths'}). One agent owns all of them so their changes cannot conflict.` : ''}
${cluster.items.some((i) => i.needs_ui_review) ? '\nAt least one of these changes the web UI, so the AGENTS.md review requirements apply to that part.' : ''}
${cluster.items.some((i) => i.blocked_by_environment) ? '\nTriage flagged part of this as not fully verifiable here. Implement it anyway and be explicit about what you could not verify.' : ''}

Steps:
1. \`gh issue view <number>\` for each and read the acceptance criteria. They are the definition of done.
2. Implement the changes. Follow the conventions in CLAUDE.md and docs/agent-harness.md.
3. Add or update tests. For a bug fix, write the test so that it **fails before your
   fix and passes after**. Prove it: revert ONLY the source change (keep any new
   imports/exports so the failure is a real assertion, not an ImportError), run the
   test, observe it go red, then restore. Put the exact red output in
   verification.red_before_fix. A test that passes either way is worth nothing.
4. Run the full gate: \`${PY} -m pytest -q\` from your worktree root, plus the
   frontend gate only if it applies (see the environment facts above).
5. Do NOT commit. Hand the work over as a FILE, not as a returned string:
   \`\`\`
   mkdir -p ${PATCH_ROOT}
   artifact_dir="$(mktemp -d \"${PATCH_ROOT}/${ARTIFACT_RUN}-issue-fleet-${cluster.numbers.join('-')}.XXXXXX\")"
   patch_path="$artifact_dir/change.patch"
   base_commit="$(git rev-parse HEAD)"
   git add -A
   git diff --cached --binary > "$patch_path"
   changed_files="$(git diff --cached --name-only)"
   patch_bytes="$(wc -c < "$patch_path" | tr -d '[:space:]')"
   patch_sha256="$(shasum -a 256 "$patch_path" | awk '{print $1}')"
   git reset
   \`\`\`
   \`git add -A\` first so new files are included; \`--binary\` so nothing is lossy.
   \`mktemp -d\` makes the artifact directory unique across concurrent and retried
   runs. Return the measured values as
   \`handoff: { patch_path, patch_bytes, patch_sha256, base_commit, changed_files }\`.
   \`files_changed\` and \`handoff.changed_files\` must list the same paths. Then
   verify your own handover: \`cd\` to a scratch clone at \`base_commit\`, \`git apply
   --check\` the file, and only report success if it applies. Put at most the first
   4000 characters in diff_excerpt — a returned string is NOT the transport and
   will be truncated and HTML-escaped.

If an issue in this cluster turns out to be wrong or already fixed, say so in
could_not_do and leave it out of completed rather than inventing a change.
If you need a shared file edited, put it in wiring_needed instead of editing it.`,
      {
        label: `impl:${cluster.numbers.map((n) => `#${n}`).join('+')}`,
        phase: 'Implement',
        schema: IMPLEMENT_SCHEMA,
        isolation: 'worktree',
      },
    ),
  (built, cluster) => {
    if (!built) return null
    // pipeline() returns the LAST stage's value, so carry the implementation
    // forward with the verdict — otherwise the diffs never reach the orchestrator.
    return agent(
      `${CONTRACT}

Adversarially review the implementation of ${cluster.numbers.map((n) => `#${n}`).join(', ')}:
${cluster.items.map((i) => `  - #${i.number}: ${i.title}`).join('\n')}

The implementing agent reported:
- claims completed: ${(built.completed || []).map((n) => `#${n}`).join(', ') || '(none)'}
- files: ${(built.files_changed || []).join(', ')}
- could not do: ${(built.could_not_do || []).join('; ') || '(nothing reported)'}
- backend: ${built.verification?.backend || '(not reported)'}
- frontend: ${built.verification?.frontend || '(not reported)'}
- red-before-fix claim: ${built.verification?.red_before_fix || '(NOT REPORTED — treat as unproven)'}

Its patch is a FILE — read it from disk, do not work from any excerpt:
  path: ${built.handoff?.patch_path || '(NO PATCH PATH REPORTED — report this as a high finding and stop)'}
  expected bytes: ${built.handoff?.patch_bytes ?? 'unknown'}
  expected SHA-256: ${built.handoff?.patch_sha256 || 'unknown'}
  expected base commit: ${built.handoff?.base_commit || 'unknown'}
  expected changed files: ${(built.handoff?.changed_files || []).join(', ') || '(none)'}

Before you trust or apply the file, independently measure and record every item:
1. Confirm your \`git rev-parse HEAD\` exactly matches the reported base commit.
2. Confirm the file exists, compare \`wc -c < patch\` against expected bytes, and
   compare \`shasum -a 256 patch\` against expected SHA-256.
3. Run \`git apply --check patch\`, then use \`git apply --numstat patch\` to list
   its actual changed paths. The actual paths must match both reported file lists.

If any metadata check or \`git apply --check\` fails, do NOT apply or revert the
patch. Return \`handoff.apply_check: false\`, include the measured/attempted
handoff values, and return \`fix_first\` or \`reject\`. A patch that does not
apply is a high finding on the handover, not something to work around.

Your job is to find what is WRONG, not to confirm it works.

1. \`gh issue view <number>\` for each and check every acceptance criterion against
   the diff. List any that are claimed but not actually met.
2. Only after all handoff checks pass, apply the patch file in your own isolated
   worktree and re-run \`${PY} -m pytest -q\`. Record the tail in
   \`tests_rerun_output\` and set \`tests_rerun_passed: true\` only if it passed.
3. Independently reproduce the red proof. Revert just the source change — keeping
   imports and exports intact so you get a real assertion failure rather than an
   ImportError — run the new tests, and confirm they go red for the RIGHT reason.
   Put what you actually saw in red_proof_confirmed. An ImportError is NOT a valid
   red proof; report it as a high finding. Set \`red_proof_is_assertion_failure:
   true\` only for that assertion failure.
4. Hunt specifically for: assertions that cannot fail, silent \`except: pass\`,
   behavior changes outside the issue's scope, edits to shared files, tests
   rewritten to accommodate a bug rather than fix it, and claims of verification
   that the environment cannot support (no weights, no TTS, no system ffmpeg, and
   no node_modules in a fresh worktree).

Default to skepticism. If you cannot convince yourself something holds, report it.
Return verdict "ship" only when every acceptance criterion is met and the tests are real.
For every verdict, return \`handoff: { patch_path, patch_bytes, patch_sha256,
base_commit, changed_files, apply_check }\` using values you observed. A ship
verdict must set both evidence booleans to true, use \`numbers\` exactly equal to
the implementation's completed issue numbers, and have no unmet acceptance
criteria or high-severity findings; otherwise it will be rejected by the admission
gate.`,
      {
        label: `verify:${cluster.numbers.map((n) => `#${n}`).join('+')}`,
        phase: 'Verify',
        schema: VERDICT_SCHEMA,
        effort: 'high',
        isolation: 'worktree',
      },
    ).then((verdict) => ({ cluster: cluster.numbers, built, verdict }))
  },
)

const validHandoff = (handoff) =>
  hasText(handoff?.patch_path) &&
  Number.isInteger(handoff?.patch_bytes) &&
  handoff.patch_bytes > 0 &&
  /^[a-f0-9]{64}$/.test(handoff.patch_sha256 || '') &&
  /^[a-f0-9]{40,64}$/.test(handoff.base_commit || '') &&
  normalizedPaths(handoff?.changed_files).length > 0

const matchingHandoff = (builtHandoff, verifiedHandoff) =>
  builtHandoff.patch_path === verifiedHandoff.patch_path &&
  builtHandoff.patch_bytes === verifiedHandoff.patch_bytes &&
  builtHandoff.patch_sha256 === verifiedHandoff.patch_sha256 &&
  builtHandoff.base_commit === verifiedHandoff.base_commit &&
  sameValues(normalizedPaths(builtHandoff.changed_files), normalizedPaths(verifiedHandoff.changed_files))

const admissionFor = (entry) => {
  const reasons = []
  const cluster = sortedNumbers(entry.cluster)
  const built = entry.built
  const verdict = entry.verdict
  const completed = sortedNumbers(built?.completed)
  const verified = sortedNumbers(verdict?.numbers)
  const implementationNumbers = sortedNumbers(built?.numbers)
  const filesChanged = normalizedPaths(built?.files_changed)
  const builtHandoff = built?.handoff
  const verifiedHandoff = verdict?.handoff
  const unmetCriteria = (Array.isArray(verdict?.acceptance_criteria_unmet) ? verdict.acceptance_criteria_unmet : []).filter(hasText)
  const highFindings = (Array.isArray(verdict?.findings) ? verdict.findings : []).filter(
    (finding) => finding?.severity === 'high',
  )

  if (verdict?.verdict !== 'ship') reasons.push(`verdict is ${verdict?.verdict ?? 'missing'}`)
  if (!sameValues(implementationNumbers, cluster)) reasons.push('implementation numbers do not match its cluster')
  if (completed.length === 0) reasons.push('implementation reported no completed issues')
  if (!sameValues(completed, verified)) reasons.push('verified issue numbers do not exactly match completed issues')
  if (!completed.every((number) => cluster.includes(number))) reasons.push('completed issue numbers fall outside the cluster')
  if (!hasText(verdict?.tests_rerun_output)) reasons.push('verification test output is missing')
  if (verdict?.tests_rerun_passed !== true) reasons.push('verification gate did not pass')
  if (!hasText(verdict?.red_proof_confirmed)) reasons.push('verification red-proof evidence is missing')
  if (verdict?.red_proof_is_assertion_failure !== true) reasons.push('verification red proof was not an assertion failure')
  if (unmetCriteria.length) reasons.push('verifier reported unmet acceptance criteria')
  if (highFindings.length) reasons.push('verifier reported high-severity findings')
  if (!validHandoff(builtHandoff)) reasons.push('implementation handoff metadata is missing or invalid')
  if (!validHandoff(verifiedHandoff)) reasons.push('verification handoff metadata is missing or invalid')
  if (validHandoff(builtHandoff) && validHandoff(verifiedHandoff)) {
    if (!hasExpectedArtifactPath(builtHandoff.patch_path, cluster)) {
      reasons.push('implementation handoff path is outside its unique artifact directory')
    }
    if (!hasExpectedArtifactPath(verifiedHandoff.patch_path, cluster)) {
      reasons.push('verification handoff path is outside its unique artifact directory')
    }
    if (!matchingHandoff(builtHandoff, verifiedHandoff)) reasons.push('verification handoff does not match implementation handoff')
    if (!sameValues(filesChanged, normalizedPaths(builtHandoff.changed_files))) {
      reasons.push('implementation files_changed does not match its handoff')
    }
    if (!sameValues(filesChanged, normalizedPaths(verifiedHandoff.changed_files))) {
      reasons.push('verifier changed files do not match implementation files_changed')
    }
  }
  if (verifiedHandoff?.apply_check !== true) reasons.push('verifier did not confirm git apply --check')

  const protectedFiles = normalizedPaths([
    ...filesChanged,
    ...(builtHandoff?.changed_files || []),
    ...(verifiedHandoff?.changed_files || []),
  ]).filter(isSharedFile)
  if (protectedFiles.length) reasons.push(`patch edits shared files: ${protectedFiles.join(', ')}`)

  return { shippable: reasons.length === 0, reasons, completed }
}

phase('Report')
const done = results.filter(Boolean)
const verdicts = done.map((entry) => entry.verdict).filter(Boolean)
const admitted = done.map((entry) => ({ ...entry, admission: admissionFor(entry) }))
const shippable = admitted.filter((entry) => entry.admission.shippable).flatMap((entry) => entry.admission.completed)
const needsWork = admitted.filter((entry) => !entry.admission.shippable)
const dropped = clusters
  .filter((c, index) => !results[index])
  .map((c) => c.numbers.map((n) => `#${n}`).join('+'))

log(`ship: ${shippable.length ? shippable.map((n) => `#${n}`).join(', ') : 'none'}`)
if (needsWork.length) {
  log(
    `needs work: ${needsWork
      .map(
        (entry) =>
          `${entry.cluster.map((n) => `#${n}`).join('+')}(${entry.verdict?.verdict ?? 'no verdict'}: ${entry.admission.reasons.join('; ')})`,
      )
      .join(', ')}`,
  )
}
if (dropped.length) {
  log(`dropped (agent failed, no result): ${dropped.join(', ')}`)
}

// Integration is deliberately NOT automated: committing, wiring shared files, and
// opening PRs stay with the orchestrator, who can see the whole picture.
return {
  triage,
  artifact_run: ARTIFACT_RUN,
  clusters: clusters.map((c) => c.numbers),
  verdicts,
  shippable,
  needs_work: needsWork.map((entry) => ({
    cluster: entry.cluster,
    verdict: entry.verdict?.verdict ?? 'no verdict',
    admission_reasons: entry.admission.reasons,
    summary: entry.verdict?.summary ?? '',
    findings: entry.verdict?.findings ?? [],
    acceptance_criteria_unmet: entry.verdict?.acceptance_criteria_unmet ?? [],
  })),
  dropped,
  patches: admitted.map((entry) => ({
    cluster: entry.cluster.map((n) => `#${n}`).join('+'),
    patch_path: entry.built?.handoff?.patch_path ?? null,
    patch_bytes: entry.built?.handoff?.patch_bytes ?? null,
    patch_sha256: entry.built?.handoff?.patch_sha256 ?? null,
    base_commit: entry.built?.handoff?.base_commit ?? null,
    changed_files: entry.built?.handoff?.changed_files ?? [],
    completed: entry.built?.completed ?? [],
    admitted: entry.admission.shippable,
    admission_reasons: entry.admission.reasons,
    wiring_needed: entry.built?.wiring_needed ?? [],
    could_not_do: entry.built?.could_not_do ?? [],
  })),
  next_step:
    'Orchestrator: apply the shippable diffs, make any wiring_needed edits to shared files, run the full gate, then commit and open a PR.',
}
