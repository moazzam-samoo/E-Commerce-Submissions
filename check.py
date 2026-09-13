import re
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests

# ---- Deadline configuration ----
DEADLINE = datetime(2026, 9, 13, 23, 59, 59, tzinfo=timezone.utc)
DEADLINE_STR = DEADLINE.strftime('%d-%b-%Y %H:%M UTC')

df = pd.read_csv('submissions.csv')

# Folders we consider plausible -- if a student used a different casing for the
# folder itself (e.g. "Docs" instead of "docs"), we still catch it, since each
# is a distinct real path in the git tree.
FOLDER_CANDIDATES = ["docs", "Docs", "DOCS", "doc", "Doc", "DOC", ""]  # "" = repo root

# Case-insensitive pattern matching the filename itself, covering every
# realistic spelling: sprint/Sprint/SPRINT, _/-/none, 1/01, .md/.MD
FILENAME_PATTERN = re.compile(r"^sprint[-_]?0?1\.md$", re.IGNORECASE)

SESSION = requests.Session()


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
        r = SESSION.get(api_url, params=params, timeout=10)
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
    """Returns (Status, Reason). Uses directory-listing instead of guessing raw URLs -- far fewer requests."""
    parsed = parse_github_repo(repo_url)
    if not parsed:
        return "Rejected ❌", f"Invalid GitHub repo URL format: '{repo_url}'. Could not extract owner/repo from the link."

    owner, repo = parsed

    # Step 1: verify repo exists and get its default branch in one call
    try:
        repo_check = SESSION.get(f"https://api.github.com/repos/{owner}/{repo}", timeout=10)
    except requests.RequestException as e:
        return "Rejected ❌", f"Network error while verifying repository '{owner}/{repo}': {e}"

    if repo_check.status_code == 404:
        return "Rejected ❌", f"Repository '{owner}/{repo}' not found on GitHub — it may be private, deleted, renamed, or the link contains a typo."
    if repo_check.status_code == 403:
        return "Rejected ❌", "GitHub API rate limit exceeded while verifying repository — try again later or add an auth token."
    if repo_check.status_code != 200:
        return "Rejected ❌", f"GitHub API returned unexpected status {repo_check.status_code} while verifying repository '{owner}/{repo}'."

    default_branch = repo_check.json().get("default_branch", "main")

    # Step 2: list each candidate folder ONCE via the Contents API and pattern-match filenames inside it
    # (This replaces guessing 500+ raw URLs -- only ~7 requests per repo now.)
    for folder in FOLDER_CANDIDATES:
        contents_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{folder}"
        try:
            r = SESSION.get(contents_url, params={"ref": default_branch}, timeout=10)
        except requests.RequestException:
            continue

        if r.status_code != 200:
            continue  # folder doesn't exist under this exact casing -- try the next candidate

        try:
            items = r.json()
        except ValueError:
            continue
        if not isinstance(items, list):
            continue  # this path pointed to a file, not a folder -- skip

        for item in items:
            if item.get("type") == "file" and FILENAME_PATTERN.match(item.get("name", "")):
                found_path = f"{folder}/{item['name']}" if folder else item["name"]
                commit_date, err = get_last_commit_date(owner, repo, found_path, default_branch)

                if commit_date is None:
                    return "Rejected ❌", f"File found at '{default_branch}/{found_path}' but its commit date could not be verified. {err}"

                commit_str = commit_date.strftime('%d-%b-%Y %H:%M UTC')

                if commit_date <= DEADLINE:
                    return "Accepted ✅", f"File found at '{default_branch}/{found_path}', last committed {commit_str} — on time (deadline was {DEADLINE_STR})."
                else:
                    late_by = format_timedelta(commit_date - DEADLINE)
                    return "Rejected ❌", (
                        f"File found at '{default_branch}/{found_path}' but last committed {commit_str}, "
                        f"which is {late_by} after the deadline ({DEADLINE_STR}). Late submission."
                    )

    return "Rejected ❌", (
        f"No submission file found. Checked folders (docs/Docs/DOCS/doc/Doc/DOC/root) on branch "
        f"'{default_branch}' in '{owner}/{repo}' for any filename matching sprint[-_]?1.md (case-insensitive)."
    )


def process_row(index, repo_url):
    if not str(repo_url).startswith('http'):
        return index, "Rejected ❌", "RepoLink is missing or not a valid URL."
    status, reason = check_submission(repo_url)
    return index, status, reason


# Run checks in parallel across students -- this is the main speed fix.
results = {}
with ThreadPoolExecutor(max_workers=10) as executor:
    futures = [executor.submit(process_row, idx, row['RepoLink']) for idx, row in df.iterrows()]
    for future in as_completed(futures):
        idx, status, reason = future.result()
        results[idx] = (status, reason)

df['Status'] = df.index.map(lambda i: results[i][0])
df['Reason'] = df.index.map(lambda i: results[i][1])

df.to_csv('submissions.csv', index=False)
print(df[['Name', 'RollNo', 'Status', 'Reason']].to_string(index=False))
