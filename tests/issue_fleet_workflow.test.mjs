import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const testDirectory = path.dirname(fileURLToPath(import.meta.url))
const workflowPath = path.resolve(testDirectory, '../.claude/workflows/issue-fleet.js')
const workflowSource = await readFile(workflowPath, 'utf8')
const workflowBody = workflowSource.replace(/^export const meta = \{[\s\S]*?\n\}\n\n/, '')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const runWorkflow = new AsyncFunction('args', 'phase', 'agent', 'pipeline', 'log', workflowBody)

const issue = (number, filesToChange) => ({
  number,
  title: `Issue ${number}`,
  summary: `Fixture for issue ${number}`,
  files_to_change: filesToChange,
  touches_shared_files: [],
  needs_ui_review: false,
  blocked_by_environment: false,
  notes: '',
})

const CURRENT_ARTIFACT_PATH = '__CURRENT_ARTIFACT_PATH__'

const implementation = (numbers, overrides = {}) => ({
  numbers,
  completed: numbers,
  files_changed: ['core/example.py'],
  public_signatures: [],
  verification: {
    backend: '1 passed',
    frontend: 'not applicable — no apps/web changes',
    build: 'not applicable — no apps/web changes',
    red_before_fix: 'AssertionError: fixture reproduces the pre-fix failure',
  },
  wiring_needed: [],
  could_not_do: [],
  handoff: {
    patch_path: CURRENT_ARTIFACT_PATH,
    patch_bytes: 42,
    patch_sha256: 'a'.repeat(64),
    base_commit: 'b'.repeat(40),
    changed_files: ['core/example.py'],
  },
  ...overrides,
})

const verdict = (numbers, overrides = {}) => ({
  numbers,
  verdict: 'ship',
  tests_rerun_output: '1 passed',
  tests_rerun_passed: true,
  red_proof_confirmed: 'AssertionError: fixture reproduces the pre-fix failure',
  red_proof_is_assertion_failure: true,
  findings: [],
  acceptance_criteria_unmet: [],
  summary: 'ready',
  handoff: {
    patch_path: CURRENT_ARTIFACT_PATH,
    patch_bytes: 42,
    patch_sha256: 'a'.repeat(64),
    base_commit: 'b'.repeat(40),
    changed_files: ['core/example.py'],
    apply_check: true,
  },
  ...overrides,
})

const withArtifactPath = (value, artifactPath) =>
  JSON.parse(JSON.stringify(value).replaceAll(CURRENT_ARTIFACT_PATH, artifactPath))

async function executeFleet({ issues, triagedIssues = issues, conflicts = [], built, reviewed }) {
  const calls = []
  const logs = []
  let artifactPath = ''
  const agent = async (prompt, options) => {
    calls.push({ prompt, options })
    if (options.label === 'triage') return { issues: triagedIssues, conflicts }
    if (options.label.startsWith('impl:')) {
      const artifactTemplate = prompt.match(/mktemp -d "([^"]+)\.XXXXXX"/)
      assert.ok(artifactTemplate, 'Implementation prompt must include an artifact template')
      artifactPath = `${artifactTemplate[1]}.ABC123/change.patch`
      return withArtifactPath(built, artifactPath)
    }
    if (options.label.startsWith('verify:')) return withArtifactPath(reviewed, artifactPath)
    throw new Error(`Unexpected agent label: ${options.label}`)
  }
  const pipeline = async (items, implement, verify) => {
    const results = []
    for (const item of items) results.push(await verify(await implement(item), item))
    return results
  }

  const result = await runWorkflow(
    issues.map((item) => item.number),
    () => {},
    agent,
    pipeline,
    (message) => logs.push(message),
  )
  return { calls, logs, result }
}

test('isolates implementation and verification worktrees', async () => {
  const { calls, result } = await executeFleet({
    issues: [issue(1, ['core/example.py'])],
    built: implementation([1]),
    reviewed: verdict([1]),
  })

  const workerCalls = calls.filter(({ options }) => options.label.startsWith('impl:') || options.label.startsWith('verify:'))
  const triageCall = calls.find(({ options }) => options.label === 'triage')
  assert.equal(triageCall.options.isolation, 'worktree')
  assert.deepEqual(workerCalls.map(({ options }) => options.isolation), ['worktree', 'worktree'])
  assert.deepEqual(result.shippable, [1])
})

test('clusters exact file overlaps even when triage omits a conflict pair', async () => {
  const { calls, result } = await executeFleet({
    issues: [issue(1, ['core/shared.py']), issue(2, ['core/shared.py'])],
    built: implementation([1, 2]),
    reviewed: verdict([1, 2]),
  })

  assert.deepEqual(result.clusters, [[1, 2]])
  assert.ok(calls.some(({ options }) => options.label === 'impl:#1+#2'))
})

