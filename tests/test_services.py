"""Server-manager ownership tests. No real daemon is started or signalled."""
import importlib.util
import json
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def services(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / 'scripts/services.py'
    spec = importlib.util.spec_from_file_location('headline60_test_services', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'STATE', tmp_path / '.run')
    monkeypatch.setattr(module, 'LOGS', tmp_path / 'logs')
    monkeypatch.setattr(module, 'RUNNER', tmp_path / 'scripts/service_runner.py')
    module.STATE.mkdir()
    module.LOGS.mkdir()
    monkeypatch.setattr(module.time, 'sleep', lambda value: None)
    return module


def owned_record(module, role='api', pid=41, token='a' * 32):
    identity = {'started': 'Wed Oct 28 22:25:56 2026',
                'command': f"{module.ROOT / '.venv/bin/python'} {module.RUNNER} {role} --instance {token}"}
    return {'pid': pid, 'role': role, 'instance': token, 'identity': identity}


def save_record(module, record, role='api'):
    (module.STATE / f'{role}.json').write_text(json.dumps(record))


def test_ownership_requires_exact_command_and_start_time(services, monkeypatch):
    record = owned_record(services)
    monkeypatch.setattr(services, 'ps', lambda pid: record['identity'])
    assert services.owned(record, 'api')
    changed = {**record['identity'], 'started': 'Thu Oct 29 22:25:56 2026'}
    monkeypatch.setattr(services, 'ps', lambda pid: changed)
    assert not services.owned(record, 'api')
    # Merely mentioning the runner in an unrelated command is not ownership.
    changed = {**record['identity'], 'command': '/bin/echo ' + record['identity']['command']}
    monkeypatch.setattr(services, 'ps', lambda pid: changed)
    assert not services.owned({**record, 'identity': changed}, 'api')


@pytest.mark.parametrize('pid', [True, 0, -1, 1, '41'])
def test_invalid_pid_is_never_queried_or_signalled(services, monkeypatch, pid):
    monkeypatch.setattr(services, 'ps', lambda value: pytest.fail('invalid PID was queried'))
    assert not services.owned({**owned_record(services), 'pid': pid}, 'api')


@pytest.mark.parametrize('value', ['[]', '42', 'null', '{broken'])
def test_bad_pid_file_is_not_a_process_record(services, value):
    (services.STATE / 'api.json').write_text(value)
    assert services.load('api') is None


def test_stale_foreign_pid_never_receives_signal(services, monkeypatch):
    record = owned_record(services)
    save_record(services, record)
    monkeypatch.setattr(services, 'ps', lambda pid: {'started': record['identity']['started'], 'command': 'ollama serve'})
    monkeypatch.setattr(services.os, 'kill', lambda *args: pytest.fail('foreign PID was signalled'))
    monkeypatch.setattr(services, 'descendants', lambda pid: pytest.fail('foreign descendants were inspected'))
    services.stop_one('api')
    assert not (services.STATE / 'api.json').exists()


def test_duplicate_start_reuses_healthy_external_server(services, monkeypatch):
    monkeypatch.setattr(services, 'healthy', lambda role: True)
    monkeypatch.setattr(services.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('duplicate process created'))
    assert services.start_one('ollama') is False
    assert not (services.STATE / 'ollama.json').exists()


def test_owned_unhealthy_server_is_waited_for_not_duplicated(services, monkeypatch):
    record = owned_record(services)
    save_record(services, record)
    monkeypatch.setattr(services, 'healthy', lambda role: False)
    monkeypatch.setattr(services, 'ps', lambda pid: record['identity'])
    waited = []
    monkeypatch.setattr(services, 'wait_health', waited.append)
    monkeypatch.setattr(services.subprocess, 'Popen', lambda *a, **k: pytest.fail('duplicate process created'))
    assert services.start_one('api') is False
    assert waited == ['api']


def setup_fake_child(services, monkeypatch, valid=True):
    launched = {}

    class Child:
        pid = 53
        terminated = False
        killed = False
        def poll(self):
            return None
        def terminate(self):
            self.terminated = True
        def kill(self):
            self.killed = True
        def wait(self, timeout):
            return 0

    child = Child()
    def popen(args, **kwargs):
        launched.update(args=args, kwargs=kwargs)
        return child
    monkeypatch.setattr(services.subprocess, 'Popen', popen)
    monkeypatch.setattr(services, 'healthy', lambda role: False)
    def process(pid):
        command = ' '.join(launched['args']) if valid else '/unrelated/application'
        return {'started': 'Wed Oct 28 22:25:56 2026', 'command': command}
    monkeypatch.setattr(services, 'ps', process)
    return launched, child


def test_new_child_is_detached_and_ownership_recorded_before_health(services, monkeypatch):
    launched, _ = setup_fake_child(services, monkeypatch)
    def health(role):
        assert services.owned(services.load(role), role)
    monkeypatch.setattr(services, 'wait_health', health)
    assert services.start_one('api')
    assert launched['kwargs']['start_new_session'] is True
    assert launched['kwargs']['stdin'] == services.subprocess.DEVNULL
    assert services.load('api')['pid'] == 53


def test_failed_health_cleans_only_just_created_service(services, monkeypatch):
    setup_fake_child(services, monkeypatch)
    def fail(role):
        raise RuntimeError('health failed')
    monkeypatch.setattr(services, 'wait_health', fail)
    stopped = []
    monkeypatch.setattr(services, 'stop_one', stopped.append)
    with pytest.raises(RuntimeError, match='health failed'):
        services.start_one('api')
    assert stopped == ['api']


def test_unverified_new_child_uses_creation_handle_for_cleanup(services, monkeypatch):
    _, child = setup_fake_child(services, monkeypatch, valid=False)
    monkeypatch.setattr(services.os, 'kill', lambda *args: pytest.fail('unverified PID was signalled'))
    with pytest.raises(RuntimeError, match='실행 명령'):
        services.start_one('api')
    assert child.terminated
    assert not (services.STATE / 'api.json').exists()


def test_stop_checks_child_identity_and_escalates_only_owned_child(services, monkeypatch):
    record = owned_record(services)
    save_record(services, record)
    child_identity = {'started': 'Wed Oct 28 22:26:00 2026', 'command': 'owned tts worker'}
    old_identity = {'started': 'Wed Oct 28 22:26:00 2026', 'command': 'old child'}
    actual = {41: record['identity'], 42: child_identity,
              43: {'started': 'Thu Oct 29 22:26:00 2026', 'command': 'foreign reused pid'}}
    monkeypatch.setattr(services, 'ps', actual.get)
    monkeypatch.setattr(services, 'descendants', lambda pid: [(42, child_identity), (43, old_identity)])
    clock = iter(range(100))
    monkeypatch.setattr(services.time, 'monotonic', lambda: next(clock))
    signals = []
    def kill(pid, sig):
        signals.append((pid, sig))
        if pid == 41 or sig == signal.SIGKILL:
            actual.pop(pid, None)
    monkeypatch.setattr(services.os, 'kill', kill)
    services.stop_one('api')
    assert signals == [(41, signal.SIGTERM), (42, signal.SIGTERM), (42, signal.SIGKILL)]
    assert 43 in actual
    assert not (services.STATE / 'api.json').exists()


def test_process_exits_between_identity_check_and_term(services, monkeypatch):
    record = owned_record(services)
    save_record(services, record)
    monkeypatch.setattr(services, 'ps', lambda pid: record['identity'])
    monkeypatch.setattr(services, 'descendants', lambda pid: [])
    def disappeared(pid, sig):
        raise ProcessLookupError
    monkeypatch.setattr(services.os, 'kill', disappeared)
    services.stop_one('api')
    assert not (services.STATE / 'api.json').exists()


def test_status_distinguishes_external_healthy_and_owned_unhealthy(services, monkeypatch):
    record = owned_record(services)
    save_record(services, record)
    monkeypatch.setattr(services, 'ps', lambda pid: record['identity'])
    monkeypatch.setattr(services, 'healthy', lambda role: role == 'ollama')
    result = services.status()
    assert result['ollama'] == {'healthy': True, 'owned': False, 'pid': None, 'stale_record': False}
    assert result['api']['owned'] and not result['api']['healthy']


def test_health_rejects_json_array_without_crashing(services, monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, 'get', lambda *args, **kwargs: SimpleNamespace(status_code=200, json=list))
    assert not services.healthy('api')
    assert not services.healthy('ollama')


def test_wait_health_reports_missing_log_and_lost_owner(services, monkeypatch):
    monkeypatch.setattr(services, 'healthy', lambda role: False)
    monkeypatch.setattr(services, 'owned', lambda record, role: False)
    with pytest.raises(RuntimeError, match='로그 파일이 없습니다'):
        services.wait_health('api')


def test_logs_tail_does_not_start_or_stop_services(services, monkeypatch, capsys):
    (services.LOGS / 'api.log').write_text('old line\nrecent line\nlast line\n')
    monkeypatch.setattr(services.sys, 'argv', ['services.py', 'logs', '--lines', '2'])
    monkeypatch.setattr(services, 'start_one', lambda role: pytest.fail('logs started a service'))
    monkeypatch.setattr(services, 'stop_one', lambda role: pytest.fail('logs stopped a service'))
    assert services.main() == 0
    output = capsys.readouterr().out
    assert 'old line' not in output and 'recent line' in output and 'last line' in output


def test_start_rollback_does_not_stop_external_ollama(services, monkeypatch):
    monkeypatch.setattr(services.sys, 'argv', ['services.py', 'start', '--no-browser'])
    def start(role):
        if role == 'ollama':
            return False  # External server reused.
        raise RuntimeError('api failed')
    monkeypatch.setattr(services, 'start_one', start)
    stopped = []
    monkeypatch.setattr(services, 'stop_one', stopped.append)
    assert services.main() == 1
    assert stopped == []


@pytest.mark.parametrize('token', [None, 1, [], {}])
def test_malformed_instance_marker_is_rejected(services, token):
    assert not services.owned({**owned_record(services), 'instance': token}, 'api')
