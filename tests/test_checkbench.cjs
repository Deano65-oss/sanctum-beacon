const test = require('node:test');
const assert = require('node:assert/strict');
const bench = require('../app/static/checkbench.js');

test('eight synthetic fixture pairs agree, without a formula-correctness claim', () => {
  const report = bench.compare(bench.EXAMPLE, bench.EXAMPLE);
  assert.equal(report.passed, true);
  assert.equal(report.counts.compared, 8);
  assert.equal(report.counts.exactMatches, 8);
  assert.equal(report.counts.withinTolerance, 0);
  assert.match(report.scope, /Does not execute or verify formulas/);
  assert.doesNotThrow(() => JSON.stringify(report));
});

test('changed example accounts for one difference and both kinds of missing case', () => {
  const report = bench.compare(bench.EXAMPLE, bench.CHANGED_EXAMPLE);
  assert.equal(report.passed, false);
  assert.equal(report.outcome, 'differences');
  assert.equal(report.counts.referenceRows, 8);
  assert.equal(report.counts.candidateRows, 8);
  assert.equal(report.counts.compared, 7);
  assert.equal(report.counts.exactMatches, 6);
  assert.equal(report.counts.differences, 1);
  assert.equal(report.counts.missingReference, 1);
  assert.equal(report.counts.missingCandidate, 1);
  assert.equal(report.rows.length, 9);
  assert.equal(report.rows.find(row => row.id === 'half-cent').delta, '-0.01');
});

test('decimal conversion is exact beyond Number precision, including the allowed limits', () => {
  const value = '999999999999999999.999999999999';
  assert.equal(bench.formatDecimal(bench.parseDecimal(value)), value);
  assert.equal(bench.formatDecimal(bench.parseDecimal('-' + value)), '-' + value);
  assert.equal(bench.formatDecimal(bench.parseDecimal('-0.000000000000')), '0');
  assert.equal(bench.formatDecimal(bench.parseDecimal(' +001.2300 ')), '1.23');
  const report = bench.compare('id,result\na,9007199254740992', 'id,result\na,9007199254740993');
  assert.equal(report.passed, false);
  assert.equal(report.rows[0].delta, '1');
});

test('absolute tolerance includes its exact boundary but not one unit beyond', () => {
  const left = 'id,result\nnegative,-10.000000000001\npositive,0.2';
  const right = 'id,result\nnegative,-10\npositive,0.200000000001';
  const accepted = bench.compare(left, right, '0.000000000001');
  assert.equal(accepted.passed, true);
  assert.equal(accepted.counts.withinTolerance, 2);
  assert.equal(accepted.counts.exactMatches, 0);
  const rejected = bench.compare(left, right, '0');
  assert.equal(rejected.counts.differences, 2);
  assert.equal(rejected.passed, false);
  assert.equal(bench.compare('id,result\na,0.1', 'id,result\na,0.3', '0.2').passed, true);
});

test('differences across the full signed range remain exact', () => {
  const report = bench.compare('id,result\na,-999999999999999999.999999999999', 'id,result\na,999999999999999999.999999999999');
  assert.equal(report.rows[0].delta, '1999999999999999999.999999999998');
  assert.equal(report.counts.differences, 1);
});

test('unsupported decimal syntax and digit overflows are rejected rather than rounded', () => {
  for (const value of ['', '.5', '1.', '1e2', 'Infinity', '-Infinity', 'NaN', '0x10', '1,000', '1_000', '1000000000000000000', '0.0000000000001', '1\n2']) {
    assert.throws(() => bench.parseDecimal(value), {code: 'decimal_format'}, value);
  }
  assert.throws(() => bench.parseDecimal(1), {code: 'decimal_format'});
});

test('negative or invalid tolerance invalidates a comparison even when values match', () => {
  for (const tolerance of ['-0.01', 'NaN', '', '1e-4']) {
    const report = bench.compare('id,result\na,1', 'id,result\na,1', tolerance);
    assert.equal(report.passed, false);
    assert.equal(report.outcome, 'invalid');
    assert.equal(report.counts.exactMatches, 0);
    assert.equal(report.counts.unassessed, 1);
    assert.equal(report.errors.at(-1).side, 'tolerance');
  }
});

test('CSV supports quoted commas, escaped quotes, BOM and CRLF without changing IDs', () => {
  const source = '\uFEFFid,result\r\n"case,one","1.00"\r\n"case ""two""",-0.25\r\n';
  const destination = 'id,result\n"case ""two""",-0.250\n"case,one",1';
  const parsed = bench.parseCSV(source);
  assert.deepEqual(parsed, [['id', 'result'], ['case,one', '1.00'], ['case "two"', '-0.25']]);
  assert.equal(bench.compare(source, destination).passed, true);
  assert.deepEqual(bench.parseCSV('a,"b\r\nc"'), [['a', 'b\r\nc']]);
});

test('malformed quotes are fatal input issues, not silently repaired', () => {
  for (const source of ['id,result\na,"1', 'id,result\na,1"2', 'id,result\na,"1" x', 'id,result\na, "1"']) {
    assert.throws(() => bench.parseCSV(source), {code: 'csv_quotes'});
    const report = bench.compare(source, 'id,result\na,1');
    assert.equal(report.passed, false);
    assert.equal(report.outcome, 'invalid');
  }
});

