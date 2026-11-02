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

    def test_service_plist(self):
        import plistlib
        p = plistlib.loads(gj.service_plist(8080, script="/x/gj", python="/usr/bin/python3",
                                            path="/opt/homebrew/bin:/usr/bin"))
        self.assertEqual(p["ProgramArguments"],
                         ["/usr/bin/python3", "/x/gj", "serve", "--port", "8080"])
        self.assertTrue(p["RunAtLoad"] and p["KeepAlive"])
        self.assertEqual(p["EnvironmentVariables"]["PATH"], "/opt/homebrew/bin:/usr/bin")
        self.assertEqual(p["EnvironmentVariables"]["GJ_DB"], str(gj.DB_PATH))


class Live(unittest.TestCase):
    def test_claude_turn(self):
        tool = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"description": "Run tests"}}]}}
        result = {"type": "user", "message": {"content": [{"type": "tool_result"}]}}
        final = {"type": "assistant", "message": {"content": [{"type": "text", "text": "All done."}]}}
        self.assertEqual(gj.claude_turn([tool]), (False, "Bash: Run tests"))
        self.assertEqual(gj.claude_turn([tool, result]), (False, "Bash: Run tests"))
        self.assertEqual(gj.claude_turn([tool, result, final]), (True, "All done."))

    def test_codex_turn(self):
        msg = {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                   "content": [{"text": "Looks good."}]}}
        ev = lambda t: {"type": "event_msg", "payload": {"type": t}}
        self.assertEqual(gj.codex_turn([ev("task_started"), msg]), (False, "Looks good."))
        self.assertEqual(gj.codex_turn([msg, ev("task_complete")]), (True, "Looks good."))

    def test_grok_turn(self):
        call = {"type": "assistant", "content": "", "tool_calls": [{"name": "grep"}]}
        self.assertEqual(gj.grok_turn([call]), (False, "grep"))
        self.assertEqual(gj.grok_turn([call, {"type": "assistant", "content": "Fixed."}]),
                         (True, "Fixed."))

    def test_lanes(self):
        t = time.time()
        live = lambda s, seen=t: {"state": s, "seen": seen, "doing": ""}
        e = lambda **kw: dict(effort(status="active", note="", **kw), flags=gj.flags(effort(**kw)))
        self.assertEqual(gj.lane(dict(e(), live=live("working")))[0], "working")
        self.assertEqual(gj.lane(dict(e(), live=live("done")))[0], "you")
        self.assertEqual(gj.lane(dict(e(), live=live("stalled", t - 600)))[0], "you")
        self.assertEqual(gj.lane(dict(e(prs=[pr()]), live=live("done")))[0], "review")
        self.assertEqual(gj.lane(dict(e(prs=[pr(checks="fail")]), live=live("done"))),
                         ("you", "#7 checks failing"))
        stale = pr(pr_updated_at=t - 10 * 86400)
        old = t - 9 * 86400
        self.assertEqual(gj.lane(dict(e(prs=[stale], updated_at=old),
                                      live=live("done", old)))[0], "idle")
        # auto-merge: GitHub will merge it, so it never lands in Needs you
        am = pr(review="APPROVED", auto_merge="SQUASH")
        self.assertEqual(gj.flags(effort(prs=[am])), [])
        self.assertEqual(gj.lane(dict(e(prs=[am]), live=live("done"))), ("review", "auto-merge on"))
        bad = pr(checks="fail", auto_merge="SQUASH")
        self.assertEqual(gj.lane(dict(e(prs=[bad]), live=live("done"))),
                         ("review", "#7 checks failing"))
        blocked = dict(e(prs=[am]), status="blocked", note="waiting on go", live=live("done"))
        self.assertEqual(gj.lane(blocked), ("review", "auto-merge on"))
        mixed = [am, pr(number=8, checks="fail")]
        self.assertEqual(gj.lane(dict(e(prs=mixed), live=live("done")))[0], "you")
        # a fresh PR next to a stale one keeps the effort in review
        self.assertEqual(gj.lane(dict(e(prs=[stale, pr(number=8)], updated_at=old),
                                      live=live("done", old))), ("review", "#7 stale 10d"))


if __name__ == "__main__":
    unittest.main()
