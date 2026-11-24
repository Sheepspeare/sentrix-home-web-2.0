"""One build-only deployment on server100; credentials are provided separately."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

BASE = Path('/home/realmagic/xy/benchmarks/atm-bench/experiments/qwen38-flash-20261124')
DATA = Path('/var/tmp/sentrix-qwen38-flash-20261124/product-data')
PYTHON = '/home/realmagic/xy/releases/psh-b2030e1-100-20261117/.venv/bin/python'
PRODUCT_UNIT = 'sentrix-atm100-doubao-api.service'
EVAL_UNIT = 'sentrix-atm100-doubao-photobench.service'
GATEWAY_UNIT = 'sentrix-atm100-qwen38-flash-gateway.service'
ENDPOINT = 'http://127.0.0.1:18138/v1'
MODEL = 'qwen3.8-flash'
ALBUM = '2026.11.17-atm-sources-addresses'
SOURCE = Path('/home/realmagic/xy/benchmarks/atm-bench/datasets/sources') / ALBUM
PROTECTED = ['sentrix-atm100-api.service', 'sentrix-atm100-photobench.service',
             'sentrix-atm100-model.service', 'sentrix-atm100-wemm.service',
             'sentrix2-api.service', 'sentrix2-photobench.service', 'sentrix-sensevoice.service']


def call(port, endpoint, payload=None, timeout=30):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(f'http://127.0.0.1:{port}{endpoint}', data=data,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)


def systemctl(*args):
    return subprocess.check_output(['systemctl', '--user', *args], text=True).strip()


def processes():
    return {unit: systemctl('show', unit, '-p', 'MainPID', '--value') for unit in PROTECTED}


def process_env(unit):
    pid = systemctl('show', unit, '-p', 'MainPID', '--value')
    assert int(pid) > 1, unit
    return dict(item.decode().split('=', 1) for item in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
                if b'=' in item)


def envfile(path, values):
    path.write_text(''.join(f'{k}={json.dumps(str(v), ensure_ascii=False)}\n' for k, v in sorted(values.items())))
    path.chmod(0o600)


def safe_env(values):
    prefixes = ('SENTRIX_', 'PHOTOBENCH_', 'BENCH_', 'FACE_', 'RETINAFACE_', 'HF_', 'TRANSFORMERS_',
                'OMP_', 'MKL_', 'OPENBLAS_', 'NUMEXPR_', 'CUDA_', 'LD_')
    return {k: v for k, v in values.items() if k.startswith(prefixes)}


def assert_idle():
    for port in [8872, 8874]:
        assert not call(port, '/api/v1/active-work').get('work'), f'{port} is busy'


def history_inventory(root):
    return {str(p.relative_to(root)): [p.stat().st_size, p.stat().st_mtime_ns]
            for p in root.rglob('*') if p.is_file() and p.name in ['run.json', 'results.jsonl']}


def prepare():
    assert_idle()
    assert not (BASE / 'prepare-intent.json').exists(), 'Inspect previous outcome before applying again'
    assert shutil.disk_usage('/var/tmp').free > 25 * 1024**3
    assert not DATA.exists(), 'New build requires a fresh data root'
    assert (BASE / 'secrets.env').is_file()
    manifest = json.loads((SOURCE / 'manifest.json').read_text())
    assert len(manifest['photos']) == 3759
    assert len(manifest['videos']) == 533
    assert len(manifest['documents']) == 6742
    old_product = process_env(PRODUCT_UNIT)
    old_eval = process_env(EVAL_UNIT)
    old_config = Path(old_eval['PHOTOBENCH_CONFIG_ROOT'])
    backup = BASE / 'backup'
    backup.mkdir(mode=0o700)
    save(BASE / 'prepare-intent.json', {'created_at': time.time(), 'protected_pids': processes(),
        'source_manifest_sha256': hashlib.sha256((SOURCE / 'manifest.json').read_bytes()).hexdigest(),
        'old_data_dir': old_product.get('SENTRIX_DATA_DIR'), 'old_config_root': str(old_config)})
    shutil.copytree(old_config, backup / 'config', symlinks=True)
    config = BASE / 'config'
    shutil.copytree(old_config, config, symlinks=False)
    connection = call(8874, '/api/v1/config')['runtime_config']
    for key in list(connection):
        if 'api_key' in key:
            del connection[key]
    connection['model_base_url'] = ENDPOINT
    connection['endpoint_model'] = MODEL
    connection['vllm_manager_url'] = ''
    connection['product_build_revision'] = json.loads((BASE / 'release/build-info.json').read_text())['build_revision']
    connection['product_settings']['vlm_context_tokens'] = 131072
    connection['product_settings']['vlm_generation_profile'] = 'legacy'
    connection['product_settings']['vlm_timeout_s'] = 180
    save(config / 'runtime_connection.json', connection)
    target = {'label': '100 · Qwen3.8 Flash · 4 concurrent', 'manager_url': '', 'model_base_url': ENDPOINT}
    # Keep the existing target schema and replace its sole deployment target.
    target_path = config / 'vllm_targets.json'
    if target_path.exists():
        targets = json.loads(target_path.read_text())
        save(backup / 'vllm-targets-schema.json', targets)
        if isinstance(targets.get('targets'), dict):
            targets['targets'] = {'atm100-qwen38-flash': target}
            for name in ['default', 'default_target', 'default_target_id']:
                if name in targets:
                    targets[name] = 'atm100-qwen38-flash'
            save(target_path, targets)
    DATA.mkdir(parents=True, mode=0o700)
    old_data = Path(old_product['SENTRIX_DATA_DIR'])
    if (old_data / 'geo').exists():
        (DATA / 'geo').symlink_to(old_data / 'geo', target_is_directory=True)
    instance = Path('/home/realmagic/xy/benchmarks/atm-bench/instances/qwen38-flash')
    instance.mkdir()
    (instance / 'product-data').symlink_to(DATA, target_is_directory=True)
    role = {'base_url': ENDPOINT, 'model': MODEL, 'api_key_env': 'QWEN_API_KEY',
            'context_tokens': 131072, 'max_output_tokens': 4096, 'timeout_s': 180,
            'payload': {'allow_id_slot': False, 'allow_chat_template_kwargs': False,
                        'supports_tools': True, 'supports_json_schema': True,
                        'supports_json_object': True, 'temperature': 0},
            'retry': {'max_attempts': 3}}
    profile = {'profile': 'cloud', 'roles': {'chat': role, 'vision': {**role, 'supports_vision': True}},
               'vision': {'image_max_side': 1280}}
    save(BASE / 'model-profile.json', profile)
    product = safe_env(old_product)
    product.update({'SENTRIX_DATA_DIR': str(DATA), 'SENTRIX_LLM_PROFILE': 'cloud',
        'SENTRIX_LLM_CONFIG': str(BASE / 'model-profile.json'), 'SENTRIX_VLM_BASE_URL': ENDPOINT,
        'SENTRIX_VLM_MODEL': MODEL, 'SENTRIX_VLM_CONTEXT_TOKENS': '131072',
        'SENTRIX_VLM_REASONING_CONTEXT_TOKENS': '131072', 'SENTRIX_VLM_GENERATION_PROFILE': 'legacy',
        'SENTRIX_IMPORT_WORKERS': '4', 'SENTRIX_ALBUM_WORKERS': '4',
        'SENTRIX_VLM_TIMEOUT_S': '180', 'PHOTOBENCH_CONFIG_ROOT': str(config),
        'SENTRIX_UTILITY_VLM_BASE_URL': '', 'SENTRIX_QWEN_ASR_BASE_URL': ''})
    envfile(BASE / 'product.env', product)
    evaluation = safe_env(old_eval)
    evaluation.update({'PHOTOBENCH_CONFIG_ROOT': str(config), 'BENCH_VLLM_BASE_URL': ENDPOINT,
        'BENCH_BIG_MODEL_ENABLED': '0', 'BENCH_BIG_MODEL_MODEL': MODEL,
        'BENCH_BIG_MODEL_BASE_URL': ENDPOINT, 'PHOTOBENCH_SENTRIX_REPO_ROOT': str(BASE / 'release'),
        'PHOTOBENCH_SENTRIX_SERVICE_UNIT': PRODUCT_UNIT})
    envfile(BASE / 'evaluation.env', evaluation)
    gateway = {'FLASH_GATEWAY_CONFIG': str(BASE / 'gateway-config.json'),
               'FLASH_REQUEST_LOG': str(BASE / 'model-requests.jsonl')}
    envfile(BASE / 'gateway.env', gateway)
    # The upstream URL contains no credential and is staged separately.
    assert (BASE / 'gateway-config.json').exists()
    units = Path.home() / '.config/systemd/user'
    for unit, filename, module, appdir, port in [
        (PRODUCT_UNIT, 'product.env', 'sentrix', BASE / 'release/apps/api/src', 8294),
        (EVAL_UNIT, 'evaluation.env', 'photobench', BASE / 'evaluation-source', 8874),
    ]:
        dropdir = units / (unit + '.d')
        drop = dropdir / '99-qwen38-flash-build.conf'
        assert not drop.exists()
        dropdir.mkdir(exist_ok=True)
        text = (f'[Service]\nEnvironmentFile={BASE / filename}\nEnvironmentFile={BASE / "secrets.env"}\n'
                f'Environment=PHOTOBENCH_CONFIG_ROOT={config}\nEnvironment=SENTRIX_VLM_BASE_URL={ENDPOINT}\n'
                f'WorkingDirectory={BASE / "release"}\nExecStart=\nExecStart={PYTHON} -m uvicorn '
                f'--factory {module}.main:create_app --host 0.0.0.0 --port {port} --app-dir {appdir}\n')
        drop.write_text(text)
        drop.chmod(0o600)
    unit = units / GATEWAY_UNIT
    assert not unit.exists()
    unit.write_text(f'[Unit]\nDescription=ATM Qwen3.8 Flash loopback adapter\n[Service]\n'
        f'WorkingDirectory={BASE}\nEnvironmentFile={BASE / "gateway.env"}\n'
        f'EnvironmentFile={BASE / "secrets.env"}\nExecStart={PYTHON} -m uvicorn --factory gateway:create_app '
        f'--host 127.0.0.1 --port 18108 --app-dir {BASE}\nRestart=on-failure\nRestartSec=5\n')
    systemctl('daemon-reload')
    systemctl('start', GATEWAY_UNIT)
    systemctl('restart', PRODUCT_UNIT)
    systemctl('restart', EVAL_UNIT)
    save(BASE / 'prepared.json', {'created_at': time.time(), 'data_dir': str(DATA), 'model': MODEL})
    print(json.dumps({'prepared': True, 'model': MODEL, 'data_dir': str(DATA)}))


def start():
    assert_idle()
    assert not (BASE / 'launch-intent.json').exists(), 'Inspect accepted run; do not replay launch'
    assert processes() == json.loads((BASE / 'prepare-intent.json').read_text())['protected_pids']
    diagnostic = call(8294, '/api/v1/diagnostics')
    effective = diagnostic['runtime_configuration']
    assert effective['vlm_model'] == MODEL and effective['llm_profile'] == 'cloud'
    assert effective['album_workers'] == 4
    pid = systemctl('show', PRODUCT_UNIT, '-p', 'MainPID', '--value')
    assert process_env(PRODUCT_UNIT)['SENTRIX_IMPORT_WORKERS'] == '4'
    assert call(18138, '/health')['model'] == MODEL
    assert call(8294, '/api/v1/health')['embeddings'] == 'ok'
    payload = {'album_id': ALBUM, 'mode': 'build', 'models': [MODEL], 'graph_enabled': True,
               'reasoning': True, 'runtime_framework': 'generic', 'model_base_url': ENDPOINT,
               'endpoint_model': MODEL, 'vllm_manager_url': '', 'sentrix_url': 'http://127.0.0.1:8294',
               'import_timeout': 86400, 'pipeline_timeout': 172800}
    save(BASE / 'launch-intent.json', {'created_at': time.time(), 'request': payload, 'product_pid': pid})
    receipt = call(8874, '/api/v1/runs', payload, timeout=120)
    save(BASE / 'launch-receipt.json', receipt)
    print(json.dumps(receipt, ensure_ascii=False))


def status():
    receipt_path = BASE / 'launch-receipt.json'
    value = {'protected_pids': processes(), 'gateway': call(18138, '/health')}
    if receipt_path.exists():
        run = json.loads(receipt_path.read_text())['run_ids'][0]
        data = call(8874, f'/api/v1/runs/{run}', timeout=60)
        value['run'] = {k: data.get(k) for k in ['run_id', 'status', 'current_phase', 'scope_id',
             'mode', 'qa_count', 'scope_build', 'phases', 'error', 'started_at', 'finished_at']}
        value['item_count'] = len(data.get('items') or [])
    print(json.dumps(value, ensure_ascii=False))


def repair_prelaunch():
    """Correct this operation's occupied port and shadowed drop-in before launch."""
    assert_idle()
    assert (BASE / 'prepared.json').exists()
    assert not (BASE / 'launch-intent.json').exists()
    assert not (BASE / 'prelaunch-repair.json').exists()
    for name in ['product.env', 'evaluation.env', 'model-profile.json', 'config/runtime_connection.json']:
        path = BASE / name
        text = path.read_text().replace('127.0.0.1:18108', '127.0.0.1:18138')
        path.write_text(text)
        path.chmod(0o600)
    build = BASE / 'release/build-info.json'
    value = json.loads(build.read_text())
    value['revision'] = value['build_revision']
    save(build, value)
    targets = BASE / 'config/vllm_targets.json'
    targets.write_text(targets.read_text().replace('127.0.0.1:18108', '127.0.0.1:18138'))
    units = Path.home() / '.config/systemd/user'
    for unit in [PRODUCT_UNIT, EVAL_UNIT]:
        dropdir = units / (unit + '.d')
        old = dropdir / '99-qwen38-flash-build.conf'
        new = dropdir / ('z' * 40 + '-qwen38-flash-build.conf')
        old.write_text(old.read_text().replace('127.0.0.1:18108', '127.0.0.1:18138'))
        assert sorted([p.name for p in dropdir.glob('*.conf')] + [new.name])[-1] == new.name
        old.rename(new)
    gateway = units / GATEWAY_UNIT
    gateway.write_text(gateway.read_text().replace('--port 18108', '--port 18138'))
    systemctl('daemon-reload')
    systemctl('start', GATEWAY_UNIT)
    systemctl('restart', PRODUCT_UNIT)
    systemctl('restart', EVAL_UNIT)
    save(BASE / 'prelaunch-repair.json', {'at': time.time(), 'endpoint': ENDPOINT,
                                       'reason': 'occupied port and later historical drop-ins'})
    print(json.dumps({'repaired_before_launch': True, 'endpoint': ENDPOINT}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'start', 'status', 'repair-prelaunch'])
    args = parser.parse_args()
    {'prepare': prepare, 'start': start, 'status': status, 'repair-prelaunch': repair_prelaunch}[args.action]()
