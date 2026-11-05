import importlib.machinery
import json
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

    def test_subagent_keeps_working(self):
        d = Path(tempfile.mkdtemp())
        main = d / "sid.jsonl"
        main.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Waiting on the inventory agent."}]}}) + "\n")
        os.utime(main, (time.time() - 600, time.time() - 600))
        subs = d / "sid" / "subagents"
        subs.mkdir(parents=True)
        sub = subs / "agent-a1.jsonl"
        tool = lambda name, **inp: json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": name, "input": inp}]}}) + "\n"
        sub.write_text(tool("Bash", description="Count objects under prefixes"))
        gj._log_paths[("claude", "sid")] = main
        e = effort(agent="claude", session_id="sid")
        live = gj.liveness(e)
        self.assertEqual((live["state"], live["doing"]), ("working", "Subagent · Bash: Count objects under prefixes"))
        sub.write_text(tool("SubagentHandback", message="done"))
        self.assertEqual(gj.liveness(e)["state"], "done")
        sub.write_text(tool("Bash", description="x"))
        os.utime(sub, (time.time() - 600, time.time() - 600))
        self.assertEqual(gj.liveness(e)["state"], "done")
        # with meta: it runs until it says finished, even through a long silent command
        meta = subs / "agent-a1.meta.json"
        meta.write_text(json.dumps({"finished": False}))
        self.assertEqual(gj.liveness(e)["state"], "working")
        meta.write_text(json.dumps({"finished": True}))
        os.utime(sub)
        self.assertEqual(gj.liveness(e)["state"], "done")

    def test_background_shell(self):
        d = Path(tempfile.mkdtemp())
        log = d / "sid.jsonl"
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        lines = [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tu1", "name": "Bash",
             "input": {"command": "bazel test //x", "description": "Run tests", "run_in_background": True}}]}},
            {"type": "user", "timestamp": ts, "toolUseResult": {"backgroundTaskId": "b1"},
             "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "running"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Tests are running."}]}},
        ]
        log.write_text("".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines))
        os.utime(log, (time.time() - 600, time.time() - 600))
        gj._log_paths[("claude", "sh")] = log
        e = effort(agent="claude", session_id="sh")
        live = gj.liveness(e)
        self.assertEqual((live["state"], live["doing"]), ("working", "Background shell · Run tests"))
        with open(log, "a") as f:   # a half-written line is left for the next read
            f.write('{"type":"queue-operation","content":"<task-notification>\\n<task-id>b1</task-id>')
        self.assertEqual(gj.liveness(e)["state"], "working")
        with open(log, "a") as f:
            f.write('"}\n')
        self.assertEqual(gj.liveness(e)["state"], "done")


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
        # a stage report on a closed effort reopens it: the next piece of a long effort
        conn.execute("UPDATE efforts SET status = 'done' WHERE id = ?", (eid,))
        conn.commit()
        gj.cmd_stage(type("A", (), {"id": eid, "stage": "build", "note": None}))
        row = gj.db().execute("SELECT * FROM efforts WHERE id = ?", (eid,)).fetchone()
        self.assertEqual(row["status"], "active")

    def test_working_agent_keeps_effort(self):
        conn = gj.db()
        eid = conn.execute("INSERT INTO efforts(title, status, created_at, updated_at) "
                           "VALUES ('x', 'review', 0, 0)").lastrowid
        gj.add_pr(conn, eid, "https://github.com/a/b/pull/60")
        conn.execute("UPDATE prs SET state = 'MERGED', pr_updated_at = 1000 WHERE number = 60")
        real = gj.liveness
        gj.liveness = lambda e: {"seen": time.time(), "state": "working", "doing": "x"}
        try:
            self.assertEqual(gj.auto_close(conn), [])
            conn.execute("UPDATE efforts SET status = 'done', updated_at = ? WHERE id = ?",
                         (time.time(), eid))
            lanes = {e["id"]: e["lane"] for e in gj.board(conn)["efforts"]}
            self.assertEqual(lanes[eid], "working")
            # a newer effort in the same session owns the live work
            old = conn.execute("INSERT INTO efforts(title, status, agent, session_id, created_at, "
                               "updated_at) VALUES ('a', 'done', 'claude', 's9', 0, ?)",
                               (time.time(),)).lastrowid
            conn.execute("INSERT INTO efforts(title, status, agent, session_id, created_at, "
                         "updated_at) VALUES ('b', 'active', 'claude', 's9', 0, ?)", (time.time(),))
            lanes = {e["id"]: e["lane"] for e in gj.board(conn)["efforts"]}
            self.assertEqual(lanes[old], "shipped")
            # closed after the agent's last activity: it's finished, whatever the log says
            gj.liveness = lambda e: {"seen": time.time() - 3600, "state": "stalled", "doing": "x"}
            lanes = {e["id"]: e["lane"] for e in gj.board(conn)["efforts"]}
            self.assertEqual(lanes[eid], "shipped")
        finally:
            gj.liveness = real
        self.assertEqual(gj.auto_close(conn), [])   # already done; nothing left to close



