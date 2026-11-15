/* Formula Checkbench: bounded CSV comparison, without formula execution. */
(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.FormulaCheckbench = api;
  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', api.mount);
    else api.mount();
  }
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const MAX_BYTES = 65536;
  const MAX_CASES = 200;
  const SCALE = 1000000000000n;
  const LIMITS = Object.freeze({bytesPerSide: MAX_BYTES, casesPerSide: MAX_CASES, integerDigits: 18, fractionalDigits: 12, idCharacters: 80});
  class InputError extends Error {
    constructor(code, message) { super(message); this.name = 'InputError'; this.code = code; }
  }
  function byteLength(text) { return new TextEncoder().encode(text).length; }
  function parseDecimal(value) {
    if (typeof value !== 'string') throw new InputError('decimal_format', 'Use a plain decimal number.');
    const text = value.trim();
    if (!/^[+-]?\d{1,18}(?:\.\d{1,12})?$/.test(text)) {
      throw new InputError('decimal_format', 'Use a plain decimal: at most 18 integer and 12 fractional digits, with no exponent.');
    }
    const negative = text[0] === '-';
    const unsigned = /^[+-]/.test(text) ? text.slice(1) : text;
    const parts = unsigned.split('.');
    const units = BigInt(parts[0]) * SCALE + BigInt((parts[1] || '').padEnd(12, '0'));
    return negative ? -units : units;
  }
  function formatDecimal(units) {
    const negative = units < 0n;
    const magnitude = negative ? -units : units;
    const fraction = String(magnitude % SCALE).padStart(12, '0').replace(/0+$/, '');
    return (negative ? '-' : '') + String(magnitude / SCALE) + (fraction ? '.' + fraction : '');
  }
  function parseCSV(source) {
    if (typeof source !== 'string') throw new InputError('csv_type', 'Paste CSV text.');
    if (source.length > MAX_BYTES || byteLength(source) > MAX_BYTES) throw new InputError('size_limit', 'Each table must be no larger than 64 KiB of UTF-8 text.');
    const text = source.replace(/^\uFEFF/, '');
    if (text === '') return [];
    const rows = [];
    let row = [], field = '', mode = 'start', endedRow = false;
    function finishField() { row.push(field); field = ''; mode = 'start'; }
    function finishRow() {
      finishField(); rows.push(row); row = []; endedRow = true;
      if (rows.length > MAX_CASES + 1) throw new InputError('case_limit', 'Each table supports at most 200 case rows, plus the header.');
    }
    for (let i = 0; i < text.length; i += 1) {
      const ch = text[i]; endedRow = false;
      if (mode === 'quoted') {
        if (ch === '"') {
          if (text[i + 1] === '"') { field += '"'; i += 1; }
          else mode = 'closed';
        } else field += ch;
        continue;
      }
      if (ch === ',') { finishField(); continue; }
      if (ch === '\n' || ch === '\r') {
        if (ch === '\r' && text[i + 1] === '\n') i += 1;
        finishRow(); continue;
      }
      if (mode === 'closed') throw new InputError('csv_quotes', 'A closing quote must be followed by a comma or line ending.');
      if (ch === '"') {
        if (mode !== 'start') throw new InputError('csv_quotes', 'A quote inside an unquoted field is invalid; quote the whole field.');
        mode = 'quoted'; continue;
      }
      field += ch; mode = 'plain';
    }
    if (mode === 'quoted') throw new InputError('csv_quotes', 'A quoted field is missing its closing quote.');
    if (!endedRow) finishRow();
    return rows;
  }
  function parseTable(text, side) {
    const errors = [], entries = new Map(), duplicateIDs = new Set();
    let records;
    try { records = parseCSV(text); }
    catch (error) { return {entries, errors: [{side, row: null, code: error.code, message: error.message}], readable: false, rowCount: null, invalidRows: 0, duplicateIDs: []}; }
    if (!records.length || records[0].length !== 2 || records[0][0].trim() !== 'id' || records[0][1].trim() !== 'result') {
      errors.push({side, row: 1, code: 'header', message: 'Use exactly two column headers: id,result.'});
      return {entries, errors, readable: false, rowCount: Math.max(0, records.length - 1), invalidRows: 0, duplicateIDs: []};
    }
    let invalidRows = 0;
    records.slice(1).forEach((record, index) => {
      const row = index + 2;
      if (record.length !== 2) {
        errors.push({side, row, code: 'column_count', message: 'This row must contain exactly two CSV fields.'}); invalidRows += 1; return;
      }
      const id = record[0].trim(), raw = record[1].trim();
      if (!id || id.length > LIMITS.idCharacters || /[\u0000-\u001F\u007F]/.test(id)) {
        errors.push({side, row, code: 'case_id', message: 'Give each case a non-empty ID of at most 80 characters, without control characters.'}); invalidRows += 1; return;
      }
      let value = null;
      try { value = parseDecimal(raw); }
      catch (error) { errors.push({side, row, code: error.code, message: 'Case “' + id + '”: ' + error.message}); invalidRows += 1; }
      if (!entries.has(id)) entries.set(id, []);
      entries.get(id).push({id, raw, value, row});
      if (entries.get(id).length > 1) duplicateIDs.add(id);
    });
    for (const id of duplicateIDs) {
      errors.push({side, row: null, code: 'duplicate_id', message: 'Case “' + id + '” appears more than once. Every case ID must be unique per table.'});
    }
    if (records.length === 1) errors.push({side, row: null, code: 'empty_table', message: 'Add at least one case below the header.'});
    return {entries, errors, readable: true, rowCount: records.length - 1, invalidRows, duplicateIDs: [...duplicateIDs]};
  }
  function compare(referenceText, candidateText, toleranceText = '0') {
    const reference = parseTable(referenceText, 'reference'), candidate = parseTable(candidateText, 'candidate');
    const errors = [...reference.errors, ...candidate.errors];
    let tolerance = null;
    try {
      tolerance = parseDecimal(toleranceText);
      if (tolerance < 0n) throw new InputError('negative_tolerance', 'Tolerance must be zero or greater.');
    } catch (error) {
      tolerance = null; errors.push({side: 'tolerance', row: null, code: error.code, message: error.message});
    }
    const counts = {referenceRows: reference.rowCount, candidateRows: candidate.rowCount, compared: 0, exactMatches: 0, withinTolerance: 0,
      differences: 0, missingReference: 0, missingCandidate: 0, invalidCases: 0, unassessed: 0,
      invalidRows: reference.invalidRows + candidate.invalidRows, duplicateIDs: reference.duplicateIDs.length + candidate.duplicateIDs.length};
    const rows = [];
    const ids = new Set([...reference.entries.keys(), ...candidate.entries.keys()]);
    for (const id of ids) {
      const left = reference.entries.get(id), right = candidate.entries.get(id);
      const invalidLeft = left && (left.length !== 1 || left[0].value === null);
      const invalidRight = right && (right.length !== 1 || right[0].value === null);
      const item = {id, reference: left && left.length === 1 ? left[0].raw : null,
        candidate: right && right.length === 1 ? right[0].raw : null, delta: null, status: '', notes: []};
      if (!reference.readable || !candidate.readable) {
        item.status = 'unassessed'; counts.unassessed += 1;
        item.notes.push('A table could not be parsed. Case presence and agreement cannot be assessed.');
        rows.push(item); continue;
      }
      if (!left) { counts.missingReference += 1; item.notes.push('Missing from reference.'); }
      if (!right) { counts.missingCandidate += 1; item.notes.push('Missing from candidate.'); }
      if (invalidLeft || invalidRight) {
        counts.invalidCases += 1; item.status = 'invalid';
        if (invalidLeft) item.notes.push('Reference has an invalid value or duplicate ID.');
        if (invalidRight) item.notes.push('Candidate has an invalid value or duplicate ID.');
      } else if (!left || !right) item.status = !left ? 'missing_reference' : 'missing_candidate';
      else {
        const delta = right[0].value - left[0].value;
        item.delta = formatDecimal(delta);
        counts.compared += 1;
        if (tolerance === null) { item.status = 'unassessed'; counts.unassessed += 1; }
        else if (delta === 0n) { item.status = 'match'; counts.exactMatches += 1; }
        else if ((delta < 0n ? -delta : delta) <= tolerance) { item.status = 'within_tolerance'; counts.withinTolerance += 1; }
        else { item.status = 'difference'; counts.differences += 1; }
      }
      rows.push(item);
    }
    const passed = errors.length === 0 && counts.compared > 0 && counts.differences === 0 && counts.missingReference === 0 && counts.missingCandidate === 0 && counts.invalidCases === 0 && counts.unassessed === 0;
    return {schemaVersion: 1, tool: 'Sanctum Formula Checkbench',
      scope: 'Compares supplied numeric results by case ID. Does not execute or verify formulas, spreadsheets or expected values.',
      tolerance: tolerance === null ? null : formatDecimal(tolerance), toleranceRule: 'Absolute difference <= tolerance; exact decimal arithmetic.',
      limits: LIMITS, passed, outcome: errors.length ? 'invalid' : passed ? 'agree' : 'differences', counts, errors, rows};
  }
  const EXAMPLE = 'id,result\nzero,0.00\none,12.00\nbulk-discount,179.91\nhalf-cent,1.01\nbelow-half,1.00\nfree,0.00\nfractional-quantity,8.50\nfractional-price,1.00\n';
  const CHANGED_EXAMPLE = 'id,result\nzero,0.00\none,12.00\nbulk-discount,179.91\nhalf-cent,1.00\nbelow-half,1.00\nfractional-quantity,8.50\nfractional-price,1.00\nnew-case,2.00\n';
  function mount() {
    const root = document.querySelector('[data-checkbench]');
    if (!root || root.dataset.mounted) return;
    root.dataset.mounted = 'true';
    const byId = id => root.querySelector('#' + id);
    const form = byId('checkbench-form'), reference = byId('cb-reference'), candidate = byId('cb-candidate'), tolerance = byId('cb-tolerance');
    const result = byId('cb-results'), errorBox = byId('cb-errors'), status = byId('cb-status'), exportButton = byId('cb-export');
    let currentReport = null, snapshot = null;
    const currentInputs = () => [reference.value, candidate.value, tolerance.value];
    function element(tag, text, className) {
      const node = document.createElement(tag); if (text !== undefined) node.textContent = text;
      if (className) node.className = className; return node;
    }
    function refreshSize() {
      for (const [input, label] of [[reference, byId('cb-reference-size')], [candidate, byId('cb-candidate-size')]]) {
        label.textContent = input.value.length > MAX_BYTES ? 'Over the 64 KiB limit' : byteLength(input.value).toLocaleString() + ' / 65,536 bytes';
      }
    }
    function invalidate() {
      exportButton.disabled = true; refreshSize();
      if (currentReport) {
        result.classList.add('cb-stale'); byId('cb-stale').hidden = false;
        status.textContent = 'Inputs changed. Compare again for an up-to-date report.';
      }
      errorBox.replaceChildren(); errorBox.hidden = true;
      [reference, candidate, tolerance].forEach(input => input.removeAttribute('aria-invalid'));
    }
    [reference, candidate, tolerance].forEach(input => input.addEventListener('input', invalidate));
    const labels = {match: 'Exact match', within_tolerance: 'Within tolerance', difference: 'Difference',
      missing_reference: 'Missing reference', missing_candidate: 'Missing candidate', invalid: 'Invalid case', unassessed: 'Not assessed'};
    function render(report) {
      result.hidden = false; result.classList.remove('cb-stale'); byId('cb-stale').hidden = true;
      byId('cb-result-title').textContent = report.outcome === 'agree' ? 'Results agree.' : report.outcome === 'invalid' ? 'Check the inputs.' : 'Differences found.';
      byId('cb-result-summary').textContent = report.counts.compared + ' case pairs compared. Tolerance: ' + (report.tolerance === null ? 'invalid' : report.tolerance) + '. Agreement describes these supplied values only.';
      const metrics = byId('cb-metrics'); metrics.replaceChildren();
      for (const [value, label] of [[report.counts.exactMatches, 'Exact matches'], [report.counts.withinTolerance, 'Within tolerance'], [report.counts.differences, 'Different'], [report.counts.missingReference + report.counts.missingCandidate, 'Missing'], [report.errors.length, 'Input issues']]) {
        const item = element('div', undefined, 'cb-metric'); item.append(element('strong', String(value)), element('span', label)); metrics.append(item);
      }
      errorBox.replaceChildren(); errorBox.hidden = report.errors.length === 0;
      if (report.errors.length) {
        errorBox.append(element('h3', 'Resolve these input issues'));
        const list = element('ul');
        for (const error of report.errors) {
          const side = error.side[0].toUpperCase() + error.side.slice(1);
          list.append(element('li', side + (error.row ? ', CSV row ' + error.row : '') + ': ' + error.message));
          const input = {reference, candidate, tolerance}[error.side]; if (input) input.setAttribute('aria-invalid', 'true');
        }
        errorBox.append(list);
      }
      const tbody = byId('cb-result-rows'); tbody.replaceChildren();
      for (const row of report.rows) {
        const tr = element('tr'); tr.dataset.status = row.status;
        const name = element('th', row.id); name.scope = 'row'; tr.append(name);
        for (const value of [row.reference, row.candidate, row.delta]) tr.append(element('td', value === null ? '—' : value));
        const state = element('td'); state.append(element('span', labels[row.status], 'cb-case-state'));
        if (row.notes.length) state.append(element('small', row.notes.join(' ')));
        tr.append(state); tbody.append(tr);
      }
      byId('cb-table-wrap').hidden = report.rows.length === 0;
      status.textContent = byId('cb-result-title').textContent + ' ' + report.counts.compared + ' case pairs compared; ' + report.errors.length + ' input issues.';
      exportButton.disabled = false;
      (report.errors.length ? errorBox : byId('cb-result-title')).focus();
    }
    form.addEventListener('submit', event => {
      event.preventDefault();
      [reference, candidate, tolerance].forEach(input => input.removeAttribute('aria-invalid'));
      snapshot = currentInputs(); currentReport = {...compare(...snapshot), generatedAt: new Date().toISOString()}; render(currentReport);
    });
    function loadExample(changed) {
      reference.value = EXAMPLE; candidate.value = changed ? CHANGED_EXAMPLE : EXAMPLE; tolerance.value = '0'; invalidate();
      status.textContent = changed ? 'Synthetic example loaded: one changed value and one missing case on each side. Select Compare results.' : 'Eight matching synthetic cases loaded. Select Compare results.';
      reference.focus();
    }
    byId('cb-load-example').addEventListener('click', () => loadExample(false));
    byId('cb-load-changed').addEventListener('click', () => loadExample(true));
    byId('cb-reset').addEventListener('click', () => {
      reference.value = ''; candidate.value = ''; tolerance.value = '0'; currentReport = null; snapshot = null;
      result.hidden = true; result.classList.remove('cb-stale'); byId('cb-stale').hidden = true; byId('cb-result-rows').replaceChildren();
      byId('cb-metrics').replaceChildren(); byId('cb-result-title').textContent = ''; byId('cb-result-summary').textContent = '';
      invalidate(); status.textContent = 'Inputs and report cleared.'; reference.focus();
    });
    exportButton.addEventListener('click', () => {
      if (!currentReport || !snapshot || currentInputs().some((value, i) => value !== snapshot[i])) { invalidate(); return; }
      const blob = new Blob([JSON.stringify(currentReport, null, 2) + '\n'], {type: 'application/json'});
      const url = URL.createObjectURL(blob), anchor = document.createElement('a');
      anchor.href = url; anchor.download = 'sanctum-checkbench-report.json'; document.body.append(anchor); anchor.click(); anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
      status.textContent = 'Report exported. The file contains the case IDs and results you supplied.';
    });
    refreshSize(); byId('cb-compare').disabled = false;
  }
  return {LIMITS, InputError, parseDecimal, formatDecimal, parseCSV, parseTable, compare, EXAMPLE, CHANGED_EXAMPLE, mount};
}));
