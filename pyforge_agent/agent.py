from flask import Flask, request, jsonify
import subprocess
import os
import shutil
from pathlib import Path
import logging
import sys
import threading
import uuid # For temporary script names
import httpx # For callbacks to server
from datetime import datetime, timezone
import platform # For OS info
import psutil # For hardware info (CPU, memory)
import tarfile # For packaging artifacts if needed
from typing import List, Optional, Dict, Any # Added for type hinting

# --- Agent Configuration ---
AGENT_WORKSPACE_ROOT = Path(os.environ.get("PYFORGE_AGENT_WORKSPACE", Path.home() / "pyforge_agent_workspace"))
AGENT_ARTIFACTS_ROOT = Path(os.environ.get("PYFORGE_AGENT_ARTIFACTS", Path.home() / "pyforge_agent_artifacts_store")) # Store for agent before upload
AGENT_PORT = int(os.environ.get("PYFORGE_AGENT_PORT", 8081))

# --- Logging ---
agent_internal_logger = logging.getLogger("PyForgeAgentInternal")
agent_internal_logger.setLevel(logging.INFO)
# BasicConfig should ideally be called only once. If other modules also call it, it might not behave as expected.
# Consider a more centralized logging setup if this becomes an issue.
if not agent_internal_logger.handlers: # Avoid adding handlers multiple times if module is reloaded
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - AGENT_INTERNAL - %(message)s')
    handler.setFormatter(formatter)
    agent_internal_logger.addHandler(handler)
    agent_internal_logger.propagate = False


logger = logging.getLogger("PyForgeAgent") # For general agent messages visible in main log
if not logger.handlers:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - AGENT - %(message)s')

PYFORGE_OUTPUT_VARS_FILENAME = "pyforge_output_vars.env"

app = Flask(__name__)

# --- Helper: SCM Operations ---
def clone_repo(scm_url: str, scm_branch: str, clone_dir: Path, build_log_file: Path):
    with open(build_log_file, "a", encoding="utf-8") as f:
        f.write(f"Cloning {scm_url} branch {scm_branch} into {clone_dir}\n")
    try:
        if clone_dir.exists(): shutil.rmtree(clone_dir)
        clone_dir.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", "--branch", scm_branch, "--depth", "1", scm_url, str(clone_dir)]
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8')
        with open(build_log_file, "a", encoding="utf-8") as f_log_clone: # Use a different var name for clarity
            for line in process.stdout: f_log_clone.write(line)
        process.wait()
        if process.returncode != 0: raise Exception(f"Git clone failed with code {process.returncode}")
        with open(build_log_file, "a", encoding="utf-8") as f_log_success: # Use a different var name
            f_log_success.write("Clone successful.\n")
        return True
    except Exception as e:
        with open(build_log_file, "a", encoding="utf-8") as f_log_err: # Use a different var name
            f_log_err.write(f"ERROR during clone: {e}\n")
        logger.error(f"Clone error: {e}")
        return False

# --- Helper: Stream Log Lines to Server ---
def stream_log_lines_to_server(build_id: str, lines: list, server_log_stream_url: str):
    if not lines or not server_log_stream_url:
        return

    payload = {"lines": lines}
    agent_internal_logger.debug(f"Streaming {len(lines)} log lines for build {build_id} to {server_log_stream_url}")
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(server_log_stream_url, json=payload)
            response.raise_for_status() 
        agent_internal_logger.debug(f"Log lines streamed successfully for build {build_id}.")
    except Exception as e:
        agent_internal_logger.warning(f"Failed to stream log lines for build {build_id}: {e}")