test('an unreadable table produces unknown comparisons, not invented missing cases', () => {
  const report = bench.compare('id,result\na,"1', 'id,result\na,1\nb,2');
  assert.equal(report.passed, false);
  assert.equal(report.counts.referenceRows, null);
  assert.equal(report.counts.compared, 0);
  assert.equal(report.counts.missingReference, 0);
  assert.equal(report.counts.missingCandidate, 0);
  assert.equal(report.counts.unassessed, 2);
  assert.ok(report.rows.every(row => row.status === 'unassessed'));
});

test('duplicate IDs on either side invalidate the case instead of last-value-wins', () => {
  const duplicate = 'id,result\na,1\na,1';
  for (const inputs of [[duplicate, 'id,result\na,1'], ['id,result\na,1', duplicate], [duplicate, duplicate]]) {
    const report = bench.compare(...inputs);
    assert.equal(report.passed, false);
    assert.equal(report.rows[0].status, 'invalid');
    assert.equal(report.counts.compared, 0);
    assert.equal(report.counts.invalidCases, 1);
    assert.equal(report.counts.duplicateIDs, inputs.filter(input => input === duplicate).length);
  }
  assert.equal(bench.compare('id,result\n a ,1\na,1', 'id,result\na,1').passed, false);
});

test('a matching subset never conceals missing, malformed or invalid rows', () => {
  const reference = 'id,result\na,1\nb,2\nbad,NaN\n,3\nc,4,extra';
  const candidate = 'id,result\na,1\nbad,NaN';
  const report = bench.compare(reference, candidate);
  assert.equal(report.passed, false);
  assert.equal(report.counts.exactMatches, 1);
  assert.equal(report.counts.missingCandidate, 1);
  assert.equal(report.counts.invalidCases, 1);
  assert.equal(report.counts.invalidRows, 4);
  assert.equal(report.errors.length, 4);
});

test('header-only, empty, blank and wrong-header tables cannot agree', () => {
  for (const source of ['', 'id,result', 'ID,result\na,1', 'id,result,extra\na,1,2', 'id,result\n\n']) {
    const report = bench.compare(source, source);
    assert.equal(report.passed, false, JSON.stringify(source));
    assert.ok(report.errors.length > 0);
  }
});

test('case IDs are trimmed but remain case-sensitive and safely handled as Map keys', () => {
  assert.equal(bench.compare('id,result\n a ,1', 'id,result\na,1').passed, true);
  const distinct = bench.compare('id,result\na,1', 'id,result\nA,1');
  assert.equal(distinct.counts.missingReference, 1);
  assert.equal(distinct.counts.missingCandidate, 1);
  assert.equal(distinct.passed, false);
  const unusual = 'id,result\n__proto__,1\nconstructor,2\n<img src=x onerror=alert(1)>,3';
  const report = bench.compare(unusual, unusual);
  assert.equal(report.passed, true);
  assert.equal(report.counts.exactMatches, 3);
  assert.equal(report.rows[2].id, '<img src=x onerror=alert(1)>');
});

test('ID length and control characters are rejected with visible diagnostics', () => {
  const source = 'id,result\n' + 'x'.repeat(81) + ',1\n"line\ncase",2';
  const report = bench.compare(source, 'id,result\na,1');
  assert.equal(report.passed, false);
  assert.equal(report.counts.invalidRows, 2);
  assert.equal(report.errors.filter(error => error.code === 'case_id').length, 2);
});

test('200 cases are accepted and 201 are rejected', () => {
  const text = count => 'id,result\n' + Array.from({length: count}, (_, i) => 'case-' + i + ',' + i).join('\r\n') + '\r\n';
  const full = bench.compare(text(200), text(200));
  assert.equal(full.passed, true);
  assert.equal(full.counts.compared, 200);
  const overflow = bench.compare(text(201), text(200));
  assert.equal(overflow.passed, false);
  assert.ok(overflow.errors.some(error => error.code === 'case_limit'));
});

test('size limit is UTF-8 bytes, inclusive at exactly 64 KiB', () => {
  assert.equal(bench.parseCSV('a'.repeat(65536))[0][0].length, 65536);
  assert.throws(() => bench.parseCSV('a'.repeat(65537)), {code: 'size_limit'});
  assert.equal(bench.parseCSV('é'.repeat(32768))[0][0].length, 32768);
  assert.throws(() => bench.parseCSV('é'.repeat(32769)), {code: 'size_limit'});
  const report = bench.compare('x'.repeat(65537), 'id,result\na,1');
  assert.equal(report.outcome, 'invalid');
  assert.equal(report.passed, false);
});

test('serialized failure report preserves exact numbers and all diagnostic categories', () => {
  const source = 'id,result\na,-0.000000000001\na,-0.000000000001\nb,999999999999999999.999999999999';
  const target = 'id,result\nb,999999999999999999.999999999998\nc,5';
  const report = JSON.parse(JSON.stringify(bench.compare(source, target)));
  assert.equal(report.counts.duplicateIDs, 1);
  assert.equal(report.counts.missingReference, 1);
  assert.equal(report.counts.missingCandidate, 1);
  assert.equal(report.counts.differences, 1);
  assert.equal(report.rows.find(row => row.id === 'b').delta, '-0.000000000001');
  assert.equal(report.rows.find(row => row.id === 'b').reference, '999999999999999999.999999999999');
  assert.equal(report.passed, false);
});