test('keeps a triage-reported conflict when the file map is incomplete', async () => {
  const { result } = await executeFleet({
    issues: [issue(1, ['core/first.py']), issue(2, ['core/second.py'])],
    conflicts: [{ a: 1, b: 2, shared_paths: ['runtime-only overlap'] }],
    built: implementation([1, 2]),
    reviewed: verdict([1, 2]),
  })

  assert.deepEqual(result.clusters, [[1, 2]])
})

test('rejects a triage plan that does not exactly match requested issues', async () => {
  await assert.rejects(
    executeFleet({
      issues: [issue(1, ['core/example.py'])],
      triagedIssues: [issue(2, ['core/example.py'])],
      built: implementation([1]),
      reviewed: verdict([1]),
    }),
    /Triage issue set must exactly match requested issues/,
  )
})

test('does not admit a ship verdict without valid independent verification evidence', async () => {
  const missingEvidenceRun = await executeFleet({
    issues: [issue(1, ['core/example.py'])],
    built: implementation([1]),
    reviewed: verdict([1], { tests_rerun_output: '', red_proof_confirmed: '' }),
  })
  const invalidEvidenceRun = await executeFleet({
    issues: [issue(1, ['core/example.py'])],
    built: implementation([1]),
    reviewed: verdict([1], {
      tests_rerun_output: '1 failed',
      tests_rerun_passed: false,
      red_proof_confirmed: 'ImportError: fixture dependency is missing',
      red_proof_is_assertion_failure: false,
    }),
  })

  assert.deepEqual(missingEvidenceRun.result.shippable, [])
  assert.deepEqual(invalidEvidenceRun.result.shippable, [])
})

test('does not admit a verdict for a different issue number', async () => {
  const { result } = await executeFleet({
    issues: [issue(1, ['core/example.py'])],
    built: implementation([1]),
    reviewed: verdict([2]),
  })

  assert.deepEqual(result.shippable, [])
  assert.equal(result.needs_work.length, 1)
})

test('does not admit a ship verdict with unmet criteria or high findings', async () => {
  const unmetCriteriaRun = await executeFleet({
    issues: [issue(1, ['core/example.py'])],
    built: implementation([1]),
    reviewed: verdict([1], { acceptance_criteria_unmet: ['required behavior is missing'] }),
  })
  const highFindingRun = await executeFleet({
    issues: [issue(1, ['core/example.py'])],
    built: implementation([1]),
    reviewed: verdict([1], {
      findings: [
        {
          severity: 'high',
          claim: 'patch can corrupt the shared checkout',
          failure_scenario: 'Verify runs outside a worktree',
        },
      ],
    }),
  })

  assert.deepEqual(unmetCriteriaRun.result.shippable, [])
  assert.deepEqual(highFindingRun.result.shippable, [])
})

test('does not admit a shared-file edit or an unverifiable handoff', async () => {
  const sharedPath = 'apps/api/main.py'
  const sharedFileRun = await executeFleet({
    issues: [issue(1, [sharedPath])],
    built: implementation([1], {
      files_changed: [sharedPath],
      handoff: { ...implementation([1]).handoff, changed_files: [sharedPath] },
    }),
    reviewed: verdict([1], { handoff: { ...verdict([1]).handoff, changed_files: [sharedPath] } }),
  })
  const wildcardSharedPath = 'generators/scene/__init__.py'
  const wildcardSharedFileRun = await executeFleet({
    issues: [issue(1, [wildcardSharedPath])],
    built: implementation([1], {
      files_changed: [wildcardSharedPath],
      handoff: { ...implementation([1]).handoff, changed_files: [wildcardSharedPath] },
    }),
    reviewed: verdict([1], { handoff: { ...verdict([1]).handoff, changed_files: [wildcardSharedPath] } }),
  })
  const invalidHandoffRun = await executeFleet({
    issues: [issue(1, ['core/example.py'])],
    built: implementation([1]),
    reviewed: verdict([1], { handoff: { ...verdict([1]).handoff, apply_check: false } }),
  })

  assert.deepEqual(sharedFileRun.result.shippable, [])
  assert.deepEqual(wildcardSharedFileRun.result.shippable, [])
  assert.deepEqual(invalidHandoffRun.result.shippable, [])
  assert.match(sharedFileRun.result.needs_work[0].admission_reasons.join(' '), /patch edits shared files/)
  assert.match(wildcardSharedFileRun.result.needs_work[0].admission_reasons.join(' '), /patch edits shared files/)
})