# --- Helper: Execute Step ---
def execute_step(script_content: str, language: str, working_dir: Path, 
                 step_specific_env: dict, job_level_env: dict, 
                 build_log_file: Path, build_id: str, 
                 server_log_stream_url: str,
                 incoming_shared_data: Dict[str, str]):
    with open(build_log_file, "a", encoding="utf-8", errors="replace") as f:
        try:
            working_dir.mkdir(parents=True, exist_ok=True) # Ensure the working directory exists
        except OSError as e_mkdir:
            f.write(f"\nERROR: Could not create working directory {working_dir}: {e_mkdir}\n")
            logger.error(f"OSError creating working_dir {working_dir} for build {build_id}: {e_mkdir}")
            return False
        
        # Environment Precedence: step_specific_env > job_level_env > incoming_shared_data > os.environ
        full_env = os.environ.copy()
        full_env.update(incoming_shared_data)
        full_env.update(job_level_env)
        full_env.update(step_specific_env)
        
        command_to_run_list: List[str]
        temp_script_path_agent: Optional[Path] = None
        use_shell_for_popen = False 

        if language == "python":
            f.write(f"\nPreparing to execute Python script in: {working_dir}\n---\n{script_content}\n---\n")
            temp_script_path_agent = working_dir / f"_pyforge_agent_script_{uuid.uuid4()}.py"
            try:
                with open(temp_script_path_agent, "w", encoding="utf-8") as temp_f:
                    temp_f.write(script_content)
            except IOError as e_io:
                f.write(f"\nERROR: Could not write temporary Python script {temp_script_path_agent}: {e_io}\n")
                logger.error(f"IOError writing temp Python script for build {build_id}: {e_io}")
                return False
            command_to_run_list = ["python3", str(temp_script_path_agent)]
        elif language == "shell":
            f.write(f"\nExecuting shell command in: {working_dir}\n---\n{script_content}\n---\n")
            temp_script_path_agent = working_dir / f"_pyforge_agent_script_{uuid.uuid4()}.sh"
            try:
                with open(temp_script_path_agent, "w", encoding="utf-8") as temp_f:
                    if not script_content.strip().startswith("#!"):
                        temp_f.write("#!/bin/sh\n") 
                    temp_f.write(script_content)
                os.chmod(temp_script_path_agent, 0o755) 
            except IOError as e_io:
                f.write(f"\nERROR: Could not write or chmod temporary shell script {temp_script_path_agent}: {e_io}\n")
                logger.error(f"IOError writing/chmod temp shell script for build {build_id}: {e_io}")
                return False
            command_to_run_list = [str(temp_script_path_agent)] 
        else:
            f.write(f"\nERROR: Unsupported script language: {language}\n")
            logger.error(f"Unsupported script language: {language} for build {build_id}")
            return False

        try:
            process = subprocess.Popen(
                command_to_run_list, 
                shell=use_shell_for_popen, 
                cwd=working_dir, 
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', env=full_env
            )

            log_buffer = []
            MAX_BUFFER_SIZE = 5 

            for line in process.stdout:
                f.write(line) 

                line_no_newline = line.rstrip('\n')
                log_buffer.append(line_no_newline)
                if len(log_buffer) >= MAX_BUFFER_SIZE:
                    stream_log_lines_to_server(build_id, list(log_buffer), server_log_stream_url)
                    log_buffer.clear()

            if log_buffer:
                stream_log_lines_to_server(build_id, list(log_buffer), server_log_stream_url)
                log_buffer.clear()

            process.wait()
            
            f.write(f"Step finished with exit code {process.returncode}\n")
            return process.returncode == 0
        except FileNotFoundError as e_fnf: 
            f.write(f"ERROR executing step (lang: {language}): File not found - {e_fnf}. Ensure interpreter/script is accessible.\n")
            logger.error(f"Step execution FileNotFoundError (build {build_id}): {e_fnf}")
            return False
        except Exception as e:
            f.write(f"ERROR executing step (lang: {language}): {e}\n")
            logger.error(f"Step execution error (build {build_id}): {e}", exc_info=True) 
            return False
        finally:
            if temp_script_path_agent and temp_script_path_agent.exists():
                try:
                    os.remove(temp_script_path_agent)
                except OSError as e_remove:
                    logger.warning(f"Could not remove temporary script {temp_script_path_agent} for build {build_id}: {e_remove}")

