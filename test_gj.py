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
        # blocked before the merge: the merge resolved it; blocked after: follow-up work, keep
        conn.execute("UPDATE efforts SET status = 'blocked', updated_at = 100 WHERE id = ?", (eid,))
        conn.execute("UPDATE prs SET pr_updated_at = 200 WHERE effort_id = ?", (eid,))
        self.assertEqual(gj.auto_close(conn), [(eid, "done")])
        conn.execute("UPDATE efforts SET status = 'blocked', updated_at = 300 WHERE id = ?", (eid,))
        self.assertEqual(gj.auto_close(conn), [])
        conn.execute("UPDATE efforts SET updated_at = 100 WHERE id = ?", (eid,))
        conn.execute("UPDATE prs SET state = 'CLOSED' WHERE effort_id = ?", (eid,))
        self.assertEqual(gj.auto_close(conn), [])   # blocked + closed unmerged: not dropped

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
                         ("you", "#7 checks failing"))
        conflict = pr(mergeable="CONFLICTING", auto_merge="SQUASH")
        self.assertEqual(gj.lane(dict(e(prs=[conflict]), live=live("done"))),
                         ("you", "#7 merge conflict"))
        blocked = dict(e(prs=[am]), status="blocked", note="waiting on go", live=live("done"))
        self.assertEqual(gj.lane(blocked), ("review", "auto-merge on"))
        mixed = [am, pr(number=8, checks="fail")]
        self.assertEqual(gj.lane(dict(e(prs=mixed), live=live("done")))[0], "you")
        # a fresh PR next to a stale one keeps the effort in review
        self.assertEqual(gj.lane(dict(e(prs=[stale, pr(number=8)], updated_at=old),
                                      live=live("done", old))), ("review", "#7 stale 10d"))


class Stages(unittest.TestCase):
    def test_stage_from_github(self):
        self.assertEqual(gj.stage_of(effort(stage="verify")), "verify")
        self.assertEqual(gj.stage_of(effort(stage="")), "")
        self.assertEqual(gj.stage_of(effort(stage="review", prs=[pr()])), "codex")
        self.assertEqual(gj.stage_of(effort(stage="human", prs=[pr()])), "human")
        self.assertEqual(gj.stage_of(effort(stage="build", prs=[pr(state="CLOSED")])), "build")
        self.assertEqual(gj.stage_of(effort(stage="codex", prs=[pr(review="APPROVED")])), "merge")
        self.assertEqual(gj.stage_of(effort(stage="build", prs=[pr(state="MERGED")])), "merge")

    def test_stage_lanes(self):
        t = time.time()
        done = {"state": "done", "seen": t, "doing": ""}
        e = dict(effort(status="active", note="", stage="plan"), flags=[], live=done)
        self.assertEqual(gj.lane(e), ("you", "plan ready for your review"))
        e = dict(effort(status="active", note="", stage="human", prs=[pr()]), flags=[], live=done)
        self.assertEqual(gj.lane(e), ("you", "Codex review done, ready for your approval"))
        e["stage"] = "codex"
        self.assertEqual(gj.lane(e)[0], "review")

    def test_report_after_pr_wins(self):
        merged = pr(state="MERGED", pr_updated_at=1000)
        # reported before the PR moved: GitHub's view (merged) wins
        self.assertEqual(gj.stage_of(effort(stage="review", stage_at=500, prs=[merged])), "merge")
        # reported after: the agent is on the next piece
        self.assertEqual(gj.stage_of(effort(stage="build", stage_at=2000, prs=[merged])), "build")

    def test_long_effort_stays_open(self):
        conn = gj.db()
        eid = conn.execute("INSERT INTO efforts(title, status, stage, stage_at, created_at, "
                           "updated_at) VALUES ('long', 'active', 'build', 2000, 0, 0)").lastrowid
        gj.add_pr(conn, eid, "https://github.com/a/b/pull/50")
        conn.execute("UPDATE prs SET state = 'MERGED', pr_updated_at = 1000 WHERE number = 50")
        self.assertNotIn((eid, "done"), gj.auto_close(conn))
        conn.execute("UPDATE efforts SET stage_at = 500 WHERE id = ?", (eid,))
        self.assertIn((eid, "done"), gj.auto_close(conn))

    def test_start_attach(self):
        conn = gj.db()
        eid = conn.execute("INSERT INTO efforts(title, status, agent, session_id, created_at, "
                           "updated_at) VALUES ('old', 'done', 'claude', 's1', 0, 0)").lastrowid
        conn.commit()
        args = type("A", (), dict(id=eid, title=None, prompt=None, epic=None, pr=None,
                                  agent="codex", session="s2", cwd="/tmp", new=False))
        gj.cmd_start(args)
        row = gj.db().execute("SELECT * FROM efforts WHERE id = ?", (eid,)).fetchone()
        self.assertEqual((row["title"], row["agent"], row["session_id"], row["status"]),
                         ("old", "codex", "s2", "active"))

    def test_stage_cmd(self):
        conn = gj.db()
        eid = conn.execute("INSERT INTO efforts(title, status, stage, created_at, updated_at) "
                           "VALUES ('x', 'blocked', 'plan', 0, 0)").lastrowid
        conn.commit()
        gj.cmd_stage(type("A", (), {"id": eid, "stage": "review", "note": "e2e green"}))
        row = gj.db().execute("SELECT * FROM efforts WHERE id = ?", (eid,)).fetchone()
        self.assertEqual((row["stage"], row["note"], row["status"]), ("review", "e2e green", "active"))


if __name__ == "__main__":
    unittest.main()