test('does not admit missing or mismatched handoff metadata', async () => {
  const emptyImplementationHandoffRun = await executeFleet({
    issues: [issue(1, ['core/example.py'])],
    built: implementation([1], {
      handoff: { patch_path: '', patch_bytes: 0, patch_sha256: '', base_commit: '', changed_files: [] },
    }),
    reviewed: verdict([1]),
  })
  const mismatchedImplementationFilesRun = await executeFleet({
    issues: [issue(1, ['core/example.py'])],
    built: implementation([1], {
      handoff: { ...implementation([1]).handoff, changed_files: ['core/other.py'] },
    }),
    reviewed: verdict([1], { handoff: { ...verdict([1]).handoff, changed_files: ['core/other.py'] } }),
  })
  const missingHandoffRun = await executeFleet({
    issues: [issue(1, ['core/example.py'])],
    built: implementation([1]),
    reviewed: verdict([1], { handoff: null }),
  })
  const mismatchedHandoffRun = await executeFleet({
    issues: [issue(1, ['core/example.py'])],
    built: implementation([1]),
    reviewed: verdict([1], { handoff: { ...verdict([1]).handoff, patch_sha256: 'c'.repeat(64) } }),
  })
  const unexpectedArtifactRun = await executeFleet({
    issues: [issue(1, ['core/example.py'])],
    built: implementation([1], {
      handoff: {
        ...implementation([1]).handoff,
        patch_path: '/tmp/creative-ai-studio-harness-patches/run-prior123-issue-fleet-1.OLD999/change.patch',
      },
    }),
    reviewed: verdict([1], {
      handoff: {
        ...verdict([1]).handoff,
        patch_path: '/tmp/creative-ai-studio-harness-patches/run-prior123-issue-fleet-1.OLD999/change.patch',
      },
    }),
  })

  assert.deepEqual(emptyImplementationHandoffRun.result.shippable, [])
  assert.deepEqual(mismatchedImplementationFilesRun.result.shippable, [])
  assert.deepEqual(missingHandoffRun.result.shippable, [])
  assert.deepEqual(mismatchedHandoffRun.result.shippable, [])
  assert.deepEqual(unexpectedArtifactRun.result.shippable, [])
  assert.match(
    mismatchedImplementationFilesRun.result.needs_work[0].admission_reasons.join(' '),
    /implementation files_changed does not match its handoff/,
  )
  assert.match(unexpectedArtifactRun.result.needs_work[0].admission_reasons.join(' '), /unique artifact directory/)
})

test('requires verifier-observed handoff and test evidence', async () => {
  const { calls } = await executeFleet({
    issues: [issue(1, ['core/example.py'])],
    built: implementation([1]),
    reviewed: verdict([1]),
  })

  const verifyCall = calls.find(({ options }) => options.label.startsWith('verify:'))
  assert.ok(verifyCall.options.schema.required.includes('handoff'))
  assert.ok(verifyCall.options.schema.required.includes('tests_rerun_passed'))
  assert.ok(verifyCall.options.schema.required.includes('red_proof_is_assertion_failure'))
  assert.match(verifyCall.prompt, /git rev-parse HEAD/)
  assert.match(verifyCall.prompt, /wc -c < patch/)
  assert.match(verifyCall.prompt, /shasum -a 256 patch/)
  assert.match(verifyCall.prompt, /git apply --check patch/)
  assert.match(verifyCall.prompt, /git apply --numstat patch/)
})

test('gives concurrent clusters distinct artifact-directory templates', async () => {
  const { calls } = await executeFleet({
    issues: [issue(1, ['core/first.py']), issue(2, ['core/second.py'])],
    built: implementation([1]),
    reviewed: verdict([1]),
  })

  const implementationPrompts = calls
    .filter(({ options }) => options.label.startsWith('impl:'))
    .map(({ prompt }) => prompt)
  const artifactTemplates = implementationPrompts.map((prompt) => prompt.match(/run-[a-z0-9-]+-issue-fleet-\d+\.XXXXXX/)?.[0])
  const artifactRunIds = artifactTemplates.map((template) => template?.replace(/-issue-fleet-\d+\.XXXXXX$/, ''))

  assert.equal(implementationPrompts.length, 2)
  assert.ok(implementationPrompts.every((prompt) => /artifact_dir=.*mktemp -d/.test(prompt)))
  assert.ok(implementationPrompts.every((prompt) => /patch_path="\$artifact_dir\/change\.patch"/.test(prompt)))
  assert.equal(new Set(artifactTemplates).size, 2)
  assert.equal(new Set(artifactRunIds).size, 1)
})