def _read_and_update_shared_data(work_dir: Path, current_shared_data: Dict[str, str], build_log_file: Path, build_id: str):
    """Reads output vars file and updates current_shared_data."""
    output_vars_file = work_dir / PYFORGE_OUTPUT_VARS_FILENAME
    if output_vars_file.exists():
        logger.info(f"Processing output variables file for build {build_id}: {output_vars_file}")
        with open(build_log_file, "a", encoding="utf-8") as f_log:
            f_log.write(f"Processing output variables from: {output_vars_file}\n")
        try:
            with open(output_vars_file, "r", encoding="utf-8") as f_vars:
                for line in f_vars:
                    line = line.strip()
                    if line and '=' in line and not line.startswith('#'): # Basic parsing, ignore comments
                        key, value = line.split('=', 1)
                        current_shared_data[key.strip()] = value.strip()
                        logger.info(f"  Build {build_id} shared var updated: {key.strip()} = ***") # Mask value
            os.remove(output_vars_file) # Clean up
        except Exception as e:
            logger.error(f"Error processing output variables file {output_vars_file} for build {build_id}: {e}")
            with open(build_log_file, "a", encoding="utf-8") as f_log:
                f_log.write(f"ERROR processing output variables file {output_vars_file}: {e}\n")


# --- Helper: Collect Artifacts & Upload ---
def collect_and_upload_artifacts(
    artifacts_config: list, workspace_dir: Path, job_name: str, build_id: str, 
    build_log_file: Path, server_artifact_upload_url: str):
    
    agent_job_build_artifact_staging_dir = AGENT_ARTIFACTS_ROOT / job_name / build_id
    agent_job_build_artifact_staging_dir.mkdir(parents=True, exist_ok=True)
    uploaded_artifact_details = [] 

    with open(build_log_file, "a", encoding="utf-8") as f_log_artifact: 
        f_log_artifact.write("\n--- Collecting & Uploading Artifacts ---\n")

        for art_conf in artifacts_config:
            pattern = art_conf.get("pattern")
            name = art_conf.get("name", Path(pattern).name)
            
            source_path_abs_list = list(workspace_dir.glob(pattern))
            if not source_path_abs_list:
                msg = f"Artifact source pattern '{pattern}' not found in workspace '{workspace_dir}'."
                f_log_artifact.write(msg + "\n")
                logger.warning(f"{msg} (Build ID: {build_id})")
                continue
            
            # For simplicity, taking the first match if glob returns multiple.
            # A more robust solution might handle multiple matches differently (e.g., archive all).
            source_path_abs = source_path_abs_list[0] 
            
            # Use a more unique name for the staged artifact if it's a directory to avoid clashes
            # For files, 'name' from config or pattern's name is fine.
            # For directories, ensure the archive name is unique.
            staged_filename = name
            if source_path_abs.is_dir():
                staged_filename = f"{name}.tar.gz"

            agent_staged_artifact_path = agent_job_build_artifact_staging_dir / staged_filename


            try:
                if source_path_abs.is_dir():
                    f_log_artifact.write(f"Archiving directory '{source_path_abs}' to '{agent_staged_artifact_path}'...\n")
                    # agent_staged_artifact_path is now the archive name
                    with tarfile.open(agent_staged_artifact_path, "w:gz") as tar:
                        tar.add(source_path_abs, arcname=source_path_abs.name) 
                    upload_file_path = agent_staged_artifact_path
                    upload_filename = agent_staged_artifact_path.name # This is now the .tar.gz name
                    msg = f"Archived directory '{pattern}' to '{upload_file_path}'."
                else: 
                    shutil.copy2(source_path_abs, agent_staged_artifact_path)
                    upload_file_path = agent_staged_artifact_path
                    upload_filename = name # Use the configured name or pattern's name
                    msg = f"Copied artifact '{pattern}' to agent path '{upload_file_path}'."
                
                f_log_artifact.write(msg + "\n")
                logger.info(f"{msg} (Build ID: {build_id})")

                f_log_artifact.write(f"Uploading {upload_filename} to server...\n")
                with open(upload_file_path, 'rb') as file_to_upload:
                    files = {'file': (upload_filename, file_to_upload)}
                    with httpx.Client(timeout=httpx.Timeout(10.0, read=300.0)) as client:
                        response = client.post(server_artifact_upload_url, files=files)
                        response.raise_for_status()
                
                server_response_json = response.json()
                msg = f"Successfully uploaded artifact '{upload_filename}'. Server path: {server_response_json.get('path')}"
                f_log_artifact.write(msg + "\n")
                logger.info(f"{msg} (Build ID: {build_id})")
                uploaded_artifact_details.append({
                    "name": upload_filename, 
                    "path_on_agent": str(upload_file_path), 
                    "server_path": server_response_json.get("path") 
                })
            except Exception as e_collect:
                msg = f"ERROR processing/uploading artifact '{pattern}': {e_collect}"
                f_log_artifact.write(msg + "\n")
                logger.error(f"{msg} (Build ID: {build_id})", exc_info=True)
                
        f_log_artifact.write("--- Artifact Collection & Upload Finished ---\n")
    return uploaded_artifact_details

