import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function loadApplyAdvancedConfig(request) {
  const start = source.indexOf('function requestedProfileConfigSections(')
  const end = source.indexOf('// ── edit profile dialog', start)
  const context = {
    host: { request },
    requestForBot: (_bot, method, params) => request(method, params),
    ensureMessagingProtocol: soul => soul,
    $lastRoster: { get: () => [] }
  }
  const section = source
    .slice(start, end)
    .concat('\nglobalThis.__advanced = { applyAdvancedConfig, applyProfileCapabilities };\n')

  assert.notEqual(start, -1, 'advanced acknowledgement helpers are missing')
  assert.notEqual(end, -1, 'advanced-config section delimiter is missing')
  vm.runInNewContext(section, context, { filename: 'advanced-config.js' })
  return context.__advanced
}

function state(patch = {}) {
  return {
    provider: '',
    model: '',
    soul: '',
    skills: [],
    toolsets: [],
    dirtyModel: false,
    dirtySoul: false,
    dirtySkills: false,
    dirtyToolsets: false,
    dirtyMcp: false,
    mcp: [],
    ...patch
  }
}

async function toolsetConfigureParams(toolsets) {
  const calls = []
  const { applyAdvancedConfig: apply } = loadApplyAdvancedConfig(async (method, params) => {
    calls.push({ method, params })
    return { ok: true, applied: { toolsets: true } }
  })

  await apply(
    { name: 'ops' },
    state({
      dirtyToolsets: true,
      toolsets
    })
  )

  assert.equal(calls.length, 1)
  assert.equal(calls[0].method, 'profiles.configure')
  return JSON.parse(JSON.stringify(calls[0].params))
}

test('regression: selecting Inherit explicitly clears the profile model assignment', async () => {
  const calls = []
  const { applyAdvancedConfig: apply } = loadApplyAdvancedConfig(async (method, params) => {
    calls.push({ method, params })
    return { code: 0, blocked: false, output: 'Unset model' }
  })

  const result = await apply({ name: 'ops' }, state({ dirtyModel: true }))

  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
    {
      method: 'cli.exec',
      params: { argv: ['--profile', 'ops', 'config', 'unset', 'model'] }
    }
  ])
  assert.deepEqual(JSON.parse(JSON.stringify(result)), { ok: true, applied: { model: true } })
})

test('integration: model clearing and other dirty sections report a merged result', async () => {
  const calls = []
  const { applyAdvancedConfig: apply } = loadApplyAdvancedConfig(async (method, params) => {
    calls.push({ method, params })
    if (method === 'cli.exec') return { code: 0, blocked: false, output: 'Unset model' }
    return { ok: true, applied: { soul: true } }
  })

  const result = await apply({ name: 'ops' }, state({ dirtyModel: true, dirtySoul: true, soul: '# Ops' }))

  assert.equal(calls[0].method, 'cli.exec')
  assert.deepEqual(JSON.parse(JSON.stringify(calls[1])), {
    method: 'profiles.configure',
    params: { name: 'ops', soul: '# Ops' }
  })
  assert.deepEqual(JSON.parse(JSON.stringify(result)), {
    ok: true,
    applied: { model: true, soul: true }
  })
})

test('regression: a rejected model clear is reported as a failed section', async () => {
  const { applyAdvancedConfig: apply } = loadApplyAdvancedConfig(async () => ({
    code: 1,
    blocked: false,
    output: 'Config key not set'
  }))

  const result = await apply({ name: 'ops' }, state({ dirtyModel: true }))

  assert.deepEqual(JSON.parse(JSON.stringify(result)), { ok: false, applied: { model: false } })
})

test('all selected toolsets explicitly clear the exact profile pin', async () => {
  assert.deepEqual(
    await toolsetConfigureParams([
      { name: 'artifact_read', enabled: true },
      { name: 'web', enabled: true }
    ]),
    {
      name: 'ops',
      enabled_toolsets: ['artifact_read', 'web'],
      clear_enabled_toolsets: true
    }
  )
})

test('no selected toolsets persist an exact empty profile pin', async () => {
  assert.deepEqual(
    await toolsetConfigureParams([
      { name: 'artifact_read', enabled: false },
      { name: 'web', enabled: false }
    ]),
    { name: 'ops', enabled_toolsets: [] }
  )
})

test('a selected toolset subset persists only that exact subset', async () => {
  assert.deepEqual(
    await toolsetConfigureParams([
      { name: 'artifact_read', enabled: true },
      { name: 'web', enabled: false }
    ]),
    { name: 'ops', enabled_toolsets: ['artifact_read'] }
  )
})

