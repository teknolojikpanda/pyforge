import yaml
from pathlib import Path
from typing import Dict, List, Optional

from .job import Job, SCM, Step, Trigger, Artifact 
from .logger_setup import logger

JOBS_CONFIG_DIR_NAME = "jobs_config" 

class JobManager:
    def __init__(self, jobs_config_dir: Optional[Path] = None):
        if jobs_config_dir:
            self.jobs_config_dir = jobs_config_dir
        else:
            self.jobs_config_dir = Path(__file__).resolve().parent.parent / JOBS_CONFIG_DIR_NAME
        
        self.jobs: Dict[str, Job] = {}
        self.load_jobs()

    def load_jobs(self):
        self.jobs = {}
        logger.info(f"Loading jobs from {self.jobs_config_dir}...")
        if not self.jobs_config_dir.exists() or not self.jobs_config_dir.is_dir():
            logger.warning(f"Jobs config directory not found or is not a directory: {self.jobs_config_dir}")
            return

        for config_file in self.jobs_config_dir.glob("*.yaml"):
            try:
                job = self._parse_job_config(config_file)
                if job:
                    if job.name in self.jobs:
                        logger.warning(f"Duplicate job name '{job.name}' found in {config_file.name}. Overwriting previous definition.")
                    self.jobs[job.name] = job
                    logger.debug(f"Successfully loaded job: {job.name} from {config_file.name}")
            except Exception as e:
                logger.error(f"Failed to load job from {config_file.name}: {e}", exc_info=True)
        logger.info(f"Loaded {len(self.jobs)} jobs.")

    def _parse_job_config(self, config_file: Path) -> Optional[Job]:
        try:
            with open(config_file, 'r') as f:
                raw_yaml_content = f.read()
            
            # Use the Job.from_yaml classmethod, passing the raw content
            job = Job.from_yaml(config_file, raw_yaml_content)
            return job

        except ValueError as ve: # Catch specific error from Job.from_yaml
            logger.error(f"Validation error parsing job config {config_file.name}: {ve}")
            return None
        except yaml.YAMLError as ye:
            logger.error(f"YAML syntax error in job config {config_file.name}: {ye}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error parsing job config {config_file.name}: {e}", exc_info=True)
            return None

    def get_job(self, name: str) -> Optional[Job]:
        return self.jobs.get(name)

    def list_jobs(self) -> List[Job]:
        return list(self.jobs.values())

    def reload_jobs(self):
        """Explicitly reloads all job configurations."""
        logger.info("Reloading all job configurations...")
        self.load_jobs()