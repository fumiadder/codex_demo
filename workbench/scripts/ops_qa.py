#!/usr/bin/env python3
"""Run isolated Docker deployment/backup/restore/rollback drills with fake data.

Requires the managed local Docker socket. Never runs against a configured server.
Each test uses a unique Compose project, new volumes and a temporary directory.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args, check=True, quiet=False):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if check and result.returncode:
        raise RuntimeError(result.stdout[-6000:])
    if not quiet:
        print(result.stdout, end='')
    return result


def docker(*args, **kw):
    return run('docker', '--host=unix:///var/run/docker.sock', *args, **kw)


def copy_release(path, sha, fail=False):
    path.mkdir(parents=True)
    for name in ('server.py', 'bedtime.py', 'persistence.py', 'media_storage.py', 'storage_jobs.py', 'requirements.txt', 'stories_data.json', 'Dockerfile', 'compose.yaml', 'Caddyfile', '.dockerignore'):
        if (ROOT/name).exists(): shutil.copy2(ROOT/name, path/name)
    for name in ('public', 'scripts'):
        shutil.copytree(ROOT/name, path/name, ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    (path/'SOURCE_SHA').write_text(sha+'\n')
    if fail:
        dockerfile=path/'Dockerfile'
        dockerfile.write_text(dockerfile.read_text().replace('CMD ["python", "server.py"]', 'CMD ["python", "-c", "import time; time.sleep(300)"]'))


def container(project):
    return docker('ps','-aq','--filter',f'label=com.docker.compose.project={project}','--filter','label=com.docker.compose.service=app',quiet=True).stdout.strip()


def execute(project, script):
    return json.loads(docker('exec','-i',container(project),'python','-c',script,quiet=True).stdout)


SEED = r'''
import hashlib,json,pathlib,sqlite3
from server import now_iso
p=pathlib.Path('/data'); data=b'fake QA upload\x00\x01'; (p/'uploads'/('4'*32)).write_bytes(data)
c=sqlite3.connect(p/'folio.sqlite3');c.execute('PRAGMA foreign_keys=ON')
c.execute('INSERT INTO users VALUES(?,?,?,?,?)',('1'*32,'qa@example.test','QA','not-a-login-hash',now_iso()))
c.execute('INSERT INTO spaces VALUES(?,?,?,?)',('2'*32,'QA','1'*32,now_iso()))
c.execute('INSERT INTO members VALUES(?,?,?)',('2'*32,'1'*32,'owner'))
c.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('3'*32,'2'*32,'media','life','QA upload','','ready','{}','1'*32,now_iso(),now_iso()))
c.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?)',('4'*32,'2'*32,'3'*32,'qa.bin','application/octet-stream',len(data),now_iso()))
audio=b'fake QA cached narration'; (p/'bedtime-audio'/('5'*32)).write_bytes(audio)
c.execute('INSERT INTO bedtime_audio VALUES(?,?,?,?,?)',('5'*32,'2'*32,'1'*32,len(audio),int(__import__('time').time())));c.commit();c.close()
print(json.dumps({'seeded':True}))
'''.replace('?,?,?,?,?,?,?,?,?,?,?,?', '?,?,?,?,?,?,?,?,?,?,?')
CHECK = r'''
import hashlib,json,pathlib,sqlite3
p=pathlib.Path('/data');c=sqlite3.connect(p/'folio.sqlite3')
print(json.dumps({'users':c.execute('SELECT count(*) FROM users').fetchone()[0],'titles':[x[0] for x in c.execute('SELECT title FROM items ORDER BY id')],'upload':hashlib.sha256((p/'uploads'/('4'*32)).read_bytes()).hexdigest(),'audio':hashlib.sha256((p/'bedtime-audio'/('5'*32)).read_bytes()).hexdigest()}))
'''


def main():
    token='opsqa'+os.urandom(4).hex()
    os.environ['DOCKER_HOST']='unix:///var/run/docker.sock'
    for key in ('DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'):
        os.environ.pop(key,None)
    project=token
    restored=token+'_restored'
    with tempfile.TemporaryDirectory(prefix='zhixu-opsqa-') as temporary:
        deployment=Path(temporary)
        # Leave Docker registry credentials/config unchanged; only isolate buildx metadata.
        os.environ['BUILDX_CONFIG']=str(deployment/'buildx')
        shared=deployment/'shared';shared.mkdir()
        (shared/'.env').write_text(f'COMPOSE_PROJECT_NAME={project}\nWORKSPACE_DATA_VOLUME={project}_data\nWORKSPACE_DOMAIN=qa.example.test\n')
        releases=deployment/'releases'
        first,second,bad=(releases/('a'*40),releases/('b'*40),releases/('c'*40))
        copy_release(first,'a'*40);copy_release(second,'b'*40);copy_release(bad,'c'*40,True)
        process=None
        try:
            run('bash',str(first/'scripts/deploy.sh'),'--root',temporary,'--sha','a'*40,'--app-only')
            assert execute(project,SEED)['seeded']
            before=execute(project,CHECK)
            run('bash',str(second/'scripts/deploy.sh'),'--root',temporary,'--sha','b'*40,'--app-only')
            assert execute(project,CHECK)==before
            print('PASS update preserved users, records, and uploaded bytes')
            backup=Path((deployment/'state/latest-backup').read_text().strip())
            run('bash',str(second/'scripts/restore.sh'),'--backup',str(backup),'--new-volume',restored)
            # Read restored data in an isolated helper; original volume untouched.
            image=docker('inspect','--format','{{.Image}}',container(project),quiet=True).stdout.strip()
            restored_data=json.loads(docker('run','--rm','--network','none','--mount',f'type=volume,source={restored},target=/data','--entrypoint','python',image,'-c',CHECK,quiet=True).stdout)
            assert restored_data==before
            assert execute(project,CHECK)==before
            print('PASS backup restored into a new volume without modifying active data')
            rejected=run('bash',str(second/'scripts/restore.sh'),'--backup',str(backup),'--new-volume',restored,check=False,quiet=True)
            assert rejected.returncode!=0
            print('PASS restore refused to overwrite an existing volume')
            previous=docker('inspect','--format','{{.Image}}',container(project),quiet=True).stdout.strip()
            command=['bash',str(bad/'scripts/deploy.sh'),'--root',temporary,'--sha','c'*40,'--app-only']
            with (deployment/'failed-deploy.log').open('w+') as logfile:
                process=subprocess.Popen(command,stdout=logfile,stderr=subprocess.STDOUT,text=True)
                deadline=time.monotonic()+150
                written=False
                while process.poll() is None and time.monotonic()<deadline:
                    candidate=container(project)
                    if candidate:
                        label=docker('inspect','--format','{{index .Config.Labels "org.opencontainers.image.revision"}}',candidate,check=False,quiet=True)
                        if label.returncode==0 and label.stdout.strip()=='c'*40:
                            mutate="import sqlite3; c=sqlite3.connect('/data/folio.sqlite3'); c.execute(\"UPDATE items SET title='written after replacement'\"); c.commit(); print('committed')"
                            wrote=docker('exec',candidate,'python','-c',mutate,check=False,quiet=True)
                            if wrote.returncode==0:
                                written=True
                                break
                    time.sleep(1)
                assert written, 'Failed candidate never became available for committed-write drill'
                code=process.wait(timeout=120)
                logfile.seek(0); output=logfile.read();print(output,end='')
            assert code!=0 and 'ROLLBACK previous immutable app image is healthy' in output
            assert docker('inspect','--format','{{.Image}}',container(project),quiet=True).stdout.strip()==previous
            expected=dict(before,titles=['written after replacement'])
            assert execute(project,CHECK)==expected
            assert (deployment/'state/current-sha').read_text().strip()=='b'*40
            print('PASS failed health check rolled back the image and retained a write committed after backup')
            guarded=releases/('d'*40);copy_release(guarded,'d'*40)
            module=guarded/'bedtime.py'
            module.write_text(module.read_text().replace('SCHEMA = """','SCHEMA = """\nCREATE TABLE IF NOT EXISTS ops_qa_guard (id INTEGER PRIMARY KEY);',1))
            rejected=run('bash',str(guarded/'scripts/deploy.sh'),'--root',temporary,'--sha','d'*40,'--app-only',check=False,quiet=True)
            assert rejected.returncode==4 and 'Schema change refused before stopping app' in rejected.stdout
            assert execute(project,CHECK)==expected
            print('PASS bedtime schema change refused before stopping the active app')
            invalid=releases/('e'*40);copy_release(invalid,'e'*40)
            (invalid/'server.py').write_text('invalid syntax(\n')
            rejected=run('bash',str(invalid/'scripts/deploy.sh'),'--root',temporary,'--sha','e'*40,'--app-only',check=False,quiet=True)
            assert rejected.returncode!=0
            assert execute(project,CHECK)==expected
            print('PASS schema helper failure stopped deployment without touching the active app')
            # Corruption is rejected before any restoration target exists.
            (backup/'data/uploads'/('4'*32)).write_bytes(b'corrupt')
            corrupt=run('bash',str(second/'scripts/restore.sh'),'--backup',str(backup),'--new-volume',token+'_corrupt',check=False,quiet=True)
            assert corrupt.returncode!=0
            assert docker('volume','inspect',token+'_corrupt',check=False,quiet=True).returncode!=0
            print('PASS corrupt backup rejected before creating a restore volume')
        finally:
            if process is not None and process.poll() is None:
                process.terminate();process.wait(timeout=10)
            ids=docker('ps','-aq','--filter',f'label=com.docker.compose.project={project}',quiet=True).stdout.split()
            if ids: docker('rm','-f',*ids,check=False,quiet=True)
            networks=docker('network','ls','-q','--filter',f'label=com.docker.compose.project={project}',quiet=True).stdout.split()
            if networks: docker('network','rm',*networks,check=False,quiet=True)
            for volume in (project+'_data',restored,token+'_corrupt'):
                docker('volume','rm',volume,check=False,quiet=True)
            for sha in ('a'*40,'b'*40,'c'*40):
                docker('image','rm',f'{project}-app:{sha}',check=False,quiet=True)


if __name__=='__main__':
    main()