test('regression: an advanced-section failure suppresses the contradictory success toast', () => {
  const start = source.indexOf('function EditProfileDialog(')
  const end = source.indexOf('function CreateAgentDialog(', start)
  const dialog = source.slice(start, end)

  assert.match(dialog, /let advancedFailed = false/)
  assert.match(dialog, /if \(!advancedFailed && !lookFailed\) \{\s*host\.notify\(\{ kind: 'success'/)
})

test('mixed-version gateway cannot silently acknowledge an explicit toolset clear', async () => {
  const { applyAdvancedConfig: apply } = loadApplyAdvancedConfig(async () => ({}))

  const result = await apply(
    { name: 'ops' },
    state({
      dirtyToolsets: true,
      toolsets: [
        { name: 'artifact_read', enabled: true },
        { name: 'web', enabled: true }
      ]
    })
  )

  assert.deepEqual(JSON.parse(JSON.stringify(result)), {
    ok: false,
    applied: { toolsets: false }
  })
})

test('Edit rejects legacy applied=true for an exact-empty toolset replacement', async () => {
  const { applyAdvancedConfig: apply } = loadApplyAdvancedConfig(async () => ({
    ok: true,
    applied: { toolsets: true }
  }))

  const result = await apply(
    { name: 'ops' },
    state({
      dirtyToolsets: true,
      toolsets: [
        { name: 'artifact_read', enabled: false },
        { name: 'web', enabled: false }
      ]
    })
  )

  assert.deepEqual(JSON.parse(JSON.stringify(result)), {
    ok: false,
    applied: { toolsets: false }
  })
})

test('Edit accepts none and subset only with a replaced acknowledgement', async () => {
  for (const toolsets of [
    [
      { name: 'artifact_read', enabled: false },
      { name: 'web', enabled: false }
    ],
    [
      { name: 'artifact_read', enabled: true },
      { name: 'web', enabled: false }
    ]
  ]) {
    const { applyAdvancedConfig: apply } = loadApplyAdvancedConfig(async () => ({
      ok: true,
      applied: { toolsets: true },
      acknowledged: { toolsets: 'replaced' }
    }))
    const result = await apply(
      { name: 'ops' },
      state({ dirtyToolsets: true, toolsets })
    )
    assert.equal(result.ok, true)
    assert.deepEqual(JSON.parse(JSON.stringify(result.applied)), { toolsets: true })
  }
})

test('explicit clear requires the gateway to acknowledge clear semantics', async () => {
  const { applyAdvancedConfig: apply } = loadApplyAdvancedConfig(async () => ({
    ok: true,
    applied: { toolsets: true },
    acknowledged: { toolsets: 'replaced' }
  }))

  const result = await apply(
    { name: 'ops' },
    state({
      dirtyToolsets: true,
      toolsets: [{ name: 'artifact_read', enabled: true }]
    })
  )

  assert.equal(result.ok, false)
  assert.deepEqual(JSON.parse(JSON.stringify(result.applied)), { toolsets: false })
})

test('explicit clear succeeds only with a clear acknowledgement', async () => {
  const { applyAdvancedConfig: apply } = loadApplyAdvancedConfig(async () => ({
    ok: true,
    applied: { toolsets: true },
    acknowledged: { toolsets: 'cleared' }
  }))

  const result = await apply(
    { name: 'ops' },
    state({
      dirtyToolsets: true,
      toolsets: [{ name: 'artifact_read', enabled: true }]
    })
  )

  assert.equal(result.ok, true)
  assert.deepEqual(JSON.parse(JSON.stringify(result.applied)), { toolsets: true })
})

test('New Agent capability application stops on an unacknowledged response', async () => {
  const { applyProfileCapabilities } = loadApplyAdvancedConfig(async () => ({}))

  await assert.rejects(
    applyProfileCapabilities(
      async () => ({}),
      'ops',
      { enabled_toolsets: ['artifact_read'], clear_enabled_toolsets: true }
    ),
    /did not acknowledge.*toolsets/i
  )
})

test('New Agent rejects legacy applied=true for an exact-empty replacement', async () => {
  const request = async () => ({ ok: true, applied: { toolsets: true } })
  const { applyProfileCapabilities } = loadApplyAdvancedConfig(request)

  await assert.rejects(
    applyProfileCapabilities(request, 'ops', { enabled_toolsets: [] }),
    /did not acknowledge.*toolsets/i
  )
})

test('New Agent accepts an exact-empty replacement only when acknowledged as replaced', async () => {
  const request = async () => ({
    ok: true,
    applied: { toolsets: true },
    acknowledged: { toolsets: 'replaced' }
  })
  const { applyProfileCapabilities } = loadApplyAdvancedConfig(request)

  const result = await applyProfileCapabilities(request, 'ops', { enabled_toolsets: [] })

  assert.equal(result.ok, true)
  assert.deepEqual(JSON.parse(JSON.stringify(result.applied)), { toolsets: true })
})

test('New Agent capability application accepts an acknowledged subset', async () => {
  const calls = []
  const request = async (method, params) => {
    calls.push({ method, params })
    return {
      ok: true,
      applied: { toolsets: true },
      acknowledged: { toolsets: 'replaced' }
    }
  }
  const { applyProfileCapabilities } = loadApplyAdvancedConfig(request)

  const result = await applyProfileCapabilities(
    request,
    'ops',
    { enabled_toolsets: ['artifact_read'] }
  )

  assert.equal(result.ok, true)
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
    {
      method: 'profiles.configure',
      params: { name: 'ops', enabled_toolsets: ['artifact_read'] }
    }
  ])
})

test('Edit reports a missing acknowledgement for every dirty gateway section', async () => {
  const { applyAdvancedConfig: apply } = loadApplyAdvancedConfig(async () => ({ ok: true, applied: {} }))

  const result = await apply(
    { name: 'ops' },
    state({ dirtySoul: true, soul: '# Ops', dirtySkills: true, skills: [] })
  )

  assert.equal(result.ok, false)
  assert.deepEqual(JSON.parse(JSON.stringify(result.applied)), {
    soul: false,
    skills: false
  })
})