class Codex(unittest.TestCase):
    R = "<!-- codex-review-receipt:v1 -->"

    def test_state(self):
        c = lambda at, body: {"created_at": at, "body": body}
        comments = [c("2026-11-03T00:17:00Z", "@codex review"),
                    c("2026-11-03T00:22:00Z", self.R + "\n\nCodex Review: Didn't find any major issues."),
                    c("2026-11-03T00:30:00Z", "@codex security review")]
        reviews = [{"submitted_at": "2026-11-02T22:49:00Z", "id": 1, "body": self.R + " Codex Review"},
                   {"submitted_at": None, "id": 2, "body": self.R}]
        asked, receipts = gj.codex_state(comments, reviews)
        self.assertEqual(asked, gj.parse_ts("2026-11-03T00:17:00Z"))
        self.assertEqual([(r["id"], r["clean"]) for r in receipts], [(1, False), (None, True)])
        # a fresh request with nothing after it: still waiting
        comments.append(c("2026-11-03T01:00:00Z", "@codex review"))
        asked, receipts = gj.codex_state(comments, reviews)
        self.assertEqual([r for r in receipts if r["at"] > asked], [])

    def test_thumbs_up_is_clean(self):
        bot = {"login": "chatgpt-codex-connector[bot]"}
        reactions = [{"content": "+1", "created_at": "2026-11-04T23:51:00Z", "user": bot},
                     {"content": "eyes", "created_at": "2026-11-04T23:49:00Z", "user": bot},
                     {"content": "+1", "created_at": "2026-11-04T23:52:00Z", "user": {"login": "someone"}}]
        asked, receipts = gj.codex_state([], [], reactions)
        self.assertEqual([(r["at"], r["clean"]) for r in receipts],
                         [(gj.parse_ts("2026-11-04T23:51:00Z"), True)])
        # a 👍 from an earlier round doesn't answer a later request
        ask = [{"created_at": "2026-11-05T00:10:00Z", "body": "@codex review"}]
        asked, receipts = gj.codex_state(ask, [], reactions)
        self.assertEqual([r for r in receipts if r["at"] > asked], [])

    def test_finding(self):
        body = ("**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange)</sub></sub>  Move the note**\n\n"
                "It belongs in the README.\n\nUseful? React with 👍 / 👎.\n<!-- codex-code-review-finding:v1 -->")
        self.assertEqual(gj.codex_finding({"body": body, "path": "a/b.ts", "line": 3}),
                         "- [P1] a/b.ts:3\n  Move the note: It belongs in the README.")

    def test_wait(self):
        import io, contextlib
        polls = []
        ask = {"created_at": "2026-11-03T01:00:00Z", "body": "@codex review"}
        review = {"submitted_at": "2026-11-03T01:05:00Z", "id": 9, "body": self.R}
        finding = {"body": "**Bug**\nOff by one.", "path": "x.py", "line": 7}

        def items(path):
            if path.endswith("/reviews/9/comments"):
                return [finding]
            if path.endswith("/issues/1/comments"):
                polls.append(1)
                return [ask]
            return [review] if len(polls) > 1 else []
        real = gj.gh_items, gj.run, gj.time.sleep
        gj.gh_items, gj.run, gj.time.sleep = items, lambda *a, **k: "OPEN", lambda s: None
        args = type("A", (), dict(pr="https://github.com/a/b/pull/1", request=False, timeout=5,
                                  interval=0, id=None))
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                gj.cmd_codex_wait(args)
        finally:
            gj.gh_items, gj.run, gj.time.sleep = real
        self.assertEqual(len(polls), 2)
        self.assertIn("1 finding(s). Rounds with findings: 1 of 3", out.getvalue())
        self.assertIn("- x.py:7\n  Bug: Off by one.", out.getvalue())


