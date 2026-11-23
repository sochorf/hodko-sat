"""Observe public SAT metadata; preview by default, one review issue per changed scan."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

UPSTREAM = "Alexwijn/SAT"
TARGET = "sochorf/hodko-sat"
BRANCH = "codex/hodko-sat"
API = "https://api.github.com"
MARKER = "<!-- hodko-sat-upstream-watch:v1 -->"
STATE = re.compile(r"<!-- hodko-sat-state:([0-9a-f]{64})\n([^\n]+)\n-->")
SHA = re.compile(r"[0-9a-f]{40}")
MAX_PAGES = 20
MAX_BODY = 60000


class WatchError(Exception):
    """Incomplete evidence is a failed run, not an empty successful scan."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def timestamp(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise WatchError("Expected a UTC timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise WatchError("Missing or invalid Git commit")
    return value


def validate_state(state):
    if (state.get("schema") != 1 or state.get("upstream") != UPSTREAM
            or not isinstance(state.get("branch"), str) or not state["branch"]
            or not isinstance(state.get("releases"), dict)
            or not isinstance(state.get("items"), dict)):
        raise WatchError("Invalid or incompatible watcher checkpoint")
    timestamp(state["checked_at"])
    sha(state["head"])
    for values in (state["releases"], state["items"]):
        if len(values) > 1000 or not all(
            isinstance(k, str) and k.isdigit() and isinstance(v, str)
            and re.fullmatch(r"[0-9a-f]{64}", v) for k, v in values.items()
        ):
            raise WatchError("Invalid or oversized checkpoint inventory")
    return state


def load_baseline(root):
    cfg = json.loads((root / "hodko/upstream-watch.json").read_text())
    policy = json.loads((root / "release-policy.json").read_text())
    if (cfg.get("target") != TARGET or cfg.get("target_branch") != BRANCH
            or policy.get("published_repository") != TARGET
            or cfg.get("reviewed_upstream_commit") != policy.get("upstream_commit")):
        raise WatchError("Review watcher baseline after changing repository/upstream source")
    sha(cfg["reviewed_upstream_commit"])
    timestamp(cfg["release_since"])
    validate_state(cfg["initial_state"])
    return cfg


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WatchError("API redirect refused; review repository identity")


class GitHub:
    def __init__(self, token="", *, allow_write=False):
        self.token = token
        self.allow_write = allow_write
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, body=None):
        upstream = re.escape(f"/repos/{UPSTREAM}")
        target = re.escape(f"/repos/{TARGET}")
        get_patterns = (
            upstream, target,
            upstream + r"/releases\?per_page=100&page=[1-9][0-9]*",
            upstream + r"/issues\?state=all&sort=updated&direction=asc&since=[0-9A-FTZ%:+.-]+&per_page=100&page=[1-9][0-9]*",
            target + r"/issues\?state=all&sort=created&direction=desc&per_page=100&page=[1-9][0-9]*",
            upstream + r"/git/ref/(?:heads|tags)/[A-Za-z0-9._%/-]+",
            upstream + r"/git/tags/[0-9a-f]{40}",
            upstream + r"/pulls/[1-9][0-9]*",
        )
        valid_get = method == "GET" and body is None and any(re.fullmatch(p, path) for p in get_patterns)
        valid_post = self.allow_write and method == "POST" and path == f"/repos/{TARGET}/issues"
        if not (valid_get or valid_post):
            raise WatchError("API operation outside watcher allowlist")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "hodko-sat-upstream-watch"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            req = Request(API + path, data=None if body is None else canonical(body).encode(),
                          headers=headers, method=method)
            with self.opener.open(req, timeout=30) as response:
                if response.status != (201 if method == "POST" else 200):
                    raise WatchError("Unexpected API response status")
                return json.load(response)
        except HTTPError as error:
            code = error.code
            error.close()
            raise WatchError(f"GitHub {method} HTTP {code}; no automatic retry") from None
        except (URLError, OSError, TimeoutError, ValueError):
            # POST may already have succeeded. The next full scan must decide.
            raise WatchError(f"GitHub {method} transport/JSON failure; no automatic retry") from None


