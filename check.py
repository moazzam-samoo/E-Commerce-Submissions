import re
from datetime import datetime, timezone
import pandas as pd
import requests

# ---- Deadline configuration ----
DEADLINE = datetime(2026, 9, 13, 23, 59, 59, tzinfo=timezone.utc)

df = pd.read_csv('submissions.csv')

BRANCHES = ["main", "master"]

# Generate every reasonable filename variant
NAME_WORDS = ["sprint", "Sprint", "SPRINT"]
SEPARATORS = ["_", "-", ""]
EXTENSIONS = [".md", ".MD"]
FILENAMES = {
    f"{word}{sep}1{ext}"
    for word in NAME_WORDS
    for sep in SEPARATORS
    for ext in EXTENSIONS
}
FOLDERS = ["docs/", "Docs/", "DOCS/", ""]
CANDIDATE_PATHS = sorted({f"{folder}{name}" for folder in FOLDERS for name in FILENAMES})


def parse_github_repo(url):
    url = str(url).strip().rstrip("/")
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(\.git)?$", url)
    if not match:
        return None
    return match.group(1), match.group(2)


def get_last_commit_date(owner, repo, path, branch):
    """Returns the datetime of the most recent commit that touched this file, or None on failure."""
    api_url = f"https://api.github.com/repos/{owner}/{repo}/commits"
    params = {"path": path, "sha": branch, "per_page": 1}
    try:
        r = requests.get(api_url, params=params, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data:
                date_str = data[0]["commit"]["committer"]["date"]  # e.g. 2026-09-10T14:23:00Z
                return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (requests.RequestException, KeyError, IndexError, ValueError):
        pass
    return None


def check_submission(repo_url):
    parsed = parse_github_repo(repo_url)
    if not parsed:
        return "Repo Link Error ❌"
    owner, repo = parsed

    for branch in BRANCHES:
        for path in CANDIDATE_PATHS:
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
            try:
                r = requests.get(raw_url, timeout=6)
            except requests.RequestException:
                continue

            if r.status_code == 200 and len(r.text.strip()) > 0:
                commit_date = get_last_commit_date(owner, repo, path, branch)

                if commit_date is None:
                    return f"Submitted ✅ but commit date unknown ({branch}/{path})"

                if commit_date <= DEADLINE:
                    return f"Accepted ✅ (committed {commit_date.strftime('%d-%b-%Y %H:%M UTC')})"
                else:
                    return f"Late ❌ Not Accepted (committed {commit_date.strftime('%d-%b-%Y %H:%M UTC')})"

    return "Not Submitted ❌"


for index, row in df.iterrows():
    repo = str(row['RepoLink'])
    if not repo.startswith("http"):
        df.at[index, 'Status'] = "Repo Link Error ❌"
        continue
    df.at[index, 'Status'] = check_submission(repo)

df.to_csv('submissions.csv', index=False)
print(df)
