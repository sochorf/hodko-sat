"""Offline regression tests: no network, repository writes, or Home Assistant."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

import watch_upstream as w

START = "2026-11-23T00:00:00Z"
NOW = "2026-11-24T09:15:00Z"
HEAD = "a" * 40
TAG = "b" * 40


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.issues = []
        self.head = HEAD
        self.branch = "develop"
        self.tags = {"5.0.0": TAG}
        self.releases = [{"id": 1, "tag_name": "5.0.0", "name": "Release", "body": "Original",
                          "published_at": START, "draft": False, "prerelease": False}]
        self.upstream_items = []
        self.pulls = {}
        self.fail = None
        self.fail_after_post = False

    def request(self, method, path, body=None):
        self.calls.append((method, path, deepcopy(body)))
        if self.fail and self.fail in path:
            raise w.WatchError("API failed")
        u = f"/repos/{w.UPSTREAM}"
        t = f"/repos/{w.TARGET}"
        if method == "POST" and path == t + "/issues":
            issue = dict(body, number=len(self.issues) + 1, user={"login": "github-actions[bot]"}, state="open")
            self.issues.append(deepcopy(issue))
            if self.fail_after_post:
                raise w.WatchError("POST timeout")
            return issue
        if path == u:
            return {"full_name": w.UPSTREAM, "default_branch": self.branch}
        if path.startswith(u + "/git/ref/heads/"):
            return {"object": {"type": "commit", "sha": self.head}}
        if path.startswith(u + "/git/ref/tags/"):
            return {"object": {"type": "commit", "sha": self.tags[path.rsplit("/", 1)[1]]}}
        if path.startswith(u + "/pulls/"):
            return deepcopy(self.pulls[int(path.rsplit("/", 1)[1])])
        parsed = urlsplit(path)
        q = parse_qs(parsed.query)
        page = int(q["page"][0])
        if parsed.path == t + "/issues":
            rows = list(reversed(self.issues))
        elif parsed.path == u + "/issues":
            rows = [x for x in self.upstream_items if x["updated_at"] >= q["since"][0]]
        elif parsed.path == u + "/releases":
            rows = self.releases
        else:
            raise AssertionError(path)
        return deepcopy(rows[(page - 1) * 100:page * 100])


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.cfg = {"target": w.TARGET, "target_branch": w.BRANCH,
                    "reviewed_upstream_commit": TAG, "release_since": START,
                    "initial_state": {"schema": 1, "upstream": w.UPSTREAM, "branch": "develop",
                                      "head": HEAD, "checked_at": START, "releases": {}, "items": {}}}
        state, _ = w.collect(self.api, self.cfg, self.cfg["initial_state"], START)
        self.cfg["initial_state"] = state
        self.api.calls.clear()

    def run_watch(self, apply=False, cutoff=NOW):
        return w.run(self.api, self.cfg, apply=apply, cutoff=cutoff)

    def writes(self):
        return [x for x in self.api.calls if x[0] != "GET"]

    def item(self, number=206, **kw):
        return {"number": number, "title": "Stale outdoor report", "body": "Details", "state": "open",
                "updated_at": "2026-11-24T08:00:00Z", "comments": 0, "labels": [], **kw}

    def test_unchanged_is_quiet_and_preview_never_writes(self):
        self.assertEqual(self.run_watch(True)["changes"], [])
        self.api.head = "c" * 40
        self.assertEqual(len(self.run_watch()["changes"]), 1)
        self.assertEqual(self.writes(), [])

    def test_one_digest_and_closed_issue_dedup(self):
        self.api.head = "c" * 40
        self.api.releases[0]["body"] = "Updated notes"
        self.api.upstream_items = [self.item()]
        result = self.run_watch(True)
        self.assertEqual(len(result["changes"]), 3)
        self.assertEqual(len(self.writes()), 1)
        self.api.issues[0]["state"] = "closed"
        self.assertEqual(self.run_watch(True)["changes"], [])
        self.assertEqual(len(self.writes()), 1)

    def test_rewind_to_previously_seen_head_is_new_event(self):
        self.api.head = "c" * 40
        self.run_watch(True)
        self.api.head = HEAD
        self.assertEqual(len(self.run_watch(True)["changes"]), 1)
        self.assertEqual(len(self.writes()), 2)

    def test_stable_prerelease_and_edited_release(self):
        for prerelease in (False, True):
            with self.subTest(prerelease=prerelease):
                self.setUp()
                self.api.tags["5.1.0-beta.1"] = "d" * 40
                self.api.releases.append(dict(self.api.releases[0], id=2, tag_name="5.1.0-beta.1", prerelease=prerelease))
                self.assertEqual(len(self.run_watch(True)["changes"]), 1)
                self.api.releases[-1]["body"] = "Revised"
                self.assertEqual(len(self.run_watch(True)["changes"]), 1)

    def test_tag_movement_and_release_removal(self):
        self.api.tags["5.0.0"] = "d" * 40
        self.assertEqual(len(self.run_watch(True)["changes"]), 1)
        self.api.releases.clear()
        self.assertIn("není ve veřejném seznamu", self.run_watch(True)["changes"][0])

    def test_old_release_is_out_of_scope_and_draft_ignored(self):
        self.api.releases += [dict(self.api.releases[0], id=8, published_at="2020-01-01T00:00:00Z"),
                              dict(self.api.releases[0], id=9, draft=True)]
        self.assertEqual(self.run_watch(True)["changes"], [])

    def test_issue_change_and_pr_merge_are_new(self):
        self.api.upstream_items = [self.item(pull_request={"url": "not-executed"})]
        self.api.pulls[206] = {"updated_at": "2026-11-24T08:00:00Z", "head": {"sha": "e" * 40}, "merged": False, "state": "open"}
        self.run_watch(True)
        self.api.upstream_items[0].update(state="closed", updated_at="2026-11-24T10:00:00Z")
        self.api.pulls[206].update(merged=True, state="closed", updated_at="2026-11-24T10:00:00Z")
        result = self.run_watch(True, "2026-11-24T11:00:00Z")
        self.assertEqual(len(result["changes"]), 1)
        self.assertIn("merged", result["changes"][0])

    def test_future_updates_are_not_lost(self):
        self.api.head = "c" * 40
        self.api.upstream_items = [self.item(updated_at="2026-11-24T10:00:00Z")]
        self.assertEqual(len(self.run_watch(True)["changes"]), 1)
        self.assertEqual(len(self.run_watch(True, "2026-11-24T11:00:00Z")["changes"]), 1)

    def test_failed_scan_does_not_publish_partial_result(self):
        for fragment in ("/releases?", "/issues?state=all&sort=updated", "/git/ref/heads/"):
            with self.subTest(fragment=fragment):
                self.setUp()
                self.api.head = "c" * 40
                self.api.fail = fragment
                with self.assertRaises(w.WatchError): self.run_watch(True)
                self.assertEqual(self.writes(), [])

    def test_pagination_and_closed_evidence_on_second_page(self):
        self.api.head = "c" * 40
        self.run_watch(True)
        self.api.issues[0]["state"] = "closed"
        self.api.issues += [{"number": i + 2, "body": "", "user": {"login": "user"}} for i in range(100)]
        self.assertEqual(self.run_watch(True)["changes"], [])
        self.assertTrue(any("sort=created" in p and "page=2" in p for _, p, _ in self.api.calls))

    def test_page_limit_and_late_page_failure_never_write(self):
        self.api.upstream_items = [self.item(i + 1) for i in range(100)]
        with patch.object(w, "MAX_PAGES", 1), self.assertRaises(w.WatchError): self.run_watch(True)
        self.assertEqual(self.writes(), [])
        self.api.fail = "page=2"
        with self.assertRaises(w.WatchError): self.run_watch(True)
        self.assertEqual(self.writes(), [])

    def test_post_timeout_reconciles_without_duplicate(self):
        self.api.head = "c" * 40
        self.api.fail_after_post = True
        with self.assertRaises(w.WatchError): self.run_watch(True)
        self.assertEqual(len(self.writes()), 1)
        self.api.fail_after_post = False
        self.assertEqual(self.run_watch(True)["changes"], [])
        self.assertEqual(len(self.writes()), 1)

    def test_corrupt_checkpoint_fails_instead_of_recreating(self):
        self.api.head = "c" * 40
        self.run_watch(True)
        self.api.issues[0]["body"] = w.MARKER
        with self.assertRaises(w.WatchError): self.run_watch(True)
        self.assertEqual(len(self.writes()), 1)

    def test_human_issue_cannot_supply_checkpoint(self):
        self.api.issues = [{"number": 100, "user": {"login": "visitor"}, "body": w.MARKER}]
        self.assertEqual(self.run_watch(True)["changes"], [])

    def test_time_reversal_and_body_limit_fail(self):
        with self.assertRaises(w.WatchError): self.run_watch(True, "2026-11-22T00:00:00Z")
        self.api.head = "c" * 40
        with patch.object(w, "MAX_BODY", 10), self.assertRaises(w.WatchError): self.run_watch(True)
        self.assertEqual(self.writes(), [])

    def test_source_text_is_not_executable_or_a_mention(self):
        self.api.upstream_items = [self.item(title="@sochorf [x](https://evil.invalid)\n<script>`$(cmd)`")]
        result = self.run_watch(True)
        self.assertNotIn("@sochorf", result["changes"][0])
        self.assertNotIn("<script>", result["changes"][0])
        self.assertTrue(all(m in ("GET", "POST") for m, _, _ in self.api.calls))

    def test_policy_is_checked_before_reading_upstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "hodko").mkdir()
            (root / "hodko/upstream-watch.json").write_text(json.dumps(self.cfg))
            (root / "release-policy.json").write_text(json.dumps({"published_repository": w.TARGET, "upstream_commit": TAG}))
            self.assertEqual(w.load_baseline(root), self.cfg)
            (root / "release-policy.json").write_text(json.dumps({"published_repository": w.TARGET, "upstream_commit": HEAD}))
            with self.assertRaises(w.WatchError): w.load_baseline(root)

    def test_writes_require_enabled_fork_context(self):
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": w.TARGET,
               "GITHUB_REF": "refs/heads/" + w.BRANCH, "GITHUB_EVENT_NAME": "schedule",
               "GH_TOKEN": "test", "SAT_UPSTREAM_ISSUES_ENABLED": "true"}
        w.require_actions(env)
        w.require_actions(env | {"GITHUB_EVENT_NAME": "workflow_dispatch"})
        for field, value in (("GITHUB_REPOSITORY", w.UPSTREAM), ("GITHUB_REF", "refs/pull/1/merge"),
                             ("GITHUB_EVENT_NAME", "push"), ("SAT_UPSTREAM_ISSUES_ENABLED", "false"),
                             ("GH_TOKEN", ""), ("GITHUB_ACTIONS", "false")):
            with self.subTest(field=field), self.assertRaises(w.WatchError): w.require_actions(env | {field: value})

    def test_api_rejects_unrelated_writes_and_redirects(self):
        api = w.GitHub("secret", allow_write=True)
        api.opener = Mock()
        for method, path in (("POST", f"/repos/{w.UPSTREAM}/issues"),
                             ("POST", f"/repos/{w.TARGET}/releases"),
                             ("PUT", f"/repos/{w.TARGET}/pulls/1/merge"),
                             ("GET", "https://evil.invalid")):
            with self.subTest(path=path), self.assertRaises(w.WatchError): api.request(method, path)
        api.opener.open.assert_not_called()
        with self.assertRaises(w.WatchError): w.GitHub().request("POST", f"/repos/{w.TARGET}/issues", {})
        with self.assertRaises(w.WatchError): w.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid")

    def test_api_transport_exception_hides_credentials(self):
        api = w.GitHub("secret", allow_write=True)
        api.opener = Mock()
        api.opener.open.side_effect = TimeoutError("secret")
        with self.assertRaises(w.WatchError) as error: api.request("POST", f"/repos/{w.TARGET}/issues", {})
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(api.opener.open.call_count, 1)

    def test_generated_read_paths_are_accepted_by_real_allowlist(self):
        self.api.upstream_items = [self.item(pull_request={})]
        self.api.pulls[206] = {"updated_at": "2026-11-24T08:00:00Z", "head": {"sha": "e" * 40}, "merged": False, "state": "open"}
        self.run_watch()
        client = w.GitHub()
        client.opener = MagicMock()
        client.opener.open.return_value.__enter__.return_value = Mock(status=200)
        with patch.object(w.json, "load", return_value={}):
            for method, path, body in self.api.calls:
                with self.subTest(path=path):
                    self.assertEqual(client.request(method, path, body), {})
            client.request("GET", f"/repos/{w.UPSTREAM}/git/ref/heads/topic%2Ffeature")

    def test_annotated_tag_is_resolved_and_cycles_fail(self):
        api = Mock()
        api.request.side_effect = [
            {"object": {"type": "tag", "sha": HEAD}},
            {"object": {"type": "commit", "sha": TAG}},
        ]
        self.assertEqual(w.resolve_ref(api, "tags", "5.0.0"), TAG)
        api.request.side_effect = None
        api.request.return_value = {"object": {"type": "tag", "sha": HEAD}}
        with self.assertRaises(w.WatchError): w.resolve_ref(api, "tags", "5.0.0")

    def test_malformed_or_missing_release_flag_does_not_write(self):
        self.api.releases[0].pop("prerelease")
        with self.assertRaises(w.WatchError): self.run_watch(True)
        self.assertEqual(self.writes(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
