from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from enum import Enum

class BuildExecutionStatus(Enum):
    PENDING = "PENDING"
    DISPATCHING = "DISPATCHING"
    WAITING_FOR_AGENT = "WAITING_FOR_AGENT"
    WAITING_FOR_AGENT_EXECUTOR = "WAITING_FOR_AGENT_EXECUTOR"
    RUNNING = "RUNNING" # Could be RUNNING_ON_AGENT or RUNNING_LOCALLY
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    ERROR = "ERROR" # System-level error, e.g., agent not found, dispatch failed
    CANCELLED = "CANCELLED"


@dataclass
class Build:
    id: str
    build_number: int # Sequential number for this job
    job_name: str
    status: BuildExecutionStatus
    start_time: str  # ISO 8601
    end_time: Optional[str] = None  # ISO 8601
    triggered_by: str = "manual"
    scm_commit_hash: Optional[str] = None
    error_message: Optional[str] = None
    log_file_path: Optional[str] = None # Path on server (for local builds or dispatch logs)
    artifact_paths: List[Dict[str, str]] = field(default_factory=list) # For local/server-stored artifacts: [{"name": "...", "path": "..."}]
    
    # Agent-specific fields
    agent_name: Optional[str] = None # Name of the agent that ran/is running the build
    agent_artifact_paths: List[Dict[str, str]] = field(default_factory=list) # Artifacts reported by agent: [{"name": "...", "path_on_agent": "...", "server_path": "optional_server_path_after_upload"}]
    step_details: List[Dict[str, Any]] = field(default_factory=list)
    shared_data: Dict[str, str] = field(default_factory=dict) # For passing variables between steps
    # Each dict in step_details:
    # {'name': str, 'language': str, 'status': str ('PENDING', 'RUNNING', 'SUCCESS', 'FAILURE', 'SKIPPED'),
    #  'start_time': Optional[str], 'end_time': Optional[str], 'error_message': Optional[str]}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "build_number": self.build_number,
            "job_name": self.job_name,
            "status": self.status.value, # Store enum value as string
            "start_time": self.start_time,
            "end_time": self.end_time,
            "triggered_by": self.triggered_by,
            "scm_commit_hash": self.scm_commit_hash,
            "error_message": self.error_message,
            "log_file_path": self.log_file_path,
            "artifact_paths": self.artifact_paths,
            "agent_name": self.agent_name,
            "agent_artifact_paths": self.agent_artifact_paths,
            "step_details": self.step_details,
            "shared_data": self.shared_data,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Build':
        build = cls(
            id=data['id'],
            build_number=data.get('build_number', 0), # Ensure build_number is passed
            job_name=data['job_name'],
            status=BuildExecutionStatus(data['status']), # Load string into enum
            start_time=data['start_time']
        )
        # The rest of the fields are set after initial construction
        build.end_time = data.get('end_time')
        build.triggered_by = data.get('triggered_by', "unknown")
        build.scm_commit_hash = data.get('scm_commit_hash')
        build.error_message = data.get('error_message')
        build.log_file_path = data.get('log_file_path')
        build.artifact_paths = data.get('artifact_paths', [])
        build.agent_name = data.get('agent_name')
        build.agent_artifact_paths = data.get('agent_artifact_paths', [])
        build.step_details = data.get('step_details', []) # Load step_details
        build.shared_data = data.get('shared_data', {}) # Load shared_data
        return build