# --- Main Build Process on Agent ---
def run_build_on_agent(job_details: dict):
    step_execution_results = [] # To store details of each executed step
    build_id = job_details["build_id"]
    job_name = job_details["job_name"]
    scm_info = job_details.get("scm")
    steps = job_details.get("steps", [])
    job_level_environment = job_details.get("environment", {}) # Job-level environment
    artifacts_config = job_details.get("artifacts_config", [])
    server_callback_url = job_details["server_callback_url"]
    server_artifact_upload_url = job_details["server_artifact_upload_url"]
    server_log_stream_url = job_details.get("server_log_stream_url") 
    current_shared_data = job_details.get("initial_shared_data", {}) # Start with data from server
    
    logger.info(f"Received job: {job_name}, Build ID: {build_id}")
    build_workspace = AGENT_WORKSPACE_ROOT / job_name / build_id
    build_workspace.mkdir(parents=True, exist_ok=True)
    
    agent_log_dir = build_workspace / "_logs" 
    agent_log_dir.mkdir(parents=True, exist_ok=True)
    build_log_file = agent_log_dir / "build.log"
    
    def send_final_update(status: str, error_msg: Optional[str] = None, final_log: Optional[str] = None, 
                          uploaded_artifacts: Optional[list] = None, step_results: Optional[List[Dict]] = None,
                          updated_shared_data_payload: Optional[Dict[str, str]] = None): # Renamed for clarity
        payload = {"status": status}
        if error_msg: payload["error_message"] = error_msg
        if final_log: payload["final_log_content"] = final_log 
        if uploaded_artifacts: payload["agent_artifacts"] = uploaded_artifacts 
        if step_results: payload["executed_step_details"] = step_results 
        if updated_shared_data_payload: payload["updated_shared_data"] = updated_shared_data_payload
        
        logger.info(f"Sending final update for build {build_id} to {server_callback_url}: Status {status}")
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(server_callback_url, json=payload)
                response.raise_for_status()
            logger.info(f"Final update sent successfully for build {build_id}. Server ack: {response.json()}")
        except Exception as e: logger.error(f"Failed to send final update for build {build_id}: {e}", exc_info=True)

    send_final_update("RUNNING")

    source_checkout_dir = build_workspace / "source"
    if scm_info:
        if not clone_repo(scm_info["url"], scm_info["branch"], source_checkout_dir, build_log_file):
            send_final_update("FAILURE", error_msg="SCM clone failed.", final_log=build_log_file.read_text(encoding="utf-8", errors="ignore"), 
                              step_results=step_execution_results, updated_shared_data_payload=current_shared_data)
            return
    else: source_checkout_dir.mkdir(parents=True, exist_ok=True)

    all_steps_succeeded = True
    for i, step_data in enumerate(steps):
        script_content = step_data.get("script") 
        language = step_data.get("language", "python") 
        step_specific_environment = step_data.get("environment", {})
        step_name = step_data.get("name", f"Step {i+1}")
        step_work_dir_str = step_data.get("working_directory")
        current_work_dir = source_checkout_dir / step_work_dir_str if step_work_dir_str else source_checkout_dir
        step_start_time = datetime.now(timezone.utc).isoformat()
        step_error_message = None

        if not script_content:
            with open(build_log_file, "a", encoding="utf-8") as f: f.write(f"Skipping step '{step_name}' (no script).\n")
            step_status = "SKIPPED"
            step_end_time = datetime.now(timezone.utc).isoformat()
            step_execution_results.append({
                "name": step_name, "language": language, "status": step_status,
                "start_time": step_start_time, "end_time": step_end_time, "error_message": "No script defined"
            })
            continue
        
        step_succeeded = execute_step(
            script_content, language, current_work_dir.resolve(), 
            step_specific_environment, job_level_environment, 
            build_log_file, build_id, server_log_stream_url,
            current_shared_data.copy() 
        )
        _read_and_update_shared_data(current_work_dir.resolve(), current_shared_data, build_log_file, build_id)

        step_end_time = datetime.now(timezone.utc).isoformat()
        step_status = "SUCCESS" if step_succeeded else "FAILURE"
        if not step_succeeded:
            all_steps_succeeded = False
            step_error_message = f"Step '{step_name}' failed."
        
        step_execution_results.append({
            "name": step_name, "language": language, "status": step_status,
            "start_time": step_start_time, "end_time": step_end_time, "error_message": step_error_message
        })
        if not all_steps_succeeded: break 

    uploaded_artifacts_list = []
    if all_steps_succeeded and artifacts_config:
        uploaded_artifacts_list = collect_and_upload_artifacts(
            artifacts_config, source_checkout_dir, job_name, build_id, 
            build_log_file, server_artifact_upload_url
        )

    final_status = "SUCCESS" if all_steps_succeeded else "FAILURE"
    error_on_fail = "One or more build steps failed." if not all_steps_succeeded else None
    final_log_text = ""
    if build_log_file.exists():
        try:
            final_log_text = build_log_file.read_text(encoding="utf-8", errors="ignore")
        except Exception as e_read_log:
            logger.error(f"Could not read final log file {build_log_file} for build {build_id}: {e_read_log}")
            final_log_text = f"Error reading agent log: {e_read_log}"
    else:
        final_log_text = "Log file not found on agent."
    
    send_final_update(final_status, error_msg=error_on_fail, final_log=final_log_text,
                      uploaded_artifacts=uploaded_artifacts_list, step_results=step_execution_results,
                      updated_shared_data_payload=current_shared_data) 
    logger.info(f"Build {build_id} for job {job_name} finished with status: {final_status}")

