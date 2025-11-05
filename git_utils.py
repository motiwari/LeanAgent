import numpy as np
import json
import re
import shutil
import subprocess
import requests
import generate_benchmark_lean4
from lean_dojo import LeanGitRepo
from datetime import datetime
import lean_dojo
from lean_dojo.data_extraction.cache import _split_git_url
from collections import defaultdict
from dynamic_database import Repository, DynamicDatabase, Theorem



from loguru import logger
from typing import Union, List, Tuple
import math
import os

from constants import known_repositories, known_dead_repos, PR_TITLE, PR_BODY, TMP_BRANCH, COMMIT_MESSAGE

personal_access_token = os.environ.get("GITHUB_ACCESS_TOKEN")
BATCH_SIZE = 4
from filenames import REPO_DIR, DATA_DIR


def clone_repo(repo_url):
    """Clone a git repository and return the path to the repository and its sha."""
    repo_name = os.path.join(*_split_git_url(repo_url)).replace(".git", "")
    
    logger.info(f"Repo name: {repo_name}")
    
    repo_name = os.path.join(REPO_DIR, repo_name)
    if os.path.exists(repo_name):
        print(f"Repository already exists in directory: {repo_name}")
        process = subprocess.Popen(
            ["git", "-C", repo_name, "rev-parse", "HEAD"], stdout=subprocess.PIPE
        )
        stdout, _stderr = process.communicate()
    else:
        logger.info(f"Cloning {repo_url} from scratch")
        subprocess.run(["git", "clone", repo_url, repo_name])
        process = subprocess.Popen(["git", "ls-remote", repo_url], stdout=subprocess.PIPE)
        stdout, _stderr = process.communicate()
    
    sha = re.split(r"\t+", stdout.decode("utf-8"))[0]
    sha = sha.strip()
    print("Sha is " + sha)
    return repo_name, sha



def branch_exists(repo_name, branch_name):
    """Check if a branch exists in a git repository."""
    proc = subprocess.run(
        ["git", "-C", repo_name, "branch", "-a"], stdout=subprocess.PIPE, text=True
    )
    branches = proc.stdout.split("\n")
    local_branch = branch_name
    remote_branch = f"remote/{branch_name}"
    return any(
        branch.strip().endswith(local_branch) or branch.strip().endswith(remote_branch)
        for branch in branches
    )


def create_or_switch_branch(repo_name, branch_name, base_branch):
    """Create a branch in a git repository if it doesn't exist, or switch to it if it does."""
    if not branch_exists(repo_name, branch_name):
        subprocess.run(
            ["git", "-C", repo_name, "checkout", "-b", branch_name], check=True
        )
    else:
        subprocess.run(["git", "-C", repo_name, "checkout", branch_name], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                repo_name,
                "merge",
                base_branch,
                "-m",
                f"Merging {branch_name} into {base_branch}",
            ],
            check=True,
        )


