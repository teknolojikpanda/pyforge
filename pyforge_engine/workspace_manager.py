import shutil
from pathlib import Path
from .logger_setup import logger

WORKSPACES_DIR = Path(__file__).resolve().parent.parent / "data" / "workspaces"
WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)

class WorkspaceManager:
    def create_workspace(self, job_name: str, build_id: str) -> Path:
        ws_path = WORKSPACES_DIR / job_name / build_id
        if ws_path.exists():
            logger.warning(f"Workspace {ws_path} already exists. Cleaning up.")
            self.cleanup_workspace(ws_path)
        ws_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Created workspace: {ws_path}")
        return ws_path

    def cleanup_workspace(self, workspace_path: Path):
        if workspace_path.exists() and workspace_path.is_dir():
            try:
                shutil.rmtree(workspace_path)
                logger.info(f"Cleaned up workspace: {workspace_path}")
            except Exception as e:
                logger.error(f"Error cleaning up workspace {workspace_path}: {e}")
        else:
            logger.warning(f"Workspace {workspace_path} not found or not a directory.")