class Summaries(unittest.TestCase):
    def setUp(self):
        gj.DB_PATH = Path(tempfile.mkdtemp()) / "gj.db"

    def test_span(self):
        from datetime import datetime
        mon = datetime(2026, 11, 2, 9, 30).timestamp()
        start, end, label = gj.summary_span("daily", mon)
        self.assertEqual((datetime.fromtimestamp(start), datetime.fromtimestamp(end)),
                         (datetime(2026, 10, 30), datetime(2026, 11, 2)))
        self.assertEqual(label, "Fri Oct 30 through Sun Nov 1")
        start, end, label = gj.summary_span("daily", datetime(2026, 11, 4, 8).timestamp())
        self.assertEqual((datetime.fromtimestamp(start), label), (datetime(2026, 11, 3), "Tue Nov 3"))
        start, end, _ = gj.summary_span("weekly", mon)
        self.assertEqual((end - start, end), (7 * 86400, mon))

    def test_digest(self):
        t = time.time()
        shipped = effort(id=1, title="Shipped thing", status="done", updated_at=t - 50, lane="shipped",
                         prs=[pr(number=7, state="MERGED", closed_at=t - 60)])
        old = effort(id=2, title="Long ago", status="done", updated_at=t - 9e5, lane="shipped",
                     prs=[pr(number=8, state="MERGED", closed_at=t - 9e5)])
        waiting = effort(id=3, title="Needs a look", lane="you", why="plan ready", updated_at=t - 99,
                         prs=[pr(number=9, state="MERGED", closed_at=t - 9e5), pr(number=10)])
        quiet = effort(id=4, title="Sleepy", lane="idle", updated_at=t - 9e5, created_at=t - 9e5)
        for e in (shipped, old, waiting, quiet):
            e.update(epic="", prompt="", note="", created_at=e.get("created_at", t - 9e5))
            for p in e["prs"]:
                p.update(repo="a/b", title="t", url="u")
        d = gj.summary_digest(None, t - 1000, t, [shipped, old, waiting, quiet])
        done, rest = d.split("# Still open now")
        self.assertIn("Shipped thing", done)
        self.assertIn("merged in window", done)
        self.assertNotIn("Long ago", d)
        self.assertIn("Needs a look", rest)
        self.assertIn("1 earlier PRs", rest)
        self.assertNotIn("#9 ", rest)
        self.assertIn("# Quiet", rest)
        self.assertIn("#4 Sleepy", rest)

    def test_daily_once(self):
        conn = gj.db()
        calls = []
        ask = lambda system, text: calls.append(system) or "## Executive summary\nhi"
        a = gj.make_summary(conn, "daily", ask)
        b = gj.make_summary(conn, "daily", ask)
        self.assertEqual((len(calls), a["text"], b["text"]), (1, "## Executive summary\nhi", a["text"]))
        gj.make_summary(conn, "weekly", ask)
        gj.make_summary(conn, "weekly", ask)
        self.assertEqual(len(calls), 3)
        self.assertEqual(gj.latest_summary(conn, "weekly")["text"], a["text"])


if __name__ == "__main__":
    unittest.main()
