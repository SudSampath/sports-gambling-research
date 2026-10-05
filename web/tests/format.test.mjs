import test from 'node:test';
import assert from 'node:assert/strict';
import {probability, filterCandidates, inventory} from '../format.js';

test('unknown probability is distinct from zero', () => {
  assert.equal(probability(null), 'Unknown'); assert.equal(probability('0'), '0.0%');
});
test('candidate eligibility and reason filters operate together', () => {
  const rows = [{id: 'a', contract: 'contract-a', event: 'e', eligible: false, reasons: ['unsupported_outcome']},
    {id: 'b', contract: 'contract-b', event: 'e', eligible: true, reasons: ['cost_adjusted_forecast_hypothesis']}];
  assert.deepEqual(filterCandidates(rows, 'UNSUPPORTED', 'excluded'), [rows[0]]);
  assert.deepEqual(filterCandidates(rows, 'unsupported', 'eligible'), []);
});
test('inventory count excludes rejected zero-share admission records', () => {
  assert.equal(inventory({positions: [{state: 'rejected', shares: '0'}, {state: 'pending_settlement', shares: '50'}]}).length, 1);
});
