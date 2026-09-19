"""Build the native desktop shell and private engine as one Windows package."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import zipfile


ROOT = Path(__file__).resolve().parent.parent


def run(args):
    env = dict(os.environ)
    env.update(DOTNET_CLI_HOME=str(ROOT / '.tooling/dotnet-home'),
               NUGET_PACKAGES=str(ROOT / '.tooling/nuget'),
               DOTNET_SKIP_FIRST_TIME_EXPERIENCE='1', DOTNET_CLI_TELEMETRY_OPTOUT='1')
    subprocess.run([str(a) for a in args], cwd=ROOT, env=env, check=True)


def source_archive(destination):
    """Ship corresponding application source without profiles or development artifacts."""
    excluded = {'bin', 'obj', 'artifacts', '__pycache__', '.pytest_cache', '.git'}
    extensions = {'.py', '.xaml', '.cs', '.csproj', '.manifest', '.json', '.md', '.txt',
                  '.spec', '.ps1', '.ui', '.qrc', '.png', '.ico', '.svg', '.toml', '.cfg', '.ini'}
    roots = ['companion', 'desktop', 'packaging', 'tools', 'tests', 'src', 'docs',
             'vendor/springbok-putting/source']
    files = [p for p in ROOT.iterdir() if p.is_file() and
             (p.suffix in extensions or p.name in {'LICENSE', '.gitignore'})]
    for name in roots:
        directory = ROOT / name
        if directory.exists():
            files.extend(p for p in directory.rglob('*') if p.is_file() and
                         not any(part in excluded for part in p.relative_to(directory).parts) and
                         (p.suffix in extensions or p.name.startswith(('LICENSE', 'COPYING'))))
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(set(files)):
            archive.write(path, str(Path('MevoCompanion-source') / path.relative_to(ROOT)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-engine', action='store_true')
    parser.add_argument('--dotnet', default=str(ROOT / '.tooling/dotnet/dotnet.exe'))
    options = parser.parse_args()
    from fetch_springbok_putting import ensure_tracker
    ensure_tracker()
    if not options.skip_engine:
        run([sys.executable, '-m', 'PyInstaller', '--noconfirm', 'MevoCompanionEngine.spec'])
    project = ROOT / 'desktop/MevoCompanion/MevoCompanion.csproj'
    if not project.exists():
        raise SystemExit('The native desktop project is not present yet')
    package = ROOT / 'dist' / ('.package-' + uuid.uuid4().hex)
    package.mkdir(parents=True, exist_ok=False)
    run([options.dotnet, 'publish', project, '-c', 'Release', '-r', 'win-x64',
         '--self-contained', 'true', '-p:PublishSingleFile=true',
         '-p:IncludeNativeLibrariesForSelfExtract=true', '-o', package])
    reader = ROOT / 'desktop/FsGolfReader/FsGolfReader.csproj'
    if reader.exists():
        run([options.dotnet, 'publish', reader, '-c', 'Release', '-r', 'win-x64',
             '--self-contained', 'true', '-p:PublishSingleFile=true',
             '-p:IncludeNativeLibrariesForSelfExtract=true', '-o', package / 'reader'])
    shutil.copytree(ROOT / 'dist/MevoCompanionEngine', package / 'engine', dirs_exist_ok=True)
    shutil.copy2(ROOT / 'LICENSE', package / 'LICENSE.txt')
    for name in ('README-COMPANION.md', 'THIRD-PARTY-NOTICES.md'):
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, package / name)
    from collect_licenses import collect
    collect(package / 'licenses')
    source_archive(package / 'MevoCompanion-source.zip')
    archive = ROOT / 'dist/MevoCompanion-windows-x64.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(package.rglob('*')):
            if path.is_file() and path.suffix not in {'.pdb'}:
                output.write(path, str(Path('MevoCompanion') / path.relative_to(package)))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (archive.with_suffix('.sha256')).write_text(digest + '  ' + archive.name + '\n', encoding='utf-8')
    destination = (ROOT / 'dist/MevoCompanion').resolve()
    backup = (ROOT / 'artifacts' / ('previous-package-' + uuid.uuid4().hex)).resolve()
    for path in (package.resolve(), destination, backup):
        if not path.is_relative_to(ROOT.resolve()):
            raise RuntimeError('Build output must stay inside this repository')
    if destination.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        destination.rename(backup)
    package.rename(destination)
    print(json.dumps({'package': str(destination), 'zip': str(archive), 'sha256': digest}))


if __name__ == '__main__':
    main()
