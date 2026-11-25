#!/usr/bin/python3 -I
"""Discord Social Bot deploy agent: TEST -> SWITCH -> HEALTH -> (auto) ROLLBACK.

Installed by the owner as a root-owned copy (tools/vps_deploy_bootstrap.sh) and
reached only through the SSH forced command of the dedicated deploy user:

    restrict,command="sudo -n /usr/local/sbin/discord-social-bot-deploy" ssh-ed25519 ...

Requests (SSH_ORIGINAL_COMMAND); nothing else is accepted:

    deploy <sha40>    <sha40> must be origin/main; a detached worker runs the tests
                      in a separate checkout, switches the app and checks health
    rollback <sha40>  <sha40> must be the deployed commit; return to its previous one
    status <sha40>    read-only phase of that job

Safety: standard library only; never imports bot code or reads .env; git, pip and
tests run as the bot user, never as root; one job at a time (flock); an
interrupted job is never resumed. Output is a fixed token, never paths or logs.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

APP = Path('/opt/discord-social-bot/app')
TEST_TREE = Path('/opt/discord-social-bot/deploy-test')
PYTHON = '/opt/discord-social-bot/venv/bin/python'
APP_USER = 'socialbot'
APP_HOME = '/opt/discord-social-bot'
SERVICE = 'discord-social-bot.service'
STATE = Path('/var/lib/discord-social-bot-deploy')
READY = 'Connected to Discord'
SHA_RE = re.compile(r'[0-9a-f]{40}')
HEALTH_TIMEOUT = 120  # seconds until the bot must report READY
HEALTH_STABLE = 20    # seconds without a systemd restart after READY
FINAL = {'DEPLOYED', 'ROLLED_BACK', 'FAILED', 'NEEDS_OWNER'}


class Rejected(Exception):
    """Carries a fixed code only; never input, paths or command output."""


class Host:
    """All side effects in one place so tests can substitute a fake server."""

    ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'}

    def run(self, argv: list[str], timeout: int = 900) -> tuple[int, str]:
        try:
            done = subprocess.run(argv, capture_output=True, text=True, env=self.ENV,
                                  timeout=timeout, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return 127, ''
        return done.returncode, done.stdout

    def as_app(self, argv: list[str], timeout: int = 900) -> tuple[int, str]:
        # Repository code (tests, pip) runs with the bot's rights, never root's.
        return self.run(['runuser', '-u', APP_USER, '--', 'env', f'HOME={APP_HOME}', *argv], timeout)

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def spawn(self, mode: str, sha: str) -> bool:
        rc, _ = self.run(['systemd-run', '--quiet', '--collect', f'--unit=discord-social-bot-{mode}-{sha[:12]}',
                          sys.executable, '-I', str(Path(__file__).resolve()), '--worker', mode, sha], 60)
        return rc == 0


class Agent:
    def __init__(self, host: Host | None = None, state: Path = STATE):
        self.host = host or Host()
        self.state = state
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)

    # job records --------------------------------------------------------------
    def job(self, sha: str) -> dict | None:
        try:
            return json.loads((self.state / f'{sha}.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    def save(self, job: dict, phase: str) -> None:
        job['phase'], job['at'] = phase, self.host.now()
        path = self.state / f"{job['sha']}.json"
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(job), encoding='utf-8')
        os.replace(tmp, path)

    def busy(self) -> bool:
        for path in self.state.glob('*.json'):
            job = self.job(path.stem)
            if job and job.get('phase') not in FINAL - {'NEEDS_OWNER'}:
                return True  # running, or NEEDS_OWNER blocks until the owner clears it
        return False

    def locked(self):
        handle = open(self.state / 'lock', 'w')
        fcntl.flock(handle, fcntl.LOCK_EX)
        return handle

    # git and service helpers ----------------------------------------------------
    def git(self, *args: str, cwd: Path = APP) -> tuple[int, str]:
        return self.host.as_app(['git', '-C', str(cwd), *args], 300)

    def head(self) -> str | None:
        rc, out = self.git('rev-parse', 'HEAD')
        return out.strip() if rc == 0 and SHA_RE.fullmatch(out.strip()) else None

    def restarts(self) -> int | None:
        rc, out = self.host.run(['systemctl', 'show', SERVICE, '-p', 'NRestarts', '--value'], 30)
        return int(out.strip()) if rc == 0 and out.strip().isdigit() else None

    def active(self) -> bool:
        rc, out = self.host.run(['systemctl', 'is-active', SERVICE], 30)
        return rc == 0 and out.strip() == 'active'

    def healthy(self) -> bool:
        started = self.host.now()
        if self.host.run(['systemctl', 'restart', SERVICE], 120)[0] != 0:
            return False
        before = self.restarts()
        ready_at = None
        while self.host.now() - started < HEALTH_TIMEOUT + HEALTH_STABLE:
            self.host.sleep(5)
            if not self.active() or self.restarts() != before:
                return False
            if ready_at is None:
                rc, out = self.host.run(['journalctl', '-u', SERVICE, '--since', f'@{int(started)}',
                                         '-o', 'cat', '--no-pager'], 60)
                if rc == 0 and READY in out:
                    ready_at = self.host.now()
                elif self.host.now() - started > HEALTH_TIMEOUT:
                    return False
            elif self.host.now() - ready_at >= HEALTH_STABLE:
                return True
        return False

    def requirements_changed(self, a: str, b: str) -> bool:
        return self.git('diff', '--quiet', a, b, '--', 'requirements.txt')[0] != 0

    def pip(self, tree: Path) -> bool:
        return self.host.as_app([PYTHON, '-m', 'pip', 'install', '-q', '-r', str(tree / 'requirements.txt')])[0] == 0

    def switch(self, target: str, other: str) -> bool:
        if self.git('reset', '--hard', target)[0] != 0:
            return False
        return not self.requirements_changed(other, target) or self.pip(APP)

    def tests_pass(self, sha: str, previous: str) -> bool:
        """Test the new commit in a separate checkout; the live app is untouched."""
        self.git('worktree', 'remove', '--force', str(TEST_TREE))
        if self.git('worktree', 'add', '--detach', str(TEST_TREE), sha)[0] != 0:
            return False
        try:
            # The shared venv must hold the new pins, or the tests prove nothing.
            if self.requirements_changed(previous, sha) and not self.pip(TEST_TREE):
                return False
            rc, _ = self.host.as_app(['sh', '-c', f'cd {TEST_TREE} && {PYTHON} -m unittest discover -s tests'])
            return rc == 0
        finally:
            self.git('worktree', 'remove', '--force', str(TEST_TREE))

    # requests -----------------------------------------------------------------
    def request(self, action: str, sha: str) -> str:
        if action == 'status':
            job = self.job(sha)
            return job['phase'] if job else 'UNKNOWN_JOB'
        with self.locked():
            if self.busy():
                return 'BUSY'
            if action == 'deploy':
                job = self.job(sha)
                if self.head() == sha and self.active() and (not job or job['phase'] == 'DEPLOYED'):
                    return 'ALREADY_DEPLOYED'
                rc, out = self.git('status', '--porcelain', '--untracked-files=no')
                if rc != 0 or out.strip():
                    return 'REJECTED dirty_checkout'
                if self.git('fetch', '--quiet', 'origin', 'main')[0] != 0:
                    return 'REJECTED fetch_failed'
                rc, out = self.git('rev-parse', 'FETCH_HEAD')
                if rc != 0 or out.strip() != sha:
                    return 'REJECTED not_main_head'
                previous = self.head()
                if not previous:
                    return 'REJECTED no_current_commit'
                job = {'sha': sha, 'mode': 'deploy', 'previous': previous}
            else:
                job = self.job(sha)
                if not job or job.get('phase') != 'DEPLOYED' or self.head() != sha:
                    return 'REJECTED not_current_deploy'
                job = {'sha': sha, 'mode': 'rollback', 'previous': job['previous']}
            self.save(job, 'ACCEPTED')
            if not self.host.spawn(job['mode'], sha):
                self.save(job, 'FAILED')
                return 'REJECTED spawn_failed'
            return 'ACCEPTED'

    def work(self, mode: str, sha: str) -> str:
        job = self.job(sha)
        if not job or job.get('phase') != 'ACCEPTED' or job.get('mode') != mode:
            return 'REJECTED not_accepted'  # never resume or repeat a job
        previous = job['previous']
        target = sha if mode == 'deploy' else previous
        if mode == 'deploy':
            self.save(job, 'TESTING')
            if not self.tests_pass(sha, previous):
                # Code untouched; put the old pins back if the venv was changed.
                restored = not self.requirements_changed(previous, sha) or self.pip(APP)
                self.save(job, 'FAILED' if restored else 'NEEDS_OWNER')
                return job['phase']
        self.save(job, 'SWITCHING')
        if self.switch(target, sha if mode == 'rollback' else previous) and self.healthy():
            self.save(job, 'DEPLOYED' if mode == 'deploy' else 'ROLLED_BACK')
            return job['phase']
        if mode == 'rollback':
            self.save(job, 'NEEDS_OWNER')
            return 'NEEDS_OWNER'
        self.save(job, 'ROLLING_BACK')
        if self.switch(previous, sha) and self.healthy():
            self.save(job, 'ROLLED_BACK')
            return 'ROLLED_BACK'
        self.save(job, 'NEEDS_OWNER')
        return 'NEEDS_OWNER'


def parse(command: object) -> tuple[str, str]:
    parts = command.split() if isinstance(command, str) else []
    if len(parts) != 2 or parts[0] not in {'deploy', 'rollback', 'status'} or not SHA_RE.fullmatch(parts[1]):
        raise Rejected('bad_request')
    return parts[0], parts[1]


def main(argv: list[str], environ: dict, agent: Agent | None = None) -> int:
    try:
        if len(argv) == 4 and argv[1] == '--worker':
            if argv[2] not in {'deploy', 'rollback'} or not SHA_RE.fullmatch(argv[3]):
                raise Rejected('bad_request')
            print((agent or Agent()).work(argv[2], argv[3]))
            return 0
        action, sha = parse(environ.get('SSH_ORIGINAL_COMMAND'))
        result = (agent or Agent()).request(action, sha)
    except Rejected as exc:
        result = f'REJECTED {exc}'
    except Exception:  # never echo details to the SSH client
        result = 'REJECTED internal_error'
    print(result)
    return 1 if result.startswith('REJECTED') else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv, dict(os.environ)))
