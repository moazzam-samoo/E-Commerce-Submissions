import base64
import binascii
import csv
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests


INPUT_FILE = Path("submissions_first_three_columns.csv")
REQUIRED_PATH = "docs/SPRINT_1.md"
REPORT_FILE = Path("submission_check_results.md")
MAX_WORKERS = 5

TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
SESSION = requests.Session()
if TOKEN:
    SESSION.headers.update({"Authorization": f"Bearer {TOKEN}"})


def request_with_retry(url, params=None, timeout=15, max_retries=4):
    last_response = None
    for attempt in range(max_retries):
        try:
            response = SESSION.get(url, params=params, timeout=timeout)
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
            continue

        last_response = response
        if response.status_code in (403, 429) and (
            response.status_code == 429 or "rate limit" in response.text.lower()
        ):
            if attempt < max_retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
        return response
    return last_response


def parse_github_repo(url):
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", str(url).strip())
    return match.groups() if match else None


def check_repository_public_fallback(owner, repo):
    remote_url = f"https://github.com/{owner}/{repo}.git"
    try:
        remote = subprocess.run(
            ["git", "ls-remote", "--symref", remote_url, "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "Error", "Could not contact the public repository with git."

    if remote.returncode != 0:
        return "Missing", "Repository not found or not publicly accessible."

    branch_match = re.search(r"^ref:\s+refs/heads/([^\s]+)\s+HEAD$", remote.stdout, re.MULTILINE)
    default_branch = branch_match.group(1) if branch_match else "main"
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{default_branch}/{REQUIRED_PATH}"
    file_response = request_with_retry(raw_url)

    if file_response is None:
        return "Error", "No response while reading the required file from the public repository."
    if file_response.status_code == 404:
        return "Missing", f"Required file not found at `{REQUIRED_PATH}`."
    if file_response.status_code != 200:
        return "Error", f"Could not inspect `{REQUIRED_PATH}` (HTTP {file_response.status_code})."
    if not file_response.text.strip():
        return "Invalid", f"`{REQUIRED_PATH}` exists but is empty."
    return "Submitted", f"Found non-empty `{REQUIRED_PATH}` on branch `{default_branch}`."


def check_repository(repo_url):
    parsed = parse_github_repo(repo_url)
    if not parsed:
        return "Invalid", "Invalid GitHub repository URL."

    owner, repo = parsed
    return check_repository_public_fallback(owner, repo)


def process_row(index, row):
    status, reason = check_repository(row["RepoLink"])
    return index, status, reason


def markdown_cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def main():
    with INPUT_FILE.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))

    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_row, index, row) for index, row in enumerate(rows)]
        for future in as_completed(futures):
            index, status, reason = future.result()
            results[index] = status, reason

    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report_lines = [
        "# Sprint Submission Check Results",
        "",
        f"- Checked at: {checked_at}",
        f"- Source: `{INPUT_FILE}`",
        f"- Required file: `{REQUIRED_PATH}`",
        "- Validation: file exists at the exact path and contains non-whitespace content",
        "",
        "| Name | Roll No. | Repository | Result | Details |",
        "| --- | --- | --- | --- | --- |",
    ]

    for index, row in enumerate(rows):
        status, reason = results[index]
        report_lines.append(
            f"| {markdown_cell(row['Name'])} | {markdown_cell(row['RollNo'])} | "
            f"[{markdown_cell(row['RepoLink'])}]({row['RepoLink']}) | {status} | {markdown_cell(reason)} |"
        )

    statuses = [result[0] for result in results.values()]
    report_lines.extend([
        "",
        "## Summary",
        "",
        f"- Total checked: {len(rows)}",
        f"- Submitted: {statuses.count('Submitted')}",
        f"- Missing: {statuses.count('Missing')}",
        f"- Invalid: {statuses.count('Invalid')}",
        f"- Errors: {statuses.count('Error')}",
        "",
    ])
    REPORT_FILE.write_text("\n".join(report_lines), encoding="utf-8")
    print(f"Wrote {REPORT_FILE} with {len(rows)} repository results.")


if __name__ == "__main__":
    main()
