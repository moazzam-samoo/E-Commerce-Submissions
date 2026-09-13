import os
import re
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests

# ---- Deadline configuration ----
DEADLINE = datetime(2026, 9, 13, 23, 59, 59, tzinfo=timezone.utc)
DEADLINE_STR = DEADLINE.strftime('%d-%b-%Y %H:%M UTC')

df = pd.read_csv('submissions.csv')

# Folders we consider plausible for the submission file to live in.
FOLDER_CANDIDATES = ["docs", "Docs", "DOCS", "doc", "Doc", "DOC", ""]  # "" = repo root

# Case-insensitive pattern matching the filename itself
FILENAME_PATTERN = re.compile(r"^sprint[-_]?0?1\.md$", re.IGNORECASE)

# GitHub Actions automatically provides GITHUB_TOKEN in every workflow run.
# Using it raises the API rate limit from 60/hour to 5000/hour -- no manual setup needed.
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

SESSION = requests.Session()
if TOKEN:
    SESSION.headers.update({"Authorization": f"token {TOKEN}"})


def request_with_retry(url, params=None, timeout=10, max_retries=4):
    """GET with retry/backoff so transient rate-limit hits don't wrongly fail a student."""
    last_response = None
    for attempt in range(max_retries):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
            continue

        last_response = r
        if r.status_code == 403 and "rate limit" in r.text.lower():
            if attempt < max_retries - 1:
                time.sleep(3 * (attempt + 1))  # back off and try again
                continue
        return r
    return last_response


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
    r = request_with_retry(api_url, params={"path": path, "sha": branch, "per_page": 1})

    if r is None:
        return None, "Network error while fetching commit history (no response after retries)."
    if r.status_code == 403:
        return None, "GitHub API rate limit exceeded while checking commit date, even after retries."
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

    repo_check = request_with_retry(f"https://api.github.com/repos/{owner}/{repo}")
    if repo_check is None:
        return "Rejected ❌", f"Network error while verifying repository '{owner}/{repo}' (no response after retries)."
    if repo_check.status_code == 404:
        return "Rejected ❌", f"Repository '{owner}/{repo}' not found on GitHub — it may be private, deleted, renamed, or the link contains a typo."
    if repo_check.status_code == 403:
        return "Rejected ❌", "GitHub API rate limit exceeded while verifying repository, even after retries."
    if repo_check.status_code != 200:
        return "Rejected ❌", f"GitHub API returned unexpected status {repo_check.status_code} while verifying repository '{owner}/{repo}'."

    default_branch = repo_check.json().get("default_branch", "main")

    # Search every candidate folder (including repo root) for a matching filename.
    for folder in FOLDER_CANDIDATES:
        contents_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{folder}"
        r = request_with_retry(contents_url, params={"ref": default_branch})

        if r is None or r.status_code != 200:
            continue  # this folder doesn't exist under this casing -- try the next candidate

        try:
            items = r.json()
        except ValueError:
            continue
        if not isinstance(items, list):
            continue

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


# Moderate concurrency -- fast, but low enough to avoid GitHub's secondary abuse-detection limits.
MAX_WORKERS = 5

results = {}
with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = [executor.submit(process_row, idx, row['RepoLink']) for idx, row in df.iterrows()]
    for future in as_completed(futures):
        idx, status, reason = future.result()
        results[idx] = (status, reason)

df['Status'] = df.index.map(lambda i: results[i][0])
df['Reason'] = df.index.map(lambda i: results[i][1])

df.to_csv('submissions.csv', index=False)
print(df[['Name', 'RollNo', 'Status', 'Reason']].to_string(index=False))
if not TOKEN:
    print("\nWARNING: No GITHUB_TOKEN found in environment -- running at the 60 req/hour unauthenticated limit.")
