"""Deploy agent against a fake server: no SSH, systemd, git or network is used."""
import io
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import deploy_agent  # noqa: E402
from deploy_agent import Agent, Rejected, main, parse  # noqa: E402

OLD, NEW, OTHER = 'a' * 40, 'b' * 40, 'c' * 40


class FakeHost:
    def __init__(self, *, head=OLD, main=NEW, healthy=(OLD, NEW), tests_ok=True,
                 dirty=False, requirements_changed=False, pip_ok=True):
        self.head, self.main, self.healthy_shas = head, main, set(healthy)
        self.tests_ok, self.dirty, self.req_changed, self.pip_ok = tests_ok, dirty, requirements_changed, pip_ok
        self.clock, self.restarts, self.started_at = 1000.0, 0, None
        self.root_cmds, self.app_cmds, self.spawned, self.events = [], [], [], []

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds

    def spawn(self, mode, sha):
        self.spawned.append((mode, sha))
        return True

    def run(self, argv, timeout=900):
        self.root_cmds.append(argv)
        if argv[:2] == ['systemctl', 'restart']:
            self.events.append(('restart', self.head))
            self.started_at = self.clock
            return 0, ''
        if argv[:2] == ['systemctl', 'is-active']:
            return 0, 'active\n'
        if argv[:2] == ['systemctl', 'show']:
            return 0, f'{self.restarts}\n'
        if argv[0] == 'journalctl':
            ready = self.head in self.healthy_shas and self.clock - self.started_at >= 10
            return 0, ('Connected to Discord; bot ID 1\n' if ready else 'starting\n')
        raise AssertionError(argv)

    def as_app(self, argv, timeout=900):
        self.app_cmds.append(argv)
        if argv[0] == 'git':
            args = argv[3:]
            if args == ['rev-parse', 'HEAD']:
                return 0, self.head + '\n'
            if args[0] == 'status':
                return 0, ' M bot.py\n' if self.dirty else ''
            if args[0] == 'fetch':
                return 0, ''
            if args == ['rev-parse', 'FETCH_HEAD']:
                return 0, self.main + '\n'
            if args[0] == 'diff':
                return (1 if self.req_changed else 0), ''
            if args[:2] == ['reset', '--hard']:
                self.head = args[2]
                self.events.append(('reset', args[2]))
                return 0, ''
            if args[0] == 'worktree':
                return 0, ''
        if argv[1:3] == ['-m', 'pip']:
            self.events.append(('pip', argv[-1]))
            return (0 if self.pip_ok else 1), ''
        if argv[0] == 'sh':
            self.events.append(('tests', self.head))
            return (0 if self.tests_ok else 1), ''
        raise AssertionError(argv)


class DeployAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def agent(self, **kwargs):
        host = FakeHost(**kwargs)
        return Agent(host, Path(self.tmp.name)), host

    def deploy(self, agent, host, sha=NEW):
        self.assertEqual(agent.request('deploy', sha), 'ACCEPTED')
        self.assertEqual(host.spawned[-1], ('deploy', sha))
        return agent.work('deploy', sha)

    def test_requests_are_strict_and_never_echoed(self):
        for command in (None, '', 'deploy', f'deploy {NEW} extra', f'DEPLOY {NEW}', 'deploy ' + 'A' * 40,
                        f'deploy {NEW[:39]}', 'deploy $(id)', f'shell {NEW}', f'deploy {NEW};id'):
            with self.subTest(command=command), self.assertRaises(Rejected):
                parse(command)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(['agent'], {'SSH_ORIGINAL_COMMAND': 'deploy $(touch /tmp/pwned)'}), 1)
        self.assertEqual(out.getvalue(), 'REJECTED bad_request\n')

    def test_happy_deploy_tests_before_touching_the_live_app(self):
        agent, host = self.agent()
        self.assertEqual(self.deploy(agent, host), 'DEPLOYED')
        self.assertEqual(host.head, NEW)
        self.assertEqual(host.events[:3], [('tests', OLD), ('reset', NEW), ('restart', NEW)])
        self.assertEqual(agent.request('status', NEW), 'DEPLOYED')

    def test_git_pip_and_tests_never_run_as_root(self):
        agent, host = self.agent(requirements_changed=True)
        self.deploy(agent, host)
        self.assertFalse([c for c in host.root_cmds if c[0] in {'git', 'sh', deploy_agent.PYTHON}])
        self.assertTrue(any(c[0] == 'git' for c in host.app_cmds))

    def test_real_host_drops_root_for_repository_code(self):
        seen = []
        host = deploy_agent.Host()
        host.run = lambda argv, timeout=900: seen.append(argv) or (0, '')
        host.as_app(['git', 'status'])
        self.assertEqual(seen, [['runuser', '-u', 'socialbot', '--', 'env', 'HOME=/opt/discord-social-bot',
                                 'git', 'status']])
        self.assertNotIn('HOME', deploy_agent.Host.ENV)  # no root home or secrets leak to children

    def test_only_the_current_main_head_from_a_clean_checkout(self):
        agent, host = self.agent()
        self.assertEqual(agent.request('deploy', OTHER), 'REJECTED not_main_head')
        agent, host = self.agent(dirty=True)
        self.assertEqual(agent.request('deploy', NEW), 'REJECTED dirty_checkout')
        self.assertEqual(host.spawned, [])

    def test_already_deployed_is_not_restarted(self):
        agent, host = self.agent(head=NEW)
        self.assertEqual(agent.request('deploy', NEW), 'ALREADY_DEPLOYED')
        self.assertEqual(host.spawned, [])

    def test_failing_tests_leave_production_untouched(self):
        agent, host = self.agent(tests_ok=False)
        self.assertEqual(self.deploy(agent, host), 'FAILED')
        self.assertEqual(host.head, OLD)
        self.assertNotIn('restart', [e[0] for e in host.events])

    def test_new_pins_are_tested_and_restored_when_tests_fail(self):
        agent, host = self.agent(tests_ok=False, requirements_changed=True)
        self.assertEqual(self.deploy(agent, host), 'FAILED')
        pips = [e[1] for e in host.events if e[0] == 'pip']
        self.assertEqual(pips, [str(deploy_agent.TEST_TREE / 'requirements.txt'),
                                str(deploy_agent.APP / 'requirements.txt')])
        self.assertLess(host.events.index(('pip', pips[0])), host.events.index(('tests', OLD)))

    def test_unhealthy_release_is_rolled_back_automatically(self):
        agent, host = self.agent(healthy=(OLD,))
        self.assertEqual(self.deploy(agent, host), 'ROLLED_BACK')
        self.assertEqual(host.head, OLD)

    def test_crash_loop_counts_as_unhealthy(self):
        agent, host = self.agent()
        original = host.run

        def run(argv, timeout=900):
            if argv[:2] == ['systemctl', 'show'] and host.head == NEW and host.started_at is not None:
                host.restarts += 1  # systemd restarted the crashed bot
            return original(argv, timeout)

        host.run = run
        self.assertEqual(self.deploy(agent, host), 'ROLLED_BACK')

    def test_failed_rollback_blocks_everything_until_the_owner_looks(self):
        agent, host = self.agent(healthy=())
        self.assertEqual(self.deploy(agent, host), 'NEEDS_OWNER')
        host.main = OTHER
        self.assertEqual(agent.request('deploy', OTHER), 'BUSY')

    def test_jobs_are_never_resumed_or_repeated(self):
        agent, host = self.agent()
        self.deploy(agent, host)
        self.assertEqual(agent.work('deploy', NEW), 'REJECTED not_accepted')
        self.assertEqual(agent.work('deploy', OTHER), 'REJECTED not_accepted')

    def test_running_job_makes_new_requests_busy(self):
        agent, host = self.agent()
        self.assertEqual(agent.request('deploy', NEW), 'ACCEPTED')
        self.assertEqual(agent.request('deploy', NEW), 'BUSY')
        self.assertEqual(agent.request('status', NEW), 'ACCEPTED')
        self.assertEqual(agent.request('status', OTHER), 'UNKNOWN_JOB')

    def test_manual_rollback_only_of_the_current_deploy(self):
        agent, host = self.agent()
        self.assertEqual(agent.request('rollback', NEW), 'REJECTED not_current_deploy')
        self.deploy(agent, host)
        self.assertEqual(agent.request('rollback', NEW), 'ACCEPTED')
        self.assertEqual(agent.work('rollback', NEW), 'ROLLED_BACK')
        self.assertEqual(host.head, OLD)


if __name__ == '__main__':
    unittest.main()
