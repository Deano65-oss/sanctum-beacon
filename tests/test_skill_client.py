import importlib.util
import json
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import create_app

spec = importlib.util.spec_from_file_location('skill_client', Path(__file__).parents[1] / 'skills/sanctum-community/scripts/sanctum.py')
skill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill)


def test_real_api_roundtrip_and_persistent_receipt(tmp_path, monkeypatch, capsys):
    base = 'http://127.0.0.1:8000'
    app = create_app('sqlite:///' + str(tmp_path / 'db'), base, 'test-operator-secret-' * 3)
    monkeypatch.setattr(skill.httpx, 'Client', lambda **kw: TestClient(app, base_url=base, follow_redirects=False))
    skill.main(['--base-url', base, '--discover'])
    assert 'open_tasks' in json.loads(capsys.readouterr().out)
    key = tmp_path / 'agent.pem'
    args = ['--base-url', base, '--key-file', str(key), '--name', 'Local test']
    answer = 'I can explain safe retries for a network request.'
    skill.main(args + ['--join', '--answer', answer])
    first = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    identity = first[0]['agent_id']
    receipt = first[-1]['receipt']
    assert key.stat().st_mode & 0o777 == 0o600
    skill.main(args + ['--answer', answer, '--starter'])
    again = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert again[0]['agent_id'] == identity
    assert again[-1]['receipt'] == receipt
    assert 'access_token' not in str(again)
    app.state.engine.dispose()
