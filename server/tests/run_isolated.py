#!/usr/bin/env python3
"""Copy only source into a disposable installation before backend test imports."""
import argparse, os, pathlib, shutil, subprocess, tempfile, json
p=argparse.ArgumentParser();p.add_argument('--name',default='check');p.add_argument('--cwd',default='server');p.add_argument('command',nargs=argparse.REMAINDER);a=p.parse_args()
source=pathlib.Path(__file__).resolve().parents[2]
root=pathlib.Path(tempfile.mkdtemp(prefix='streamhome-'+a.name+'-'))
allowed={'.py','.sh','.json','.ts','.md','.txt','.toml','.yml','.yaml','.mjs','.tsx','.css','.html','.svg','.png','.jpg','.jpeg','.webp','.woff2','.ico'}
files=set(subprocess.check_output(['git','ls-files'],cwd=source,text=True).splitlines())
files.update(str(f.relative_to(source)) for f in (source/'server').rglob('*.py') if f.relative_to(source/'server').parts[0] not in {'venv','temp','media','logs','.run'} and '__pycache__' not in f.parts)
files.update(str(f.relative_to(source)) for f in (source/'web').rglob('*') if f.suffix in {'.ts','.tsx','.mjs','.css','.html','.svg','.png','.jpg','.jpeg','.webp','.woff2','.ico'} and not any(part in {'node_modules','dist','test-results','playwright-report'} for part in f.relative_to(source/'web').parts))
files.update(str(f.relative_to(source)) for f in (source/'server/tests').rglob('*.mjs'))
files.update(str(f.relative_to(source)) for f in (source/'scripts').rglob('*') if f.suffix in {'.py','.sh'} and f.is_file() and not f.is_symlink())
for name in files:
 f=source/name
 if f.suffix not in allowed or name.startswith(('memory-bank/','.agents/','artifacts/')) or not f.is_file() or f.is_symlink():continue
 dest=root/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(f,dest)
(root/'venv').symlink_to(source/'venv',target_is_directory=True)
(root/'web').mkdir(exist_ok=True)
(root/'web/node_modules').symlink_to(source/'web/node_modules',target_is_directory=True)
(root/'bin').mkdir(exist_ok=True)
for name in ('ffmpeg','ffprobe','rclone'):
 executable=shutil.which(name,path=str(source/'bin')+os.pathsep+os.environ.get('PATH',''))
 if executable:(root/'bin'/name).symlink_to(executable)
# No live environment, credentials, database, runtime state, media or diagnostics copied.
env={k:v for k,v in os.environ.items() if k in {'PATH','HOME','LANG','LC_ALL','TMPDIR'}}
(root/'test-home').mkdir()
(root/'temp').mkdir()
env['HOME']=str(root/'test-home')
env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(root/'server'), JWT_SECRET='isolated-test-secret-not-production', SECRET_ENCRYPTION_KEY='AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=', SETUP='false', STORAGE_ENGINE='LOCAL', STREAMHOME_INSTANCE_ROOT=str(root), PUBLIC_URL='http://localhost:3000')
command=a.command[1:] if a.command and a.command[0]=='--' else a.command
if not command: print(root);raise SystemExit()
command=[str(root/'venv/bin/python') if x=='PYTHON' else x for x in command]
print('ISOLATED_ROOT='+str(root),flush=True)
result=subprocess.run(command,cwd=root/a.cwd,env=env)
(root/'test-result.json').write_text(json.dumps({'command':command,'cwd':a.cwd,'exit_code':result.returncode}))
raise SystemExit(result.returncode)