def scan(api, path):
    rows = []
    for page in range(1, MAX_PAGES + 1):
        batch = api.request("GET", path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")
        if not isinstance(batch, list) or not all(isinstance(x, dict) for x in batch):
            raise WatchError("Malformed paginated response")
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    raise WatchError("Pagination bound reached; scan is incomplete")


def checkpoint(issues, initial):
    eligible = [i for i in issues if "pull_request" not in i
                and i.get("user", {}).get("login") == "github-actions[bot]"
                and MARKER in (i.get("body") or "")]
    if not eligible:
        return deepcopy(validate_state(initial))
    newest = max(eligible, key=lambda i: i["number"])
    matches = STATE.findall(newest.get("body") or "")
    if len(matches) != 1:
        raise WatchError("Latest watcher issue lost its checkpoint; inspect before retrying")
    expected, encoded = matches[0]
    state = json.loads(encoded)
    if digest(state) != expected:
        raise WatchError("Watcher issue checkpoint was modified")
    return deepcopy(validate_state(state))


def resolve_ref(api, kind, name):
    obj = api.request("GET", f"/repos/{UPSTREAM}/git/ref/{kind}/" + quote(name, safe=""))["object"]
    for _ in range(5):
        value = sha(obj["sha"])
        if obj["type"] == "commit":
            return value
        if obj["type"] != "tag":
            break
        obj = api.request("GET", f"/repos/{UPSTREAM}/git/tags/{value}")["object"]
    raise WatchError("Cannot resolve ref to a commit")


def plain(value):
    # Remote metadata is data, never shell/YAML/code or a notification mention.
    value = re.sub(r"[\r\n\t<>`\[\]]", " ", str(value))
    return value.replace("@", "＠").replace("#", "＃")[:180]


def collect(api, cfg, previous, cutoff):
    if timestamp(cutoff) < timestamp(previous["checked_at"]):
        raise WatchError("Clock precedes last checkpoint")
    current = deepcopy(previous)
    current["checked_at"] = cutoff
    changes = []
    repo = api.request("GET", f"/repos/{UPSTREAM}")
    if repo.get("full_name") != UPSTREAM or not repo.get("default_branch"):
        raise WatchError("Upstream repository identity changed")
    current["branch"] = repo["default_branch"]
    current["head"] = resolve_ref(api, "heads", current["branch"])
    if (current["head"], current["branch"]) != (previous["head"], previous["branch"]):
        changes.append(f"Vývojová větev {plain(current['branch'])}: "
                       f"[porovnat {previous['head'][:8]} → {current['head'][:8]}]"
                       f"(https://github.com/{UPSTREAM}/compare/{previous['head']}...{current['head']}). "
                       "Může jít i o přepsání historie; nejde automaticky o nové vydání.")
    releases = {}
    for release in scan(api, f"/repos/{UPSTREAM}/releases"):
        if type(release.get("draft")) is not bool or type(release.get("prerelease")) is not bool:
            raise WatchError("Malformed release flags")
        if release["draft"]:
            continue
        published = timestamp(release["published_at"])
        if published < timestamp(cfg["release_since"]):
            continue
        # Releases/tags are read live, not as an atomic GitHub transaction.
        tag = release["tag_name"]
        if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", tag):
            raise WatchError("Unsupported release tag; manual review required")
        key = str(int(release["id"]))
        commit = resolve_ref(api, "tags", tag)
        releases[key] = digest({k: release.get(k) for k in (
            "tag_name", "name", "body", "prerelease", "published_at")} | {"commit": commit})
        if previous["releases"].get(key) != releases[key]:
            kind = "prerelease" if release["prerelease"] else "stable"
            changes.append(f"Vydání nebo změna vydání [{plain(tag)}]"
                           f"(https://github.com/{UPSTREAM}/releases/tag/{quote(tag, safe='')}), "
                           f"{kind}, commit `{commit[:12]}`. Ověřit poznámky i případný přesun tagu.")
    for key in previous["releases"].keys() - releases.keys():
        changes.append(f"Dříve sledované vydání ID {key} už není ve veřejném seznamu; prověřit odstranění/skrytí.")
    current["releases"] = releases
    query = urlencode({"state": "all", "sort": "updated", "direction": "asc", "since": previous["checked_at"]})
    for item in scan(api, f"/repos/{UPSTREAM}/issues?{query}"):
        updated = timestamp(item["updated_at"])
        if updated > timestamp(cutoff):
            continue  # The next run includes updates after the fixed scan boundary.
        number = int(item["number"])
        if number < 1:
            raise WatchError("Invalid issue number")
        data = {k: item.get(k) for k in ("title", "body", "state", "updated_at", "comments", "labels")}
        kind = "issue"
        status = item["state"]
        if "pull_request" in item:
            kind = "pull"
            pr = api.request("GET", f"/repos/{UPSTREAM}/pulls/{number}")
            if timestamp(pr["updated_at"]) > timestamp(cutoff):
                continue
            data["head"] = sha(pr["head"]["sha"])
            data["merged"] = pr["merged"]
            status = "merged" if pr["merged"] else pr["state"]
        fingerprint = digest(data)
        key = str(number)
        if previous["items"].get(key) != fingerprint:
            changes.append(f"{kind.upper()} [{number}: {plain(item['title'])}]"
                           f"(https://github.com/{UPSTREAM}/{'pull' if kind == 'pull' else 'issues'}/{number}) "
                           f"— {plain(status)}, změna {item['updated_at']}.")
        current["items"][key] = fingerprint
    validate_state(current)
    return current, changes


def payload(state, changes):
    body = f"""{MARKER}

Změny původního SAT od posledního evidovaného stavu (časy UTC).
Automatická detekce metadat, nikoli ověření chyby nebo kompatibility Hodko.

""" + "\n".join("- " + change for change in changes) + """

- [ ] Rozlišit dopad na Hodko PI/HR `compat`, read-only shadow a nativní SAT5.
- [ ] Porovnat s již převzatými opravami; rozhodnout převzít / sledovat / nepoužít.
- [ ] Před převzetím ověřit čistý start, původní PI/HR časy a TTL, jediného
      zapisovatele, ACK/timeout/retry/keepalive, rollover, TUV a návrat verze.

Toto issue nic nemerguje, nepublikuje ani nenasazuje. Limit 55 °C a živé
řízení nejsou součástí kontroly. Issue lze uzavřít, ale nemažte jeho kontrolní
blok: používá se i v uzavřených issues pro kontinuitu a deduplikaci.

""" + f"<!-- hodko-sat-state:{digest(state)}\n{canonical(state)}\n-->"
    if len(body.encode()) > MAX_BODY:
        raise WatchError("Review digest exceeds size limit; no partial checkpoint written")
    return {"title": "Upstream SAT: změny k posouzení " + state["checked_at"],
            "body": body, "assignees": ["sochorf"]}


def run(api, cfg, *, apply=False, cutoff=None):
    cutoff = cutoff or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Read ALL open and closed target issues before any external write.
    issues = scan(api, f"/repos/{TARGET}/issues?state=all&sort=created&direction=desc")
    previous = checkpoint(issues, cfg["initial_state"])
    current, changes = collect(api, cfg, previous, cutoff)
    created = None
    if changes:
        body = payload(current, changes)
        if apply:
            created = api.request("POST", f"/repos/{TARGET}/issues", body)
            if not isinstance(created, dict) or type(created.get("number")) is not int:
                raise WatchError("Incomplete POST response; next full scan must determine outcome")
            created = created["number"]
    return {"mode": "apply" if apply else "preview", "checked_at": cutoff,
            "previous_checkpoint": previous["checked_at"], "changes": changes,
            "created_issue": created, "checkpoint_advanced": created is not None}


def require_actions(env):
    if not (env.get("GITHUB_ACTIONS") == "true" and env.get("GITHUB_REPOSITORY") == TARGET
            and env.get("GITHUB_REF") == "refs/heads/" + BRANCH
            and env.get("GITHUB_EVENT_NAME") in {"schedule", "workflow_dispatch"}
            and env.get("GH_TOKEN") and env.get("SAT_UPSTREAM_ISSUES_ENABLED") == "true"):
        raise WatchError("Issue writes require enabled fork/default-branch Actions context")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Create one review issue (Actions only)")
    args = parser.parse_args()
    try:
        if args.apply:
            require_actions(os.environ)
        root = Path(__file__).resolve().parents[1]
        result = run(GitHub(os.environ.get("GH_TOKEN", ""), allow_write=args.apply),
                     load_baseline(root), apply=args.apply)
        print(canonical(result))
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(summary, "a") as stream:
                stream.write("## SAT upstream check\n\n" + "\n".join("- " + x for x in result["changes"]))
                stream.write("\n\nMode: " + result["mode"] + "; changes: " + str(len(result["changes"])) + "\n")
    except (WatchError, KeyError, TypeError, ValueError, OSError) as error:
        print("Upstream check failed: " + (str(error) if isinstance(error, WatchError) else type(error).__name__), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
