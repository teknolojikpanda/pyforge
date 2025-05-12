import os
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Dict, Optional, List, Any
import uuid
import json
import logging
import time # For sleep during retries
import httpx # For making requests to agents

from .job_manager import JobManager
from .job import Job # Make sure Job is imported
from .logger_setup import logger, get_build_logger, LOGS_DIR as BUILD_LOGS_DIR
from .build import Build # Import the Build dataclass
from .agent_manager import AgentManager, AgentInfo # New import

# --- Constants ---
BUILDS_METADATA_DIR_NAME = "builds_metadata"
ARTIFACTS_DIR_NAME = "artifacts"
WORKSPACES_DIR_NAME = "workspaces" # For local builds

PYFORGE_OUTPUT_VARS_FILENAME = "pyforge_output_vars.env"
# --- Globals for tracking active builds (could be part of the class) ---
# Key: build_id (str), Value: Future object from ThreadPoolExecutor
active_builds: Dict[str, Future] = {}


class BuildExecutor:
    def __init__(self, job_manager: JobManager, data_dir: Path):
        self.job_manager = job_manager
        self.data_dir = data_dir # Root data directory (e.g., pyforge/data)
        self.builds_root_dir = data_dir / BUILDS_METADATA_DIR_NAME
        self.artifacts_root_dir = data_dir / ARTIFACTS_DIR_NAME
        self.build_executor_pool = ThreadPoolExecutor(max_workers=os.cpu_count() or 4)
        self.agent_manager = AgentManager(data_dir) # Initialize agent manager, pass data_dir for config path
        self.logger = logger # Use the global logger for BuildExecutor's own logs

        self.builds_root_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_root_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / WORKSPACES_DIR_NAME).mkdir(parents=True, exist_ok=True)

    def _get_next_build_number(self, job_name: str) -> int:
        job_builds_meta_dir = self.builds_root_dir / job_name
        if not job_builds_meta_dir.exists():
            return 1
        
        max_build_num = 0
        for build_id_dir in job_builds_meta_dir.iterdir():
            if build_id_dir.is_dir():
                meta_file = build_id_dir / "build_info.json"
                if meta_file.exists():
                    try:
                        with open(meta_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        current_num = data.get("build_number")
                        if isinstance(current_num, int):
                            max_build_num = max(max_build_num, current_num)
                    except (json.JSONDecodeError, IOError) as e:
                        self.logger.warning(f"Could not read or parse build_info.json in {build_id_dir} for build number: {e}")
                    except Exception as e: # Catch any other unexpected error
                        self.logger.error(f"Unexpected error getting build number from {build_id_dir}: {e}", exc_info=True)
        return max_build_num + 1

    def trigger_build(self, job_name: str, triggered_by: str = "manual", scm_commit_hash: Optional[str] = None) -> Optional[Build]:
        job = self.job_manager.get_job(job_name)
        if not job:
            self.logger.error(f"Job '{job_name}' not found. Cannot trigger build.")
            return None

        build_id = str(uuid.uuid4())
        build_start_time = datetime.now(timezone.utc)
        next_build_number = self._get_next_build_number(job_name)

        # Server-side metadata directory for the build
        build_meta_dir = self.builds_root_dir / job_name / build_id
        build_meta_dir.mkdir(parents=True, exist_ok=True)

        # Initialize step_details
        initial_step_details = []
        if job.steps:
            for step_config in job.steps:
                initial_step_details.append({
                    "name": step_config.name,
                    "language": step_config.language or "python", # Default from job.py
                    "status": "PENDING", # Initial status
                    "start_time": None, "end_time": None, "error_message": None
                })

        # Create initial build status object
        build = Build(
            id=build_id,
            build_number=next_build_number,
            job_name=job_name,
            status="PENDING",
            start_time=build_start_time.isoformat(),
            triggered_by=triggered_by,
            scm_commit_hash=scm_commit_hash,
            step_details=initial_step_details
            # shared_data is initialized by default_factory=dict
        )
        self.save_build_status(build) # Save initial PENDING state

        self.logger.info(f"Triggering build {build.id} for job '{job_name}'...")

        if job.agent:
            agent_info = self.agent_manager.get_agent(job.agent.name) # Use job.agent.name
            if not agent_info:
                self.logger.error(f"Agent '{job.agent.name}' specified for job '{job_name}' not found or configured. Build cannot run.")
                build.status = "ERROR"
                build.end_time = datetime.now(timezone.utc).isoformat()
                build.error_message = f"Agent '{job.agent.name}' not found."
                self.save_build_status(build)
                return build 

            self.logger.info(f"Job '{job_name}' (Build ID: {build.id}) is configured to run on agent: {job.agent.name}. Dispatching...")
            build.agent_name = job.agent.name # Store the agent name string in the Build object
            self.save_build_status(build)
            
            future = self.build_executor_pool.submit(self._dispatch_to_agent, build, job, agent_info)
            active_builds[build_id] = future
            return build
        else: # No specific agent specified in job config, try to find ANY available agent
            self.logger.info(f"Job '{job_name}' (Build ID: {build.id}) has no specific agent. Attempting to find the most available agent...")
            
            most_available_agent_info = self.agent_manager.find_most_available_agent()

            if most_available_agent_info:
                self.logger.info(f"Found most available agent '{most_available_agent_info.name}' for job '{job_name}'. Dispatching...")
                build.agent_name = most_available_agent_info.name # Set the agent name on the build object
                self.save_build_status(build) # Save updated build status with agent name
                future = self.build_executor_pool.submit(self._dispatch_to_agent, build, job, most_available_agent_info)
                active_builds[build_id] = future
                return build # Build dispatched to the most available agent
            else: # No agent specified, and no available agent found (or no agents configured)
                # No agent specified, and no available agent found (or no agents configured)
                self.logger.error(f"No agent specified for job '{job_name}' and no available agents found. Build cannot run on an agent.")
                build.status = "ERROR" 
                build.end_time = datetime.now(timezone.utc).isoformat()
                build.error_message = "No agent specified and no available agents found to run the job."
                self.save_build_status(build)
                return build


    def _dispatch_to_agent(self, build: Build, job: Job, agent_info: AgentInfo):
        """Sends the job to the agent for execution."""
        build_log_path_server = self.get_build_log_path(build.job_name, build.id, on_server=True)
        dispatch_logger = self._setup_build_specific_logger(f"dispatch_{build.id}", build_log_path_server)        
        try:
            # Access connect_timeout from the job.agent object
            connect_timeout_seconds = job.agent.connect_timeout if job.agent and job.agent.connect_timeout is not None else 0
            executor_wait_timeout_seconds = job.agent.executor_wait_timeout if job.agent and job.agent.executor_wait_timeout is not None else 0
            retry_interval_seconds = 15 

            dispatch_logger.info(f"Dispatching build {build.id} for job {job.name} to agent {agent_info.name} at {agent_info.address} (timeout: {connect_timeout_seconds}s)")
            build.status = "DISPATCHING"
            self.save_build_status(build) # Save initial dispatching status

            server_callback_base_url = os.environ.get("PYFORGE_SERVER_CALLBACK_URL", "http://localhost:5000")
            
            payload = {
                "build_id": build.id,
                "job_name": job.name,
                "job_description": job.description,
                "scm": job.scm.to_dict() if job.scm else None,
                "steps": [step.to_dict() for step in job.steps], # This will include 'language'
                "environment": job.environment,
                "artifacts_config": [art.to_dict() for art in job.artifacts],
                "server_callback_url": f"{server_callback_base_url}/api/agent/build/{build.id}/update",
                "server_artifact_upload_url": f"{server_callback_base_url}/api/agent/build/{build.id}/artifact",
                "server_log_stream_url": f"{server_callback_base_url}/api/agent/build/{build.id}/stream_log",
                "initial_shared_data": build.shared_data.copy() # Send current shared data
            }

            dispatch_attempt_start_time = time.monotonic()

            # --- Agent Connection Loop ---
            agent_is_reachable = False
            while not agent_is_reachable: # Loop until agent is confirmed reachable or timeout
                elapsed_time = time.monotonic() - dispatch_attempt_start_time
                if connect_timeout_seconds > 0 and elapsed_time > connect_timeout_seconds:
                    error_msg = f"Agent {agent_info.name} connection timed out after {connect_timeout_seconds} seconds."
                    dispatch_logger.error(error_msg)
                    build.status = "ERROR"
                    build.error_message = error_msg
                    build.end_time = datetime.now(timezone.utc).isoformat()
                    self.save_build_status(build)
                    return # Exit dispatch function

                try: 
                    dispatch_logger.info(f"Checking reachability of agent {agent_info.name} at {agent_info.address}/health...")
                    # Use a short timeout for the health check itself
                    health_check_timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0) # Reasonably short
                    with httpx.Client(timeout=health_check_timeout) as health_client:
                        response = health_client.get(f"{agent_info.address}/health")
                        response.raise_for_status() # Will raise for 4xx/5xx status
                    dispatch_logger.info(f"Agent {agent_info.name} is reachable. Health status: {response.status_code}")
                    agent_is_reachable = True # Set flag to exit loop

                except httpx.ConnectError as ce:
                    error_msg_conn = f"Connection to agent {agent_info.name} at {agent_info.address} failed: {ce}"
                    dispatch_logger.warning(error_msg_conn)
                    if connect_timeout_seconds <= 0: 
                        build.status = "ERROR"
                        build.error_message = error_msg_conn
                        build.end_time = datetime.now(timezone.utc).isoformat()
                        self.save_build_status(build)
                        return # Exit dispatch function
                    
                    build.status = "WAITING_FOR_AGENT" 
                    build.error_message = f"Agent not reachable, retrying... Last error: {ce}" 
                    self.save_build_status(build) 

                    remaining_time = connect_timeout_seconds - elapsed_time
                    dispatch_logger.info(f"Will retry in {retry_interval_seconds}s. Time remaining for agent connection: {max(0, remaining_time):.0f}s.")
                    time.sleep(retry_interval_seconds)
                    # Loop continues for reachability check

                except httpx.HTTPStatusError as hse: # Agent is reachable but /health endpoint returns error
                    error_msg = f"Agent {agent_info.name} /health endpoint returned status {hse.response.status_code}: {hse.response.text}"
                    dispatch_logger.error(error_msg) # No exc_info needed as hse contains response
                    build.status = "ERROR"
                    build.error_message = error_msg # More specific error
                    build.end_time = datetime.now(timezone.utc).isoformat()
                    self.save_build_status(build)
                    return # Exit dispatch function

                except Exception as e_conn_check: 
                    error_msg = f"Error checking agent {agent_info.name} reachability: {e_conn_check}"
                    dispatch_logger.error(error_msg, exc_info=True)
                    # Decide if this is a retryable error or immediate failure
                    if connect_timeout_seconds <= 0: # No extended wait, fail immediately
                        build.status = "ERROR"
                        build.error_message = error_msg
                        build.end_time = datetime.now(timezone.utc).isoformat()
                        self.save_build_status(build)
                        return # Exit dispatch function
                    
                    build.status = "WAITING_FOR_AGENT"
                    build.error_message = f"Error checking agent reachability, retrying... Last error: {e_conn_check}"
                    self.save_build_status(build)
                    remaining_time = connect_timeout_seconds - elapsed_time
                    dispatch_logger.info(f"Will retry agent reachability check in {retry_interval_seconds}s. Time remaining: {max(0, remaining_time):.0f}s.")
                    time.sleep(retry_interval_seconds)
                    # Loop continues for reachability check

            # If we exit the loop, agent_is_reachable must be True
            if not agent_is_reachable: # Should not happen if loop logic is correct, but as a safeguard
                self.logger.error(f"Exited agent reachability check loop for build {build.id} without confirming reachability. This is unexpected.")
                build.status = "ERROR"
                build.error_message = "Internal error: Failed to confirm agent reachability."
                build.end_time = datetime.now(timezone.utc).isoformat()
                self.save_build_status(build)
                return

            # --- Executor Allocation Loop ---
            executor_allocation_start_time = time.monotonic()
            executor_allocated = False
            while not executor_allocated:
                elapsed_executor_wait_time = time.monotonic() - executor_allocation_start_time
                if executor_wait_timeout_seconds > 0 and elapsed_executor_wait_time > executor_wait_timeout_seconds:
                    error_msg = f"Timeout waiting for a free executor on agent {agent_info.name} after {executor_wait_timeout_seconds}s."
                    dispatch_logger.error(error_msg)
                    build.status = "ERROR"
                    build.error_message = error_msg
                    build.end_time = datetime.now(timezone.utc).isoformat()
                    self.save_build_status(build)
                    return 

                if self.agent_manager.allocate_executor(agent_info.name):
                    executor_allocated = True
                    dispatch_logger.info(f"Executor allocated on agent {agent_info.name}.")
                    break 
                else:
                    build.status = "WAITING_FOR_AGENT_EXECUTOR"
                    error_msg_exec = f"No free executors on agent {agent_info.name}. Waiting..."
                    build.error_message = error_msg_exec 
                    self.save_build_status(build)
                    dispatch_logger.info(error_msg_exec + f" Will check again in {retry_interval_seconds}s.")
                    time.sleep(retry_interval_seconds)

            # --- Actual Job Dispatch to Agent (if executor was allocated) ---
            if executor_allocated:
                try:
                    dispatch_logger.info(f"Dispatching job payload to agent {agent_info.name}...")
                    timeout_config = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0)
                    with httpx.Client(timeout=timeout_config) as client:
                        response = client.post(f"{agent_info.address}/api/v1/execute_job", json=payload)
                        response.raise_for_status()
                    
                    dispatch_logger.info(f"Successfully dispatched build {build.id} to agent {agent_info.name}. Agent responded: {response.json()}")
                    build.status = "WAITING_FOR_AGENT" 
                    build.error_message = None 
                    self.save_build_status(build)

                except Exception as e_dispatch: 
                    self.agent_manager.release_executor(agent_info.name) 
                    error_msg = f"Failed to dispatch job payload to agent {agent_info.name} after allocating executor: {e_dispatch}"
                    dispatch_logger.error(error_msg, exc_info=True)
                    build.status = "ERROR"
                    build.error_message = error_msg
                    build.end_time = datetime.now(timezone.utc).isoformat()
                    self.save_build_status(build)

        finally:
            for handler in list(dispatch_logger.handlers):
                if isinstance(handler, logging.FileHandler):
                    handler.close()
                    dispatch_logger.removeHandler(handler)

    def update_build_from_agent(self, build_id: str, status: str, log_content: Optional[str] = None,
                                error_message: Optional[str] = None, agent_artifacts: Optional[list] = None,
                                executed_step_details: Optional[List[Dict[str, Any]]] = None,
                                updated_shared_data: Optional[Dict[str, str]] = None):
        build = self.get_build_status_obj(build_id) 
        if not build:
            self.logger.error(f"Agent callback for unknown build_id: {build_id}")
            return False

        self.logger.info(f"Received update for build {build_id} from agent {build.agent_name}: Status={status}")
        build.status = status
        if error_message: build.error_message = error_message
        else: build.error_message = None 
        if agent_artifacts: build.agent_artifact_paths = agent_artifacts 

        if executed_step_details:
            self.logger.info(f"Processing {len(executed_step_details)} step details from agent for build {build_id}")
            # Create a dictionary for quick lookup of existing step details by name
            current_step_details_map = {detail['name']: detail for detail in build.step_details}
            
            for agent_step_detail in executed_step_details:
                step_name = agent_step_detail.get("name")
                if step_name in current_step_details_map:
                    # Update existing step detail
                    current_step_details_map[step_name]["status"] = agent_step_detail.get("status", "UNKNOWN")
                    current_step_details_map[step_name]["start_time"] = agent_step_detail.get("start_time")
                    current_step_details_map[step_name]["end_time"] = agent_step_detail.get("end_time")
                    current_step_details_map[step_name]["error_message"] = agent_step_detail.get("error_message")
                else:
                    # This case should ideally not happen if initialized correctly, but log it
                    self.logger.warning(f"Received detail for unknown step '{step_name}' from agent for build {build_id}. Adding it.")
                    build.step_details.append(agent_step_detail) # Or handle as an error
            # No need to reassign build.step_details if we modified items in place

        if updated_shared_data is not None:
            self.logger.info(f"Updating shared_data for build {build_id} with {len(updated_shared_data)} vars from agent.")
            build.shared_data.update(updated_shared_data)


        if status in ["SUCCESS", "FAILURE", "ERROR", "CANCELLED"]:
            build.end_time = datetime.now(timezone.utc).isoformat()
            if build.agent_name:
                self.agent_manager.release_executor(build.agent_name)
        
        server_log_path = self.get_build_log_path(build.job_name, build.id, on_server=True)
        try:
            with open(server_log_path, "a", encoding="utf-8") as f:
                f.write(f"\n--- Agent Update ({datetime.now(timezone.utc).isoformat()}) ---\n")
                f.write(f"Status: {status}\n")
                if error_message: f.write(f"Error: {error_message}\n")
                if log_content: f.write(f"Log from agent (summary/final part):\n{log_content}\n")
                f.write("-----------------------------------------\n")
        except Exception as e:
            self.logger.error(f"Failed to append agent update to server log {server_log_path} for build {build_id}: {e}")

        self.save_build_status(build)
        return True

    def _setup_build_specific_logger(self, logger_name: str, log_file_path: Path) -> logging.Logger:
        build_logger = logging.getLogger(logger_name)
        if any(isinstance(h, logging.FileHandler) and h.baseFilename == str(log_file_path) for h in build_logger.handlers):
            return build_logger 

        for handler in list(build_logger.handlers): build_logger.removeHandler(handler)

        build_logger.setLevel(logging.DEBUG)
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        
        fh = logging.FileHandler(log_file_path)
        fh.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        build_logger.addHandler(fh)
        build_logger.propagate = False 
        return build_logger

    # NOTE: This method _run_build_process is currently NOT called by the main trigger_build flow,
    # which prioritizes agent-based execution. If local builds (i.e., executed directly
    # by the PyForge server) are desired, trigger_build logic would need to be updated
    # to call this method, perhaps as a fallback or based on job configuration.
    # If reinstated, consider refactoring to use SCMHandler for checkout and
    # ArtifactManager for artifact collection for consistency.
    def _run_build_process(self, build: Build, job: Job, build_workspace: Path):
        build_log_path = self.get_build_log_path(job.name, build.id)
        build_logger = self._setup_build_specific_logger(f"build_{build.id}", build_log_path)
        
        build_logger.info(f"Starting local build process for job '{job.name}', Build ID: {build.id}")
        build.status = "RUNNING"
        build.log_file_path = str(build_log_path.relative_to(BUILD_LOGS_DIR.parent))
        self.save_build_status(build)

        current_scm_commit_hash = None
        source_checkout_dir = build_workspace / "source" 

        try:
            if job.scm:
                build_logger.info(f"Cloning/updating SCM: {job.scm.url} branch {job.scm.branch}")
                if source_checkout_dir.exists():
                    shutil.rmtree(source_checkout_dir)
                source_checkout_dir.mkdir(parents=True, exist_ok=True)
                
                clone_cmd = ["git", "clone", "--branch", job.scm.branch, "--depth", "1", job.scm.url, str(source_checkout_dir)]
                # For local SCM clone, pass empty dict for shared_data as it's not relevant for this command
                self._execute_command(" ".join(clone_cmd), "shell", build_workspace, build_logger, job.environment, {}) 
                
                hash_cmd = ["git", "rev-parse", "HEAD"]
                process = subprocess.run(hash_cmd, cwd=source_checkout_dir, capture_output=True, text=True, check=False)
                if process.returncode == 0:
                    current_scm_commit_hash = process.stdout.strip()
                    build.scm_commit_hash = current_scm_commit_hash
                    build_logger.info(f"SCM checkout successful. Commit: {current_scm_commit_hash}")
                else:
                    build_logger.warning(f"Could not get commit hash: {process.stderr}")
            else:
                source_checkout_dir.mkdir(parents=True, exist_ok=True) 

            for step_index, step in enumerate(job.steps):
                build_logger.info(f"Executing step: {step.name} (Language: {step.language})")
                
                # Update step status to RUNNING in build.step_details
                if build.step_details and len(build.step_details) > step_index:
                    build.step_details[step_index]["status"] = "RUNNING"
                    build.step_details[step_index]["start_time"] = datetime.now(timezone.utc).isoformat()
                    self.save_build_status(build) # Save intermediate status

                step_work_dir_str = step.working_directory
                current_work_dir = source_checkout_dir / step_work_dir_str if step_work_dir_str else source_checkout_dir
                current_work_dir.mkdir(parents=True, exist_ok=True) 

                # Prepare environment for the step
                env_for_step = job.environment.copy() # Start with job-level env
                env_for_step.update(step.environment) # Override/add with step-specific env

                step_succeeded = self._execute_command(
                    step.script, 
                    step.language or "python", # Default to python if not specified
                    current_work_dir, 
                    build_logger, 
                    env_for_step, # This is the merged job+step specific env
                    build.shared_data.copy() # Pass a copy of current shared data
                )

                # Process output variables from the step
                self._process_output_vars(current_work_dir, build, build_logger)

                if not step_succeeded:
                    if build.step_details and len(build.step_details) > step_index:
                        build.step_details[step_index]["status"] = "FAILURE"
                        build.step_details[step_index]["end_time"] = datetime.now(timezone.utc).isoformat()
                        build.step_details[step_index]["error_message"] = f"Step '{step.name}' failed."
                        self.save_build_status(build)
                    raise Exception(f"Step '{step.name}' failed.")
                
                # Update step status to SUCCESS
                if build.step_details and len(build.step_details) > step_index:
                    build.step_details[step_index]["status"] = "SUCCESS"
                    build.step_details[step_index]["end_time"] = datetime.now(timezone.utc).isoformat()
                    self.save_build_status(build)
            
            if job.artifacts:
                build_logger.info("Collecting artifacts...")
                job_artifact_dir = self.artifacts_root_dir / job.name / build.id
                job_artifact_dir.mkdir(parents=True, exist_ok=True)
                
                collected_artifact_paths = []
                for art_conf in job.artifacts:
                    for found_path in source_checkout_dir.glob(art_conf.pattern):
                        dest_path = job_artifact_dir / found_path.name 
                        if found_path.is_file():
                            shutil.copy2(found_path, dest_path)
                            build_logger.info(f"Collected artifact: {found_path.name} to {dest_path}")
                            collected_artifact_paths.append({
                                "name": art_conf.name, 
                                "path": str(dest_path.relative_to(self.artifacts_root_dir))
                            })
                        elif found_path.is_dir(): 
                            shutil.copytree(found_path, dest_path / found_path.name, dirs_exist_ok=True)
                            build_logger.info(f"Collected directory artifact: {found_path.name} to {dest_path / found_path.name}")
                            collected_artifact_paths.append({
                                "name": art_conf.name,
                                "path": str((dest_path / found_path.name).relative_to(self.artifacts_root_dir))
                            })
                build.artifact_paths = collected_artifact_paths

            build.status = "SUCCESS"
            build_logger.info("Build completed successfully.")

        except Exception as e:
            build.status = "FAILURE"
            build.error_message = str(e)
            # Mark the currently running or last attempted step as FAILED if not already
            # This part might need refinement if the exact failing step isn't easily determined here
            # For now, the overall build error message covers it.
            # If a specific step failed, its status should have been set to FAILURE already.

            build_logger.error(f"Build failed with unhandled exception: {e}", exc_info=True)
        finally:
            build.end_time = datetime.now(timezone.utc).isoformat()
            self.save_build_status(build)
            build_logger.info(f"Local build {build.id} finished with status: {build.status}")
            
            for handler in list(build_logger.handlers):
                if isinstance(handler, logging.FileHandler):
                    handler.close()
                    build_logger.removeHandler(handler)
            
    def _process_output_vars(self, work_dir: Path, build: Build, logger_instance: logging.Logger):
        """Reads PYFORGE_OUTPUT_VARS_FILENAME and updates build.shared_data."""
        output_vars_file = work_dir / PYFORGE_OUTPUT_VARS_FILENAME
        if output_vars_file.exists():
            logger_instance.info(f"Processing output variables file: {output_vars_file}")
            try:
                with open(output_vars_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and '=' in line and not line.startswith('#'): # Basic parsing, ignore comments
                            key, value = line.split('=', 1)
                            build.shared_data[key.strip()] = value.strip()
                            logger_instance.info(f"  Set shared var: {key.strip()} = ***") # Mask value
                os.remove(output_vars_file) # Clean up
            except Exception as e:
                logger_instance.error(f"Error processing output variables file {output_vars_file}: {e}")
            self.save_build_status(build) # Save updated shared_data

    def _execute_command(self, script_content: str, language: str, cwd: Path, 
                         build_logger: logging.Logger, 
                         step_merged_env: Dict[str, str], 
                         current_build_shared_data: Dict[str, str]):
        """Executes a script (shell or python) and logs its output."""
        # Environment Precedence: step_merged_env > current_build_shared_data > os.environ
        full_env = os.environ.copy()
        full_env.update(current_build_shared_data) # Shared data from previous steps
        full_env.update(step_merged_env) # Job and step specific env (takes precedence)

        command_to_run: list[str]
        temp_script_path: Optional[Path] = None
        use_shell_for_popen = False

        if language == "python":
            build_logger.info(f"Preparing to run Python script in {cwd}")
            temp_script_path = cwd / f"_pyforge_temp_script_{uuid.uuid4()}.py"
            with open(temp_script_path, "w", encoding="utf-8") as f:
                f.write(script_content)
            command_to_run = ["python3", str(temp_script_path)]
            use_shell_for_popen = False
        elif language == "shell":
            build_logger.info(f"Running shell command in {cwd}:\n{script_content}")
            # For shell scripts, write to file to handle complex scripts better
            temp_script_path = cwd / f"_pyforge_temp_script_{uuid.uuid4()}.sh"
            with open(temp_script_path, "w", encoding="utf-8") as f:
                if not script_content.strip().startswith("#!"):
                    f.write("#!/bin/sh\n") # Default shebang
                f.write(script_content)
            os.chmod(temp_script_path, 0o755)
            command_to_run = [str(temp_script_path)]
            use_shell_for_popen = False # Execute the file directly
        else:
            build_logger.error(f"Unsupported script language: {language}")
            return False

        try:
            process = subprocess.Popen(
                command_to_run, # Pass as a list
                shell=use_shell_for_popen, 
                cwd=cwd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.STDOUT, 
                text=True, 
                encoding='utf-8', 
                errors='replace', 
                env=full_env
            )
        except FileNotFoundError as e: 
            build_logger.error(f"Command execution error: {e}. Ensure interpreter/shell is in PATH.")
            if temp_script_path and temp_script_path.exists(): # Clean up if interpreter not found
                os.remove(temp_script_path)
            return False

        for line in process.stdout:
            build_logger.info(line.strip())
        process.wait()

        if temp_script_path and temp_script_path.exists():
            os.remove(temp_script_path) 

        if process.returncode != 0:
            build_logger.error(f"Command failed with exit code {process.returncode}")
            return False
        return True

    def save_build_status(self, build: Build):
        build_meta_file = self.builds_root_dir / build.job_name / build.id / "build_info.json"
        try:
            build_meta_file.parent.mkdir(parents=True, exist_ok=True)
            
            build_dict = build.to_dict() 
            with open(build_meta_file, "w") as f:
                json.dump(build_dict, f, indent=2)
                
        except (OSError, IOError) as e: 
            self.logger.error(
                f"File I/O error saving build status for {build.id} (job: {build.job_name}) to {build_meta_file}: {e}",
                exc_info=True
            )
        except TypeError as e: 
            self.logger.error(
                f"Type error serializing build status for {build.id} (job: {build.job_name}): {e}",
                exc_info=True
            )
        except Exception as e:
            self.logger.error(
                f"Unexpected error saving build status for {build.id} (job: {build.job_name}) to {build_meta_file}: {e}",
                exc_info=True
            )

    def get_build_status_obj(self, build_id: str) -> Optional[Build]:
        for job_dir in self.builds_root_dir.iterdir():
            if not job_dir.is_dir(): continue
            build_specific_dir = job_dir / build_id
            if build_specific_dir.is_dir():
                build_meta_file = build_specific_dir / "build_info.json"
                if build_meta_file.exists():
                    try:
                        with open(build_meta_file, "r") as f:
                            data = json.load(f)
                        return Build.from_dict(data)
                    except json.JSONDecodeError:
                        self.logger.error(f"Error decoding build status JSON for {build_id} in job {job_dir.name}")
                    except Exception as e:
                        self.logger.error(f"Error loading build status for {build_id} in job {job_dir.name}: {e}")
                return None 
        return None 

    def get_build_status(self, build_id: str) -> Optional[dict]:
        build_obj = self.get_build_status_obj(build_id)
        return build_obj.to_dict() if build_obj else None

    def list_builds_for_job(self, job_name: str, limit: int = 20) -> List[dict]:
        job_builds_dir = self.builds_root_dir / job_name
        if not job_builds_dir.exists():
            return []
        
        build_info_files = []
        for build_dir_path in job_builds_dir.iterdir():
            if build_dir_path.is_dir():
                meta_file = build_dir_path / "build_info.json"
                if meta_file.exists():
                    build_info_files.append(meta_file)
        
        build_info_files.sort(key=lambda f: os.path.getmtime(f), reverse=True)

        builds_data = []
        for bf in build_info_files[:limit]:
            try:
                with open(bf, "r") as f:
                    build_data = json.load(f)
                builds_data.append(build_data)
            except Exception as e:
                self.logger.error(f"Error reading build file {bf}: {e}")
        return builds_data

    def get_build_log_path(self, job_name: str, build_id: str, on_server: bool = False) -> Path:
        log_dir_base = BUILD_LOGS_DIR
        job_log_dir = log_dir_base / job_name / build_id 
        job_log_dir.mkdir(parents=True, exist_ok=True)
        
        if on_server: 
            return job_log_dir / "dispatch_server.log"
        return job_log_dir / "build.log" 

    def get_build_log_content(self, job_name: str, build_id: str, max_lines: Optional[int] = None) -> str:
        build = self.get_build_status_obj(build_id) 
        if build and build.agent_name:
            agent_info = self.agent_manager.get_agent(build.agent_name)
            server_dispatch_log_path = self.get_build_log_path(job_name, build_id, on_server=True)
            server_log_content = ""
            if server_dispatch_log_path.exists():
                 try:
                    with open(server_dispatch_log_path, "r", encoding="utf-8", errors="replace") as f:
                        server_log_content = f.read() 
                 except Exception as e:
                    server_log_content = f"Error reading server dispatch log: {e}"
            return (f"Build is/was executed on agent: {build.agent_name} ({agent_info.address if agent_info else 'Unknown Address'}).\n"
                    f"Full logs are on the agent. Server-side dispatch/update log:\n{'-'*20}\n{server_log_content}")

        log_path = self.get_build_log_path(job_name, build_id) 
        if not log_path.exists():
            return "Log file not found."
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                if max_lines:
                    lines = f.readlines()
                    return "".join(lines[-max_lines:])
                return f.read()
        except Exception as e:
            self.logger.error(f"Error reading log file {log_path}: {e}")
            return f"Error reading log file: {e}"

    def shutdown(self, wait: bool = True):
        self.logger.info(f"Shutting down BuildExecutor's thread pool (wait={wait})...")
        if hasattr(self.build_executor_pool, 'shutdown'):
            if not wait and hasattr(self.build_executor_pool, '_threads'): 
                 self.build_executor_pool.shutdown(wait=False, cancel_futures=True) # type: ignore
            else:
                 self.build_executor_pool.shutdown(wait=wait)
        self.logger.info("BuildExecutor's thread pool shutdown complete.")
