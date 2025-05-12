import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Any
import datetime

class BuildStatus(Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    ABORTED = "ABORTED"

@dataclass
class SCMDataConfig: # Renamed to avoid conflict with job.SCM
    type: str
    url: str
    branch: str = "main"
@dataclass
class TriggerDataConfig: # Renamed
    type: str
    cron: str = None # For scm_poll or scheduled triggers

@dataclass
class StepDataConfig: # Renamed
    name: str
    script: str
    shell: str = "bash" # or "cmd" or "powershell"
    env: Dict[str, str] = field(default_factory=dict)
    ignore_errors: bool = False

@dataclass
class ArtifactConfig:
    path: str # Path pattern relative to workspace
    destination: str # Path relative to the global artifacts directory

@dataclass
class JobDataModel: # Renamed
    name: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    scm: SCMDataConfig = None
    triggers: List[TriggerDataConfig] = field(default_factory=list)
    steps: List[StepDataConfig] = field(default_factory=list)
    artifacts: List[ArtifactConfig] = field(default_factory=list)
    parameters: Dict[str, Any] = field(default_factory=dict) # For parameterized builds
    raw_config: Dict[str, Any] = field(default_factory=dict) # Store original YAML

@dataclass
class BuildDataRecord: # Renamed
    job_id: str
    job_name: str # Denormalized for convenience
    build_number: int
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: BuildStatus = BuildStatus.PENDING
    start_time: datetime.datetime = None
    end_time: datetime.datetime = None
    triggered_by: str = "manual" # or "scm_poll", "webhook"
    commit_hash: str = None # If SCM based
    logs: List[str] = field(default_factory=list)
    workspace_path: str = None
    artifact_paths: Dict[str, str] = field(default_factory=dict) # {original_path: archived_path}
