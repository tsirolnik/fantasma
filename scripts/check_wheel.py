"""Install a built wheel into a temporary directory and exercise the real CLI.

Uses dependencies already installed in the invoking Python environment. No
model credentials, Docker, browser downloads, or network targets are needed.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel', type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        for asset in ('profiles/interactive.toml', 'profiles/lab.toml', 'web/frontend.html', 'web/interactive.html'):
            if asset not in names:
                raise SystemExit(f'Missing wheel asset: {asset}')
        if any(name.startswith(('data/', 'tests/', 'benchmark/')) or name.endswith('.env') for name in names):
            raise SystemExit('Unexpected private or development data in wheel')
    with tempfile.TemporaryDirectory(prefix='fantasma-wheel-') as temp:
        root = Path(temp)
        installed = root / 'installed'
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', '--target', str(installed), str(wheel)], check=True)
        env = {**os.environ, 'PYTHONPATH': str(installed)}
        cli = installed / 'bin' / 'fantasma'
        def run(argv, **kwargs):
            return subprocess.run(argv, cwd=root, env=env, text=True, capture_output=True, check=True, **kwargs)
        run([sys.executable, '-c', 'import main, profiles, web, os; from pathlib import Path; base=Path(os.environ["PYTHONPATH"]); assert all(Path(m.__file__).is_relative_to(base) for m in (main,profiles,web))'])
        print(run([str(cli), '--version']).stdout.strip())
        result = run([str(cli), 'profiles', 'inspect', 'interactive'])
        if json.loads(result.stdout)['profile']['name'] != 'interactive':
            raise SystemExit('Bundled profile did not resolve')
        result = run([str(cli), 'full', '--dry-run', '--no-browser'], input='/quit\n')
        if 'Session closed.' not in result.stdout:
            raise SystemExit('Interactive CLI did not close cleanly')
    print('Wheel CLI, module origins, bundled assets and dry-run verified outside checkout.')


if __name__ == '__main__':
    main()
