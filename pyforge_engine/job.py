from dataclasses import dataclass, field
from typing import List, Optional, Dict
import yaml
from pathlib import Path

@dataclass
class SCM:
    type: str  # e.g., "git"
    url: str
    branch: str

    def to_dict(self) -> dict:
        return {"type": self.type, "url": self.url, "branch": self.branch}

@dataclass
class Step:
    name: str
    script: str
    language: Optional[str] = "python" # Default to "python"
    working_directory: Optional[str] = None # Relative to checkout dir
    environment: Dict[str, str] = field(default_factory=dict) # Step-specific environment variables

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "language": self.language,
            "script": self.script,
            "working_directory": self.working_directory,
            "environment": self.environment
        }

@dataclass
class Trigger:
    type: str  # e.g., "scm_poll", "manual", "cron"
    cron: Optional[str] = None  # For cron triggers

    def to_dict(self) -> dict:
        return {"type": self.type, "cron": self.cron}

@dataclass
class Artifact:
    name: str
    pattern: str # e.g., "dist/*.zip", "output.log"

    def to_dict(self) -> dict:
        return {"name": self.name, "pattern": self.pattern}

@dataclass
class AgentConfig:
    name: str
    connect_timeout: Optional[int] = None # Seconds to wait for agent to become reachable
    executor_wait_timeout: Optional[int] = None # Seconds to wait for a free executor on the agent

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "connect_timeout": self.connect_timeout,
            "executor_wait_timeout": self.executor_wait_timeout
        }
@dataclass
class Job:
    name: str
    description: Optional[str] = None
    scm: Optional[SCM] = None
    steps: List[Step] = field(default_factory=list)
    triggers: List[Trigger] = field(default_factory=list)
    artifacts: List[Artifact] = field(default_factory=list)
    environment: Dict[str, str] = field(default_factory=dict)
    agent: Optional[AgentConfig] = None  # Agent configuration object
    raw_config: Optional[str] = None # The raw YAML string of the job configuration

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "scm": self.scm.to_dict() if self.scm else None,
            "steps": [s.to_dict() for s in self.steps],
            "triggers": [t.to_dict() for t in self.triggers],
            "artifacts": [a.to_dict() for a in self.artifacts],
            "environment": self.environment,            
            "agent": self.agent.to_dict() if self.agent else None,
            "raw_config": self.raw_config,
        }

    @classmethod
    def from_yaml(cls, file_path: Path, raw_yaml_content: str) -> 'Job':
        config = yaml.safe_load(raw_yaml_content)
        job_data = config.get('job', {})
        if 'name' not in job_data or 'steps' not in job_data:
            raise ValueError(f"Job config {file_path.name} must contain 'name' and 'steps' under 'job' key.")
        
        scm_data = job_data.get('scm')
        scm = None
        if scm_data:
            if not all(key in scm_data for key in ['type', 'url', 'branch']):
                raise ValueError(
                    f"SCM configuration in {file_path.name} is missing one or more required fields "
                    f"('type', 'url', 'branch'). Found: {list(scm_data.keys())}"
                )
            scm = SCM(**scm_data)
        
        steps_data = job_data.get('steps', [])
        steps = []
        for s_data in steps_data:
            s_lang = s_data.get('language')
            if s_lang is None: 
                s_data['language'] = "python" 
            steps.append(Step(**s_data))
        triggers_data = job_data.get('triggers', [])
        triggers = [Trigger(**t) for t in triggers_data]

        artifacts_data = job_data.get('artifacts', [])
        artifacts = [Artifact(**a) for a in artifacts_data]
        
        environment_data = job_data.get('environment', {})
        
        agent_config_data = job_data.get('agent')
        parsed_agent_config: Optional[AgentConfig] = None

        if isinstance(agent_config_data, dict):
            agent_name = agent_config_data.get('name')
            if not agent_name or not isinstance(agent_name, str):
                raise ValueError(
                    f"Agent configuration in {file_path.name} must have a 'name' (string) field. "
                    f"Found: {agent_name}"
                )
            
            agent_timeout_val = agent_config_data.get('connect_timeout')
            executor_timeout_val = agent_config_data.get('executor_wait_timeout')

            parsed_agent_timeout: Optional[int] = None
            parsed_executor_timeout: Optional[int] = None

            if agent_timeout_val is not None:
                try:
                    parsed_agent_timeout = int(agent_timeout_val)
                except ValueError:
                    raise ValueError(
                        f"Invalid 'connect_timeout' value '{agent_timeout_val}' in agent config for {file_path.name}. "
                        "It must be an integer."
                    )
            if executor_timeout_val is not None:
                try:
                    parsed_executor_timeout = int(executor_timeout_val)
                except ValueError:
                    raise ValueError(
                        f"Invalid 'executor_wait_timeout' value '{executor_timeout_val}' in agent config for {file_path.name}. "
                        "It must be an integer."
                    )
            parsed_agent_config = AgentConfig(name=agent_name, connect_timeout=parsed_agent_timeout, executor_wait_timeout=parsed_executor_timeout)
        elif agent_config_data is not None: 
            raise ValueError(
                f"Agent configuration in {file_path.name} must be a dictionary (with 'name' and optional 'connect_timeout', 'executor_wait_timeout'). "
                f"Found type: {type(agent_config_data)}"
            )

        return cls(
            name=job_data['name'],
            description=job_data.get('description'),
            scm=scm,
            steps=steps,
            triggers=triggers,
            artifacts=artifacts,
            environment=environment_data,
            agent=parsed_agent_config,
            raw_config=raw_yaml_content
        )
