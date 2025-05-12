import shutil
import glob
from pathlib import Path
from .models import ArtifactConfig
from .logger_setup import logger

ARTIFACTS_ROOT_DIR = Path(__file__).resolve().parent.parent / "data" / "artifacts"
ARTIFACTS_ROOT_DIR.mkdir(parents=True, exist_ok=True)

class ArtifactManager:
    def archive_artifacts(self, workspace_path: Path, job_name: str, build_id: str, artifact_configs: list[ArtifactConfig]) -> dict:
        archived_files = {}
        if not artifact_configs:
            return archived_files

        build_artifact_dir = ARTIFACTS_ROOT_DIR / job_name / build_id
        build_artifact_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Archiving artifacts for {job_name}/{build_id} to {build_artifact_dir}")

        for config in artifact_configs:
            source_pattern = workspace_path / config.path
            destination_subdir = build_artifact_dir / (config.destination or "") # Handle empty destination
            destination_subdir.mkdir(parents=True, exist_ok=True)

            found_files = glob.glob(str(source_pattern), recursive=True)
            if not found_files:
                logger.warning(f"No files found for artifact pattern: {source_pattern}")
                continue

            for src_file_path_str in found_files:
                src_file_path = Path(src_file_path_str)
                if src_file_path.is_file():
                    try:
                        # Determine the base path from the pattern to preserve structure.
                        # This approach assumes the pattern starts with non-glob parts.
                        # For a pattern like "dist/**/*.zip", source_pattern_base_on_workspace would be "workspace_path/dist"
                        # For a pattern like "*.log", source_pattern_base_on_workspace would be "workspace_path"
                        pattern_path_obj = Path(config.path)
                        glob_base_parts = []
                        # Iterate through parts of the pattern until a glob character is found
                        for part in pattern_path_obj.parts:
                            if '*' in part or '?' in part or '[' in part:
                                break
                            glob_base_parts.append(part)
                        
                        source_pattern_base_on_workspace = workspace_path
                        if glob_base_parts:
                            source_pattern_base_on_workspace = workspace_path / Path(*glob_base_parts)
                        
                        # Get the file path relative to this determined base
                        relative_file_path_to_pattern_base = src_file_path.relative_to(source_pattern_base_on_workspace)
                        
                        # Construct destination path: destination_subdir / relative_file_path_to_pattern_base
                        dest_file_path = destination_subdir / relative_file_path_to_pattern_base

                        dest_file_path.parent.mkdir(parents=True, exist_ok=True) # Ensure parent dir exists
                        shutil.copy2(src_file_path, dest_file_path)
                        logger.info(f"Archived {src_file_path} to {dest_file_path}")
                        # Store path relative to ARTIFACTS_ROOT_DIR for easy retrieval
                        archived_files[str(src_file_path.relative_to(workspace_path))] = str(dest_file_path.relative_to(ARTIFACTS_ROOT_DIR))
                    except Exception as e:
                        logger.error(f"Failed to archive {src_file_path}: {e}")
                elif src_file_path.is_dir() and config.path.endswith('/'): # or some other indicator for dirs
                    logger.warning(f"Archiving directories ({src_file_path}) is not fully supported yet. Only copying files within.")

        return archived_files
