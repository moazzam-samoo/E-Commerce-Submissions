import re
import pandas as pd
import requests

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
    url = url.strip().rstrip("/")
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(\.git)?$", url)
    if not match:
        return None
    return match.group(1), match.group(2)


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
                if r.status_code == 200 and len(r.text.strip()) > 0:
                    return f"Submitted ✅ ({branch}/{path})"
            except requests.RequestException:
                continue
    return "Not Submitted ❌"


for index, row in df.iterrows():
    repo = str(row['RepoLink'])
    if not repo.startswith("http"):
        df.at[index, 'Status'] = "Repo Link Error ❌"
        continue
    df.at[index, 'Status'] = check_submission(repo)

df.to_csv('submissions.csv', index=False)
print(df)
