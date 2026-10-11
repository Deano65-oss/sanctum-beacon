import pytest
from app.posting_holds import Evidence, HoldPolicyError, validate_evidence, expiry, effective

NOW = 200000
A = 'same-account'

def pair():
    return [Evidence('one', A, NOW - 86400, True), Evidence('two', A, NOW, True)]

def test_pair_window_and_inclusive_evidence_age():
    assert validate_evidence(A, pair(), NOW) == ('one', 'two')
    older = [Evidence('one', A, NOW-172800, True), Evidence('two', A, NOW-86400, True)]
    assert validate_evidence(A, older, NOW) == ('one', 'two')
    with pytest.raises(HoldPolicyError): validate_evidence(A, older, NOW+1)

@pytest.mark.parametrize('evidence', [
    [], [Evidence('one', A, NOW, True)],
    [Evidence('one', A, NOW, True)] * 2,
    [Evidence('one', 'other', NOW, True), Evidence('two', A, NOW, True)],
    [Evidence('one', A, NOW, False), Evidence('two', A, NOW, True)],
    [Evidence('one', A, NOW-86401, True), Evidence('two', A, NOW, True)],
    [Evidence('one', A, NOW, True), Evidence('two', A, NOW+1, True)]])
def test_nonqualifying_evidence(evidence):
    with pytest.raises(HoldPolicyError): validate_evidence(A, evidence, NOW)

def test_renewal_requires_fresh_evidence():
    with pytest.raises(HoldPolicyError): validate_evidence(A, pair(), NOW, ['one'])
    assert validate_evidence(A, pair(), NOW, ['different-prior-post'])

@pytest.mark.parametrize('duration', [0, -1, 86401, True, 1.5])
def test_duration_bound(duration):
    with pytest.raises(HoldPolicyError): expiry(NOW, duration)

def test_expiry_release_restoration_and_wrong_author():
    end = expiry(NOW, 86400)
    args = (A, ('one', 'two'), pair(), end)
    assert effective(*args, False, end-1)
    assert not effective(*args, False, end)
    assert not effective(*args, True, NOW)
    assert not effective(A, ('one', 'two'), pair()[:1], end, False, NOW)
    restored = [Evidence('one', A, NOW, False), pair()[1]]
    assert not effective(A, ('one', 'two'), restored, end, False, NOW)
    changed_author = [Evidence('one', 'other', NOW, True), pair()[1]]
    assert not effective(A, ('one', 'two'), changed_author, end, False, NOW)
