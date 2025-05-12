from flask import Flask, render_template, redirect, url_for, request, abort, Response, send_file, jsonify
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any # Added Dict, Any
from werkzeug.utils import escape # For sanitizing URL parameters
import json # Import json for parsing CLI output
import subprocess
import sys
import httpx # For agent health checks
from pyforge_engine.agent_manager import AgentInfo # For type hinting
from pyforge_engine.logger_setup import LOGS_DIR as BUILD_LOGS_DIR, logger as global_logger # Import LOGS_DIR and alias it

# These would be passed in from main_server.py
# from pyforge_engine.job_manager import JobManager
# from pyforge_engine.build_executor import BuildExecutor

# Global instances (will be set by create_app)
job_manager_instance = None
build_executor_instance = None

def to_datetime_filter(value):
    """Converts an ISO 8601 string to a datetime object."""
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc) 
        return dt
    except (ValueError, TypeError):
        return None 

def create_app(job_manager, build_executor):
    global job_manager_instance, build_executor_instance
    job_manager_instance = job_manager
    build_executor_instance = build_executor
    
    app = Flask(__name__)
    app.template_folder = Path(__file__).parent / "templates"
    app.jinja_env.filters['to_datetime'] = to_datetime_filter

    @app.route('/')
    def index():
        jobs = job_manager_instance.list_jobs()
        return render_template('index.html', jobs=jobs)

    @app.route('/job/<job_name>')
    def job_detail(job_name):
        job = job_manager_instance.get_job(job_name)
        if not job:
            abort(404, description="Job not found")
        
        job_dict = job.to_dict() # Use the Job's to_dict method
        builds = build_executor_instance.list_builds_for_job(job_name, limit=25)
        return render_template('job_detail.html', job=job_dict, builds=builds)

    @app.route('/job/<job_name>/build', methods=['POST'])
    def trigger_job_build(job_name):
        build = build_executor_instance.trigger_build(job_name, triggered_by="web_ui")
        if build:
            return redirect(url_for('build_detail', job_name=job_name, build_id=build.id))
        else:
            return redirect(url_for('job_detail', job_name=job_name)) 

    @app.route('/job/<job_name>/build/<build_id>')
    def build_detail(job_name, build_id):
        build_info = build_executor_instance.get_build_status(build_id) 
        if not build_info:
            abort(404, description="Build not found")
        
        log_content = build_executor_instance.get_build_log_content(job_name, build_id)
        return render_template('build_detail.html', job_name=job_name, build=build_info, log_content=log_content)

    @app.route('/artifacts/<path:artifact_path>')
    def serve_artifact(artifact_path):
        ARTIFACTS_ROOT_DIR_UI = build_executor_instance.artifacts_root_dir
        
        full_artifact_path = ARTIFACTS_ROOT_DIR_UI / artifact_path
        if not full_artifact_path.is_file():
            abort(404, "Artifact not found")
        
        try:
            full_artifact_path.resolve().relative_to(ARTIFACTS_ROOT_DIR_UI.resolve())
        except ValueError: # pragma: no cover
            abort(403, "Forbidden: Access to artifact outside designated directory.")

        return send_file(full_artifact_path, as_attachment=True)

    # --- Agent API Endpoints ---
    @app.route('/api/agent/build/<build_id>/update', methods=['POST'])
    def agent_build_update(build_id):
        data = request.json
        if not data: # pragma: no cover
            return jsonify({"error": "Invalid request, JSON payload expected"}), 400

        status = data.get('status')
        final_log_content = data.get('final_log_content') 
        error_message = data.get('error_message')
        agent_artifacts = data.get('agent_artifacts') 
        executed_step_details = data.get('executed_step_details') 
        updated_shared_data = data.get('updated_shared_data') # New field for shared data


        if not status: # pragma: no cover
            return jsonify({"error": "Missing 'status' in payload"}), 400

        global_logger.debug(f"Received agent update for build {build_id}, payload keys: {list(data.keys())}")
        success = build_executor_instance.update_build_from_agent(
            build_id,
            status=status,
            log_content=final_log_content,
            error_message=error_message,
            agent_artifacts=agent_artifacts,
            executed_step_details=executed_step_details, 
            updated_shared_data=updated_shared_data 
        )
        # Note: Executor release is now handled in BuildExecutor.update_build_from_agent
        # when a terminal status is received. This is more robust as it ensures
        # the Build object is updated first.

        if success:
            return jsonify({"message": "Build status updated successfully"}), 200
        else: # pragma: no cover
            return jsonify({"error": "Failed to update build status or build not found"}), 404

    @app.route('/api/agent/build/<build_id>/stream_log', methods=['POST'])
    def agent_stream_log(build_id):
        # TODO: SECURE THIS ENDPOINT (same as /update)
        data = request.json
        if not data: # pragma: no cover
            return jsonify({"error": "Invalid request, JSON payload expected"}), 400

        log_lines = data.get('lines')
        if not isinstance(log_lines, list): # pragma: no cover
            return jsonify({"error": "Missing 'lines' (list of strings) in payload"}), 400

        build = build_executor_instance.get_build_status_obj(build_id)
        if not build: # pragma: no cover
            return jsonify({"error": "Build not found"}), 404
        
        # Append to the server-side log for this agent build
        # This is typically the dispatch_server.log
        server_log_path = build_executor_instance.get_build_log_path(build.job_name, build.id, on_server=True)
        try:
            with open(server_log_path, "a", encoding="utf-8") as f:
                for line in log_lines:
                    f.write(line + "\n") # Ensure newline if agent sends raw lines
            return jsonify({"message": f"{len(log_lines)} log lines received"}), 200
        except Exception as e: # pragma: no cover
            build_executor_instance.logger.error(f"Failed to append streamed logs for build {build_id}: {e}", exc_info=True)
            return jsonify({"error": "Failed to append logs on server"}), 500

    @app.route('/api/agent/build/<build_id>/artifact', methods=['POST'])
    def agent_upload_artifact(build_id):
        build = build_executor_instance.get_build_status_obj(build_id)
        if not build: # pragma: no cover
            return jsonify({"error": "Build not found"}), 404
        if not build.agent_name: # pragma: no cover
            return jsonify({"error": "This build is not an agent build"}), 400

        if 'file' not in request.files: # pragma: no cover
            return jsonify({"error": "No file part in the request"}), 400
        
        file_storage = request.files['file']
        if file_storage.filename == '': # pragma: no cover
            return jsonify({"error": "No selected file"}), 400

        server_artifacts_root = build_executor_instance.artifacts_root_dir
        job_artifact_dir = server_artifacts_root / build.job_name / build.id
        job_artifact_dir.mkdir(parents=True, exist_ok=True)
        
        filename = Path(file_storage.filename).name 
        save_path = job_artifact_dir / filename
        
        try:
            file_storage.save(save_path)
            relative_server_path = str(save_path.relative_to(server_artifacts_root))
            
            current_server_artifacts = build.artifact_paths or []
            if not any(art.get('path') == relative_server_path for art in current_server_artifacts):
                 current_server_artifacts.append({"name": filename, "path": relative_server_path})
            build.artifact_paths = current_server_artifacts
            build_executor_instance.save_build_status(build)

            return jsonify({"message": f"Artifact '{filename}' uploaded successfully.", "path": relative_server_path}), 200
        except Exception as e: # pragma: no cover
            build_executor_instance.logger.error(f"Failed to save uploaded artifact {filename} for build {build_id}: {e}", exc_info=True)
            return jsonify({"error": f"Failed to save artifact: {e}"}), 500

    @app.route('/api/job/<job_name>/build/<build_id>/log_update')
    def api_build_log_update(job_name, build_id):
        start_offset = request.args.get('offset', 0, type=int)
        
        build_obj = build_executor_instance.get_build_status_obj(build_id)
        if not build_obj:
            return jsonify({"error": "Build not found", "status": "UNKNOWN", "new_offset": start_offset, "log_delta": ""}), 404

        log_file_to_tail: Optional[Path] = None

        if build_obj.agent_name: # Agent build
            log_file_to_tail = build_executor_instance.get_build_log_path(job_name, build_id, on_server=True)
        elif build_obj.log_file_path: # Local build, path is relative to BUILD_LOGS_DIR.parent
            if isinstance(build_obj.log_file_path, str):
                 log_file_to_tail = BUILD_LOGS_DIR.parent / build_obj.log_file_path
            else:
                build_executor_instance.logger.warning(f"Build {build_id} has invalid log_file_path type: {type(build_obj.log_file_path)}")
        
        if not log_file_to_tail:
            return jsonify({"status": build_obj.status, "new_offset": 0, "log_delta": "Log file path not yet available or build misconfigured.\n"})

        log_delta = ""
        new_offset = start_offset

        if log_file_to_tail.exists():
            try:
                current_file_size = log_file_to_tail.stat().st_size
                if current_file_size > start_offset:
                    with open(log_file_to_tail, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(start_offset)
                        log_delta = f.read()
                    new_offset = current_file_size
                else: 
                    new_offset = current_file_size 
            except Exception as e:
                build_executor_instance.logger.error(f"Error reading log delta for build {build_id} from {log_file_to_tail}: {e}", exc_info=True)
                log_delta = f"[Error reading log on server: {str(e)}]\n"
        else:
            log_delta = "[Log file not found on server yet]\n"
                
        return jsonify({
            "status": build_obj.status, 
            "new_offset": new_offset, 
            "log_delta": log_delta
        })

    # --- Helper function to get agent details for UI ---
    def _get_agent_display_details(agent_info_obj: AgentInfo) -> dict:
        """Fetches configuration and performs a health check for display."""
        details = {
            "name": agent_info_obj.name,
            "address": agent_info_obj.address,
            "tags": ', '.join(agent_info_obj.tags) if agent_info_obj.tags else 'None',
            "max_executors": agent_info_obj.max_executors,
            "current_executors_used": agent_info_obj.current_executors_used, # Add this line
            "current_load_str": f"{agent_info_obj.current_executors_used}/{agent_info_obj.max_executors}",
            "connection_status": "Unknown",
            "python_version": "N/A",
            "platform": "N/A",
            "cpu_percent": "N/A",
            "memory_percent": "N/A",
        }
        if not agent_info_obj.address:
            details["connection_status"] = "Address Not Configured"
            return details

        try:
            health_check_timeout = httpx.Timeout(connect=2.0, read=5.0, write=2.0, pool=2.0)
            with httpx.Client(timeout=health_check_timeout) as client:
                response = client.get(f"{agent_info_obj.address}/health")
                if response.status_code == 200:
                    details["connection_status"] = "Reachable"
                    agent_health_data = response.json()
                    details["python_version"] = agent_health_data.get("python_version", "N/A").splitlines()[0]
                    details["platform"] = agent_health_data.get("platform", "N/A")
                    details["cpu_percent"] = f"{agent_health_data.get('cpu_percent', 'N/A')}%"
                    details["memory_percent"] = f"{agent_health_data.get('memory_percent', 'N/A')}%"
                else:
                    details["connection_status"] = f"Unhealthy (HTTP {response.status_code})"
        except httpx.TimeoutException:
            details["connection_status"] = "Unreachable (Timeout)"
        except httpx.RequestError:
            details["connection_status"] = "Unreachable (Connection Error)"
        except Exception as e:
            if hasattr(job_manager_instance, 'logger'):
                job_manager_instance.logger.debug(f"Error checking agent {agent_info_obj.name} status for UI: {e}")
            elif hasattr(build_executor_instance, 'logger'):
                build_executor_instance.logger.debug(f"Error checking agent {agent_info_obj.name} status for UI: {e}")
            else: 
                print(f"DEBUG: Error checking agent {agent_info_obj.name} status for UI: {e}")

            details["connection_status"] = "Error Checking Status"
        return details

    @app.route('/agents')
    def list_agents_view():
        """Displays a list of all configured agents and their status."""
        configured_agents = build_executor_instance.agent_manager.agents
        
        agents_display_data = []
        if configured_agents:
            for agent_name, agent_info_obj in configured_agents.items():
                agents_display_data.append(_get_agent_display_details(agent_info_obj))
        
        return render_template('agents.html', agents=agents_display_data)

    @app.route('/agent/<agent_name>')
    def agent_detail(agent_name):
        # Sanitize agent_name before using it for lookup
        safe_agent_name = str(escape(agent_name))
        agent_info = build_executor_instance.agent_manager.get_agent(safe_agent_name)
        
        if not agent_info:
            # Consider flashing a message or just rendering a "not found" state
            return render_template('agent_detail.html', agent=None, agent_name=safe_agent_name), 404
        
        # Use the same helper to get display details including health status
        agent_display_details = _get_agent_display_details(agent_info)

        # Execute 'python3 cli.py agent info <agent_name>'
        parsed_cli_agent_info: Optional[Dict[str, Any]] = None
        cli_info_error: Optional[str] = None

        project_root = Path(__file__).resolve().parent.parent # pyforge project root
        cli_script_path = project_root / "cli.py"
        command = [sys.executable, str(cli_script_path), "agent", "info", safe_agent_name]

        try:
            global_logger.debug(f"Running CLI command: {' '.join(command)}")
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,  # Handle non-zero exit codes manually
                cwd=project_root # Ensure cli.py runs from the project root
            )
            global_logger.debug(f"CLI command finished with return code: {process.returncode}")
            if process.returncode == 0:
                try:
                    parsed_cli_agent_info = json.loads(process.stdout)
                except json.JSONDecodeError:
                    cli_info_error = f"CLI command succeeded but returned invalid JSON:\n{process.stdout}"
            else:
                cli_info_error = f"CLI command failed with return code {process.returncode}:\n{process.stderr}"
        except Exception as e:
            cli_info_error = f"Exception while trying to run CLI command: {str(e)}"
            global_logger.error(cli_info_error, exc_info=True)

        return render_template('agent_detail.html', agent=agent_display_details, agent_name=safe_agent_name, parsed_cli_agent_info=parsed_cli_agent_info, cli_info_error=cli_info_error)

    return app