@app.route('/api/v1/execute_job', methods=['POST'])
def handle_execute_job():
    job_details = request.json
    if not job_details or "build_id" not in job_details: 
        return jsonify({"error": "Invalid job payload"}), 400
    thread = threading.Thread(target=run_build_on_agent, args=(job_details,))
    thread.daemon = True 
    thread.start()
    return jsonify({"message": "Job accepted", "build_id": job_details["build_id"]}), 202

@app.route('/health', methods=['GET'])
def health_check(): 
    try:
        cpu_percent = psutil.cpu_percent(interval=0.1) 
        memory_info = psutil.virtual_memory()
        disk_info = shutil.disk_usage(str(AGENT_WORKSPACE_ROOT))

        health_data = {
            "status": "healthy",
            "agent_workspace": str(AGENT_WORKSPACE_ROOT),
            "python_version": sys.version,
            "platform": platform.platform(),
            "cpu_percent": cpu_percent,
            "memory_percent": memory_info.percent,
            "memory_total_gb": round(memory_info.total / (1024**3), 2),
            "memory_available_gb": round(memory_info.available / (1024**3), 2),
            "disk_workspace_total_gb": round(disk_info.total / (1024**3), 2),
            "disk_workspace_free_gb": round(disk_info.free / (1024**3), 2),
        }
        return jsonify(health_data), 200
    except Exception as e:
        logger.error(f"Error collecting health stats: {e}", exc_info=True)
        return jsonify({"status": "unhealthy", "error": str(e)}), 500

if __name__ == '__main__': 
    AGENT_WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    AGENT_ARTIFACTS_ROOT.mkdir(parents=True, exist_ok=True)
    logger.info(f"PyForge Agent starting on port {AGENT_PORT}")
    logger.info(f"Agent Workspace: {AGENT_WORKSPACE_ROOT}")
    logger.info(f"Agent Artifact Staging: {AGENT_ARTIFACTS_ROOT}")
    app.run(host='0.0.0.0', port=AGENT_PORT, debug=False)
