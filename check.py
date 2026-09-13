import re
import itertools
from datetime import datetime, timezone
import pandas as pd
import requests

# ---- Deadline configuration ----
DEADLINE = datetime(2026, 9, 13, 23, 59, 59, tzinfo=timezone.utc)
DEADLINE_STR = DEADLINE.strftime('%d-%b-%Y %H:%M UTC')

df = pd.read_csv('submissions.csv')

BRANCHES = ["main", "master"]

# ---------------------------------------------------------------------------
# Build every REALISTIC filename + folder variant a student might have used.
# (We deliberately avoid letter-by-letter random casing like "sPrInT_1.md" --
#  that explodes into thousands of combinations for no real-world benefit and
#  would blow through GitHub's API rate limit. Instead we cover every way a
#  human actually tends to type it.)
# ---------------------------------------------------------------------------
WORD_CASES = ["sprint", "Sprint", "SPRINT"]
SEPARATORS = ["_", "-", ""]
NUMBER_FORMATS = ["1", "01"]
EXTENSIONS = [".md", ".MD"]

FILENAMES = {
    f"{word}{sep}{num}{ext}"
    for word, sep, num, ext in itertools.product(WORD_CASES, SEPARATORS, NUMBER_FORMATS, EXTENSIONS)
}

FOLDER_CASES = ["docs", "Docs", "DOCS", "doc", "Doc", "DOC"]
FOLDERS = [f"{f}/" for f in FOLDER_CASES] + [""]  # "" = file sits directly in repo root, no folder

CANDIDATE_PATHS = sorted({f"{folder}{name}" for folder in FOLDERS for name in FILENAMES})
TOTAL_COMBINATIONS = len(CANDIDATE_PATHS) * len(BRANCHES)


def parse_github_repo(url):
    url = str(url).strip().rstrip("/")
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(\.git)?$", url)
    if not match:
        return None
    return match.group(1), match.group(2)


def format_timedelta(td):
    total_seconds = int(td.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "a few seconds"


def get_last_commit_date(owner, repo, path, branch):
    api_url = f"https://api.github.com/repos/{owner}/{repo}/commits"
    params = {"path": path, "sha": branch, "per_page": 1}
    try:
        r = requests.get(api_url, params=params, timeout=8)
    except requests.RequestException as e:
        return None, f"Network error while fetching commit history: {e}"

    if r.status_code == 403:
        return None, "GitHub API rate limit exceeded while checking commit date — try again later or add an auth token."
    if r.status_code != 200:
        return None, f"GitHub API returned status {r.status_code} while fetching commit history."

    try:
        data = r.json()
        if not data:
            return None, "No commit history found for this file path (unexpected)."
        date_str = data[0]["commit"]["committer"]["date"]
        return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc), None
    except (KeyError, IndexError, ValueError) as e:
        return None, f"Could not parse commit date from GitHub API response: {e}"


def check_submission(repo_url):
    """Returns (Status, Reason)."""
    parsed = parse_github_repo(repo_url)
    if not parsed:
        return "Rejected ❌", f"Invalid GitHub repo URL format: '{repo_url}'. Could not extract owner/repo from the link."

    owner, repo = parsed

    repo_api_url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        repo_check = requests.get(repo_api_url, timeout=8)
    except requests.RequestException as e:
        return "Rejected ❌", f"Network error while verifying repository '{owner}/{repo}': {e}"

    if repo_check.status_code == 404:
        return "Rejected ❌", f"Repository '{owner}/{repo}' not found on GitHub — it may be private, deleted, renamed, or the link contains a typo."
    if repo_check.status_code == 403:
        return "Rejected ❌", "GitHub API rate limit exceeded while verifying repository — try again later or add an auth token."
    if repo_check.status_code != 200:
        return "Rejected ❌", f"GitHub API returned unexpected status {repo_check.status_code} while verifying repository '{owner}/{repo}'."

    for branch in BRANCHES:
        for path in CANDIDATE_PATHS:
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
            try:
                r = requests.get(raw_url, timeout=6)
            except requests.RequestException:
                continue

            if r.status_code == 200 and len(r.text.strip()) > 0:
                commit_date, err = get_last_commit_date(owner, repo, path, branch)

                if commit_date is None:
                    return "Rejected ❌", f"File found at '{branch}/{path}' but its commit date could not be verified. {err}"

                commit_str = commit_date.strftime('%d-%b-%Y %H:%M UTC')

                if commit_date <= DEADLINE:
                    return "Accepted ✅", f"File found at '{branch}/{path}', last committed {commit_str} — on time (deadline was {DEADLINE_STR})."
                else:
                    late_by = format_timedelta(commit_date - DEADLINE)
                    return "Rejected ❌", (
                        f"File found at '{branch}/{path}' but last committed {commit_str}, "
                        f"which is {late_by} after the deadline ({DEADLINE_STR}). Late submission."
                    )

    return "Rejected ❌", (
        f"No submission file found. Checked {TOTAL_COMBINATIONS} combinations of filename "
        f"(sprint/Sprint/SPRINT, _/-/none, 1/01, .md/.MD), folder (docs/Docs/DOCS/doc/Doc/DOC/root), "
        f"and branch (main/master) — none existed in '{owner}/{repo}'."
    )


results = df['RepoLink'].apply(
    lambda url: check_submission(url) if str(url).startswith('http')
    else ("Rejected ❌", "RepoLink is missing or not a valid URL.")
)
df['Status'] = results.apply(lambda x: x[0])
df['Reason'] = results.apply(lambda x: x[1])

df.to_csv('submissions.csv', index=False)
print(df[['Name', 'RollNo', 'Status', 'Reason']].to_string(index=False))