def commit_changes(repo_name, commit_message):
    """Commit changes to a git repository."""
    status = subprocess.run(
        ["git", "-C", repo_name, "status", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if status == "":
        print("No changes to commit.")
        return False
    subprocess.run(["git", "-C", repo_name, "add", "."], check=True)
    subprocess.run(["git", "-C", repo_name, "commit", "-m", commit_message], check=True)
    return True


def push_changes(repo_name, branch_name):
    """Push changes to a git repository."""
    subprocess.run(
        ["git", "-C", repo_name, "push", "-u", "origin", branch_name], check=True
    )


def get_default_branch(repo_full_name):
    """Get the default branch of a repository (default `main`)."""
    url = f"https://api.github.com/repos/{repo_full_name}"
    headers = {
        "Authorization": f"token {personal_access_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()["default_branch"]
    else:
        logger.info(f"Failed to get default branch for {repo_full_name}")
        return "main"


def create_pull_request(repo_full_name, title, body, head_branch):
    """Create a pull request in a repository."""
    base_branch = get_default_branch(repo_full_name)
    url = f"https://api.github.com/repos/{repo_full_name}/pulls"
    headers = {
        "Authorization": f"token {personal_access_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    data = {"title": title, "body": body, "head": head_branch, "base": base_branch}
    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 201:
        print("Pull request created successfully: " + response.json()["html_url"])
        return response.json()["html_url"]
    else:
        print("Failed to create pull request", response.text)
        return ""

def ensure_inside_git():
    """Ensure that the current directory is inside a git repository."""
    try:
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("Already in a Git repository")
    except subprocess.CalledProcessError:
        logger.info("Not in a Git repository. Initializing one.")
        subprocess.run(["git", "init"], check=True)
        
def get_compatible_commit(url):
    """Find the most recent commit with a Lean version that LeanAgent supports."""
    if "mathlib4" in url or "SciLean" in url or "pfr" in url:
        if "mathlib4" in url:
            sha = "2b29e73438e240a427bcecc7c0fe19306beb1310"
            v = "v4.8.0"
        elif "SciLean" in url:
            sha = "22d53b2f4e3db2a172e71da6eb9c916e62655744"
            v = "v4.7.0"
        elif "pfr" in url:
            sha = "fa398a5b853c7e94e3294c45e50c6aee013a2687"
            v = "v4.8.0-rc1"
        return sha, v
    else:
        with open(os.path.join("RAID", "data", "repo_info_compatible.json"), "r") as f:
            try:
                repos_and_compatible_commits = json.load(f)
            except json.JSONDecodeError:
                repos_and_compatible_commits = []
        
        if url in [repo["url"] + ".git" for repo in repos_and_compatible_commits if repo["commit"]]:
            logger.info(f"Repository {url} already has a compatible commit.")
            repo = [repo for repo in repos_and_compatible_commits if repo["url"] + ".git" == url][0]
            return repo["commit"], repo["version"]
            
        try:
            process = subprocess.Popen(["git", "ls-remote", url], stdout=subprocess.PIPE)
            stdout, stderr = process.communicate()
            latest_commit = re.split(r"\t+", stdout.decode("utf-8"))[0]
            logger.info(f"Latest commit: {latest_commit}")

            new_url = url.replace(".git", "")
            logger.info(f"Creating LeanGitRepo for {new_url}")
            
            repo = LeanGitRepo(new_url, latest_commit)
            logger.info(f"Getting config for {url}")
            
            config = repo.get_config("lean-toolchain")
            v = generate_benchmark_lean4.get_lean4_version_from_config(config["content"])
            
            if generate_benchmark_lean4.is_supported_version(v):
                logger.info(f"Latest commit compatible for url {url}")
                return latest_commit, v

            logger.info(f"Searching for compatible commit for {url}")
            
            ensure_inside_git()
            process = subprocess.Popen(
                ["git", "fetch", "--depth=1000000", url],  # Fetch commits
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            
            logger.info(f"Fetching commits for {url}")
            _, stderr = process.communicate()
            
            if process.returncode != 0:
                raise Exception(f"Git fetch command failed: {stderr.decode('utf-8')}")
            
            logger.info(f"Fetched commits for {url}")
            
            process = subprocess.Popen(
                ["git", "log", "--format=%H", "FETCH_HEAD"],  # Get list of commits
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            
            logger.info(f"Getting list of commits for {url}")
            
            stdout, stderr = process.communicate()
            if process.returncode != 0:
                raise Exception(f"Git log command failed: {stderr.decode('utf-8')}")
            
            commits = stdout.decode("utf-8").strip().split("\n")
            logger.info(f"Found {len(commits)} commits for {url}")
            
            new_url = url.replace(".git", "")
            
            repo_human_name = "/".join(new_url.split("/")[-2:])
            
            # Delete repo if it exists, because it might be checked out to a different commit
            if os.path.exists(os.path.join("repos", repo_human_name)):
                logger.info(f"CAREFUL: Deleting existing repo at {os.path.join('repos', repo_human_name)}")
                shutil.rmtree(os.path.join("repos", repo_human_name))
            
            subprocess.run(["git", "clone", url, os.path.join("repos", repo_human_name)], check=True)
            for commit in commits:
                logger.info(f"Checking commit {commit} for {url}")
                # Check out the commit locally
                subprocess.run(["git", "-C", os.path.join("repos", repo_human_name), "checkout", commit], capture_output=False, check=True)
                
                # Check the lean-toolchain file manually, avoid calling LeanGitRepo because it makes a lot of web requests
                with open(os.path.join("repos", repo_human_name, "lean-toolchain"), "r") as f:
                    config_content = f.read()
                
                v = generate_benchmark_lean4.get_lean4_version_from_config(config_content)
                if generate_benchmark_lean4.is_supported_version(v):
                    logger.info(f"Found compatible commit {commit} for {url}")
                    repos_and_compatible_commits.append({"url": url.replace(".git", ""), "commit": commit, "version": v})
                    with open(os.path.join(DATA_DIR, "repo_info_compatible.json"), "w") as f:
                        json.dump(repos_and_compatible_commits, f, indent=2)
                        f.flush()
                        
                    return commit, v
            raise Exception("No compatible commit found")
        except Exception as e:
            logger.info(f"Error in get_compatible_commit: {str(e)}")
            return None, None


def find_and_save_compatible_commits(repo_info_file, lean_git_repos):
    """Finds and saves compatible commits for various repositories"""
    for repo in lean_git_repos:
        url = repo.url
        if not url.endswith(".git"):
            url = url + ".git"

        # Saves the compatible commit in repo_info_file
        _sha, _v = get_compatible_commit(url)
        
    with open(repo_info_file, "r") as repos_and_compatible_commits_f:
        updated_repos = json.load(repos_and_compatible_commits_f)
        
    return updated_repos


def search_github_repositories(lean_git_repos, repos, language="Lean", num_repos=10):
    """Search for the given number of repositories on GitHub that have the given language."""
    headers = {"Authorization": personal_access_token}
    query_params = {
        "q": f"language:{language}",
        "sort": "stars",  # What can this be?
        "order": "desc",
        "per_page": 100,
    }

    cloned_count = 0
    page = 1

    while cloned_count < num_repos:
        query_params["page"] = page
        response = requests.get(
            "https://api.github.com/search/repositories",
            headers=headers,
            params=query_params,
        )

        if response.status_code == 200:
            repositories = response.json()["items"]
            for repo in repositories:
                if cloned_count >= num_repos:
                    break
                
                repo_full_name = repo["full_name"]
                print("\n\n")
                logger.info(f"Processing {repo_full_name}")
                
                
                # Skip repos that are already known
                if repo_full_name not in known_repositories + known_dead_repos + repos:
                    print("\n\n")
                    logger.info(f"Processing new repo: {repo_full_name}")
                    name = None
                    try:
                        clone_url = repo["clone_url"]
                        repo_name, sha = clone_repo(clone_url)
                        name = repo_name
                        url = clone_url.replace(".git", "")
                        # TODO: This constructor can be very slow
                        lean_git_repo = LeanGitRepo(url, sha)
                        
                        lean_git_repos.append(lean_git_repo)
                        repos.append(repo_full_name)
                        cloned_count += 1
                        logger.info(f"Cloned {repo_full_name}")
                    except Exception as e:
                        logger.info(f"CAREFUL: Deleting existing repo at {os.path.join('repos', repo_full_name)}")
                        shutil.rmtree(name)
                        logger.info(f"Failed to clone {repo_full_name} because of {e}")
                else:
                    logger.info(
                        f"Skipping {repo_full_name} since it is a known repository"
                    )
            page += 1
        else:
            logger.info("Failed to search GitHub", response.status_code)
            break

        # Check if we've reached the end of the search results
        if len(repositories) < 100:
            break

    logger.info(f"Total repositories processed: {cloned_count}")
    return lean_git_repos, repos


def add_repo_to_database(dynamic_database_json_path, repo, db):
    """Adds a repository to the dynamic database."""
    # Prepare the data necessary to add this repo to the dynamic database
    url = repo.url
    if not url.endswith(".git"):
        url = url + ".git"
    logger.info(f"\n\nProcessing {url}")

    sha, v = get_compatible_commit(url)

    if not sha:
        logger.info(f"Failed to find a compatible commit for {url}")
        return None

    logger.info(f"Found compatible commit {sha} for {url} with lean version: {v}")
    
    # Ensure that the repo is checked out to the compatible commit
    repo_name, _ = clone_repo(url)
    subprocess.run(["git", "-C", repo_name, "checkout", sha], check=True)
    logger.info(f"Checked out {url} to commit {sha}")
    
    
    url = url.replace(".git", "")
    repo = LeanGitRepo(url, sha)
    dir_name = repo.url.split("/")[-1] + "_" + sha
    dst_dir = os.path.join(DATA_DIR, dir_name)
    logger.info(f"Generating benchmark at {dst_dir}")
    
    traced_repo, _, _, total_theorems = generate_benchmark_lean4.main(
        repo.url, sha, dst_dir
    )
    
    if not traced_repo:
        logger.info(f"Failed to trace {url}")
        return None
    
    if total_theorems < 3 * BATCH_SIZE:  # Require enough theorems for train/val/test
        logger.info(f"Not enough theorems found in {url}")
        return None
    
    logger.info(f"Finished generating benchmark at {dst_dir}")

    # Add the new repo to the dynamic database
    config = repo.get_config("lean-toolchain")
    v = generate_benchmark_lean4.get_lean4_version_from_config(config["content"])
    theorems_folder = os.path.join(dst_dir, "theorems")
    premise_files_corpus = os.path.join(dst_dir, "corpus.jsonl")
    files_traced = os.path.join(dst_dir, "traced_files.jsonl")
    
    pr_url = None
    data = {
        "url": repo.url,
        "name": "/".join(repo.url.split("/")[-2:]),
        "commit": repo.commit,
        "lean_version": v,
        "lean_dojo_version": lean_dojo.__version__,
        "metadata": {
            "date_processed": datetime.datetime.now(),
        },
        "theorems_folder": theorems_folder,
        "premise_files_corpus": premise_files_corpus,
        "files_traced": files_traced,
        "pr_url": pr_url,
    }

    repo = Repository.from_dict(data)
    logger.info("Before adding new repo:")
    db.print_database_contents()
    
    
    logger.info("After adding new repo:")
    db.add_repository(repo)
    db.print_database_contents()
    
    db.to_json(dynamic_database_json_path)
    return "Done"

def calculate_difficulty(theorem: Theorem) -> Union[float, None]:
    """Calculates the difficulty of a theorem."""
    proof_steps = theorem.traced_tactics
    if any("sorry" in step.tactic for step in proof_steps):
        return float("inf")  # Hard (no proof)
    if len(proof_steps) == 0:
        return None  # To be distributed later
    return math.exp(len(proof_steps))

def categorize_difficulty(
    difficulty: Union[float, None], percentiles: List[float]
) -> str:
    """Categorizes the difficulty of a theorem."""
    if difficulty is None:
        return "To_Distribute"
    if difficulty == float("inf"):
        return "Hard (No proof)"
    elif difficulty <= percentiles[0]:
        return "Easy"
    elif difficulty <= percentiles[1]:
        return "Medium"
    else:
        return "Hard"


def sort_repositories_by_difficulty(db: DynamicDatabase) -> List[Repository]:
    """Sorts repositories by the difficulty of their theorems."""
    difficulties_by_repo = defaultdict(list)
    all_difficulties = []

    print("Ready to calculate difficulties of all theorems")
    for repo in db.repositories:
        print(f"Starting {repo.name}")
        for theorem in repo.get_all_theorems:
            difficulty = calculate_difficulty(theorem)
            theorem.difficulty_rating = difficulty
            difficulties_by_repo[repo].append(
                (
                    theorem.full_name,
                    str(theorem.file_path),
                    tuple(theorem.start),
                    tuple(theorem.end),
                    difficulty,
                )
            )
            if difficulty is not None:
                all_difficulties.append(difficulty)

        db.update_repository(repo)
        print(f"Finished {repo.name}")

    if len(all_difficulties) == 0:
        from loguru import logger
        logger.warning("No theorem difficulties found; skipping difficulty bucketing.")
        return []
    percentiles = np.percentile(all_difficulties, [33, 67])

    categorized_theorems = defaultdict(lambda: defaultdict(list))

    print("Ready to categorize theorems")
    for repo, theorems in difficulties_by_repo.items():
        print(f"Starting {repo.name}")
        for theorem_name, file_path, start, end, difficulty in theorems:
            category = categorize_difficulty(difficulty, percentiles)
            categorized_theorems[repo][category].append(
                (theorem_name, file_path, start, end, difficulty)
            )
        print(f"Finished {repo.name}")

    print("Distributed theorems with no proofs")
    for repo in categorized_theorems:
        print(f"Starting {repo.name}")
        to_distribute = categorized_theorems[repo]["To_Distribute"]
        chunk_size = len(to_distribute) // 3
        for i, category in enumerate(["Easy", "Medium", "Hard"]):
            start = i * chunk_size
            end = start + chunk_size if i < 2 else None
            categorized_theorems[repo][category].extend(to_distribute[start:end])
        del categorized_theorems[repo]["To_Distribute"]
        print(f"Finished {repo.name}")

    # Sort repositories based on the number of easy theorems
    sorted_repos = sorted(
        categorized_theorems.keys(),
        key=lambda r: len(categorized_theorems[r]["Easy"]),
        reverse=True,
    )

    return sorted_repos, categorized_theorems, percentiles


def save_sorted_repos(sorted_repos: List[Repository], file_path: str):
    """Saves the sorted repositories to a file."""
    sorted_repo_data = [
        {"url": repo.url, "commit": repo.commit, "name": repo.name}
        for repo in sorted_repos
    ]
    with open(file_path, "w") as f:
        json.dump(sorted_repo_data, f, indent=2)


def load_sorted_repos(file_path: str) -> List[Tuple[str, str, str]]:
    """Loads the sorted repositories from a file."""
    with open(file_path, "r") as f:
        sorted_repo_data = json.load(f)
    return [(repo["url"], repo["commit"], repo["name"]) for repo in sorted_repo_data]


def write_skip_file(repo_url):
    """Writes a repository URL to a file to skip it."""
    skip_file_path = os.path.join(DATA_DIR, "skip_repo.txt")
    with open(skip_file_path, "w") as f:
        f.write(repo_url)


def should_skip_repo():
    """Checks if a repository should be skipped."""
    skip_file_path = os.path.join(DATA_DIR, "skip_repo.txt")
    if os.path.exists(skip_file_path):
        with open(skip_file_path, "r") as f:
            repo_url = f.read().strip()
        return True, repo_url
    return False, None