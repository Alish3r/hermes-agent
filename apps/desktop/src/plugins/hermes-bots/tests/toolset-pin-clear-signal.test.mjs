import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// `profiles.configure` changed the wire meaning of an empty toolset list.
//
//   old gateway: `enabled_toolsets: []`          -> clear the pin
//   new gateway: `enabled_toolsets: []`          -> pin ZERO tools
//                `clear_enabled_toolsets: true`  -> clear the pin
//
// This plugin sends `[]` for "all toolsets ticked" and for "none ticked",
// both meaning "no pin". Against a new gateway that silently strips every
// tool from the bot -- the inverse of what the user selected.
//
// Sending BOTH keys for the clear case is unambiguous in both directions:
// the new gateway checks `clear_enabled_toolsets` first, and the old one
// ignores the unknown flag and sees exactly the empty list it expects.

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function extractFunction(name) {
  const start = source.indexOf(`function ${name}(`)
  assert.notEqual(start, -1, `${name}() is missing from plugin.js`)
  const end = source.indexOf('\n}', start)
  assert.notEqual(end, -1, `${name}() has no closing brace at top level`)
  return source.slice(start, end + 2)
}

// Evaluate the real helper so this tests behaviour, not just its source text.
const toolsetPinPayload = new Function(
  `${extractFunction('toolsetPinPayload')}; return toolsetPinPayload`
)()

const ts = (...names) => names.map(n => ({ name: n, enabled: true }))
const mixed = [
  { name: 'file', enabled: true },
  { name: 'terminal', enabled: false }
]

test('every toolset ticked clears the pin, and says so both ways', () => {
  const payload = toolsetPinPayload(ts('file', 'terminal', 'kanban'))
  assert.deepEqual(payload.enabled_toolsets, [])
  assert.equal(payload.clear_enabled_toolsets, true)
})

test('no toolset ticked clears the pin, and says so both ways', () => {
  const payload = toolsetPinPayload([
    { name: 'file', enabled: false },
    { name: 'terminal', enabled: false }
  ])
  assert.deepEqual(payload.enabled_toolsets, [])
  assert.equal(payload.clear_enabled_toolsets, true)
})

test('a real subset pins exactly that subset and never sends the clear flag', () => {
  const payload = toolsetPinPayload(mixed)
  assert.deepEqual(payload.enabled_toolsets, ['file'])
  assert.equal('clear_enabled_toolsets' in payload, false)
})

test('an empty catalog is treated as "nothing to pin", not a zero-tool pin', () => {
  // length 0 === length 0 hits the all-enabled branch; assert it clears
  // rather than pinning zero tools on a gateway that has not loaded a catalog.
  const payload = toolsetPinPayload([])
  assert.deepEqual(payload.enabled_toolsets, [])
  assert.equal(payload.clear_enabled_toolsets, true)
})

test('no call site builds the toolset pin payload by hand', () => {
  // Both profiles.configure senders must route through the helper, or one of
  // them silently keeps the old ambiguous `[]`.
  const handRolled = source.match(/\.enabled_toolsets\s*=/g) || []
  assert.deepEqual(
    handRolled,
    [],
    'assign the toolset pin via toolsetPinPayload() so the clear signal cannot be dropped'
  )
})
