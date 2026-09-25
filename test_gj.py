import importlib.machinery
import os
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).parent
os.environ["GJ_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
gj = importlib.machinery.SourceFileLoader("gj", str(HERE / "gj")).load_module()


def effort(**kw):
    e = dict(id=1, status="review", prs=[], updated_at=time.time(), agent="claude",
             session_id="abc", cwd="/tmp/x y")
    e.update(kw)
    return e


def pr(**kw):
    p = dict(number=7, state="OPEN", checks="pass", mergeable="MERGEABLE", review="",
             draft=0, pr_updated_at=time.time())
    p.update(kw)
    return p


class Flags(unittest.TestCase):
    def test_quiet_when_healthy(self):
        self.assertEqual(gj.flags(effort(prs=[pr()])), [])

    def test_stale_pr(self):
        f = gj.flags(effort(prs=[pr(pr_updated_at=time.time() - 5 * 86400)]))
        self.assertEqual(f, ["#7 stale 5d"])

    def test_problems(self):
        f = gj.flags(effort(prs=[pr(checks="fail", mergeable="CONFLICTING",
                                    review="CHANGES_REQUESTED")]))
        self.assertEqual(f, ["#7 checks failing", "#7 merge conflict", "#7 changes requested"])

    def test_approved(self):
        self.assertEqual(gj.flags(effort(prs=[pr(review="APPROVED")])), ["#7 approved - merge it"])

    def test_idle_without_pr(self):
        f = gj.flags(effort(status="active", updated_at=time.time() - 4 * 86400))
        self.assertEqual(f, ["no PR, idle 4d"])

    def test_closed_is_quiet(self):
        self.assertEqual(gj.flags(effort(status="done", prs=[pr(checks="fail")])), [])


class Misc(unittest.TestCase):
    def test_rollup(self):
        self.assertEqual(gj.rollup([{"conclusion": "SUCCESS"}, {"status": "IN_PROGRESS"}]), "pending")
        self.assertEqual(gj.rollup([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]), "fail")
        self.assertEqual(gj.rollup([{"state": "SUCCESS"}]), "pass")
        self.assertEqual(gj.rollup([]), "")

    def test_resume_cmds(self):
        self.assertEqual(gj.resume_cmd(effort()), "cd '/tmp/x y' && claude --resume abc")
        self.assertEqual(gj.resume_cmd(effort(agent="codex")), "cd '/tmp/x y' && codex resume abc")
        self.assertEqual(gj.resume_cmd(effort(agent="grok", session_id="")),
                         "cd '/tmp/x y' && grok --continue")

    def test_auto_close(self):
        conn = gj.db()
        t = time.time()
        eid = conn.execute("INSERT INTO efforts(title, status, created_at, updated_at) "
                           "VALUES ('x', 'review', ?, ?)", (t, t)).lastrowid
        gj.add_pr(conn, eid, "https://github.com/a/b/pull/1")
        gj.add_pr(conn, eid, "https://github.com/a/b/pull/2/files")
        conn.execute("UPDATE prs SET state = 'MERGED' WHERE number = 1")
        conn.execute("UPDATE prs SET state = 'OPEN' WHERE number = 2")
        self.assertEqual(gj.auto_close(conn), [])
        conn.execute("UPDATE prs SET state = 'CLOSED' WHERE number = 2")
        self.assertEqual(gj.auto_close(conn), [(eid, "done")])

    def test_install_idempotent(self):
        d = Path(tempfile.mkdtemp())
        f = d / "AGENTS.md"
        f.write_text("# mine\n")
        gj.TARGETS["claude"] = f
        args = type("A", (), {"agents": ["claude"], "uninstall": False})
        gj.cmd_install(args)
        gj.cmd_install(args)
        self.assertEqual(f.read_text().count(gj.MARK_BEGIN), 1)
        args.uninstall = True
        gj.cmd_install(args)
        self.assertEqual(f.read_text(), "# mine\n")


if __name__ == "__main__":
    unittest.main()
