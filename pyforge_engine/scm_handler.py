import git
from pathlib import Path
from typing import Optional
from .logger_setup import logger

class SCMHandler:
    def __init__(self, repo_url: str, branch: str = 'main'):
        self.repo_url = repo_url
        self.branch = branch

    def clone_or_update(self, target_dir: Path) -> Optional[str]:
        """Clones repo if target_dir is empty, otherwise pulls updates."""
        try:
            if not target_dir.exists() or not any(target_dir.iterdir()):
                logger.info(f"Cloning {self.repo_url} (branch: {self.branch}) into {target_dir}...")
                repo = git.Repo.clone_from(self.repo_url, target_dir, branch=self.branch)
                logger.info(f"Clone complete.")
            else:
                logger.info(f"Updating existing repo in {target_dir}...")
                repo = git.Repo(target_dir)
                origin = repo.remotes.origin
                origin.fetch()
                # A bit more robust pull: checkout branch, reset hard to origin/branch
                repo.git.checkout(self.branch)
                repo.git.reset('--hard', f'origin/{self.branch}')
                logger.info(f"Update complete.")
            
            return repo.head.commit.hexsha
        except git.exc.GitCommandError as e:
            logger.error(f"Git command error for {self.repo_url} in {target_dir}: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to clone/update {self.repo_url}: {e}")
            return None

    def get_latest_commit_hash(self, local_repo_path: Optional[Path] = None) -> Optional[str]:
        """
        Gets the latest commit hash from the remote repository.
        If local_repo_path is provided and exists, it updates it first.
        Otherwise, it does a shallow clone to a temporary location.
        """
        try:
            if local_repo_path and local_repo_path.exists():
                repo = git.Repo(local_repo_path)
                origin = repo.remotes.origin
                origin.fetch() # Ensure we have the latest refs
                remote_ref = origin.refs[self.branch]
                return remote_ref.commit.hexsha
            else:
                logger.info(f"Polling remote {self.repo_url} for branch {self.branch} using ls-remote.")
                g = git.cmd.Git()
                try:
                    # Example: git ls-remote --heads git://github.com/user/repo.git main
                    # Output: <hash>\trefs/heads/main
                    remote_output = g.ls_remote(self.repo_url, f"refs/heads/{self.branch}")
                    if remote_output:
                        # Expecting a single line for a specific branch
                        commit_hash = remote_output.split('\t')[0]
                        logger.debug(f"Latest commit hash from ls-remote for {self.repo_url} branch {self.branch}: {commit_hash}")
                        return commit_hash
                    logger.warning(f"No output from ls-remote for {self.repo_url} branch {self.branch}. Branch may not exist or repo is inaccessible.")
                    return None
                except git.exc.GitCommandError as e:
                    logger.error(f"Git ls-remote command error for {self.repo_url} branch {self.branch}: {e}")
                    return None

        except Exception as e:
            logger.error(f"Error getting latest commit for {self.repo_url} (branch {self.branch}): {e}")
            return None
