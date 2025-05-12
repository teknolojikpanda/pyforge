import time
import schedule
import git # Import the GitPython library
from threading import Thread
from pathlib import Path
from datetime import datetime, timezone # Added for setting end_time on error

from pyforge_engine.job_manager import JobManager
from pyforge_engine.build_executor import BuildExecutor, active_builds
from pyforge_engine.scm_handler import SCMHandler
from pyforge_engine.logger_setup import logger

# --- Global Instances (shared between poller and web UI if any) ---
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_ROOT = PROJECT_ROOT / "data"
JOBS_CONFIG_PATH = PROJECT_ROOT / "jobs_config" # Explicitly define the path to jobs_config

job_manager = JobManager(jobs_config_dir=JOBS_CONFIG_PATH) # Pass the explicit path
build_executor = BuildExecutor(job_manager, DATA_ROOT) # Pass job_manager and DATA_ROOT

# --- SCM Polling ---
# Store last known commit hashes for each SCM-polled job
# Key: job_name, Value: commit_hash
# This should be persisted ideally (e.g., in a small db or file)
scm_poll_last_commits = {} 

def scm_polling_task():
    logger.info("Running SCM polling task...")
    job_manager.load_jobs() # Refresh job configs, in case they changed
    
    for job in job_manager.list_jobs():
        if not job.scm:
            continue

        is_poll_trigger = any(t.type == "scm_poll" for t in job.triggers)
        if not is_poll_trigger:
            continue

        logger.debug(f"Polling SCM for job: {job.name}")
        scm_handler = SCMHandler(job.scm.url, job.scm.branch)
        
        # For polling, we need a way to check remote without full clone.
        # git ls-remote is the command. GitPython can do this:
        try:
            g = git.cmd.Git() # Requires git executable in PATH
            # Example: git ls-remote --heads https://github.com/user/repo.git main
            output = g.ls_remote('--heads', job.scm.url, job.scm.branch)
            if not output:
                logger.warning(f"No remote branch '{job.scm.branch}' found for {job.name} at {job.scm.url}")
                continue
            
            # Output is like: "commit_sha\trefs/heads/branch_name"
            latest_remote_commit = output.split('\t')[0]
            logger.debug(f"Job {job.name}: Remote commit: {latest_remote_commit}, Last known: {scm_poll_last_commits.get(job.name)}")

            if job.name not in scm_poll_last_commits:
                # First time polling this job, just store the commit
                scm_poll_last_commits[job.name] = latest_remote_commit
                logger.info(f"Initialized SCM polling for {job.name} with commit {latest_remote_commit}")
            elif scm_poll_last_commits[job.name] != latest_remote_commit:
                logger.info(f"Detected new commit for {job.name}: {latest_remote_commit}. Triggering build.")
                build_executor.trigger_build(job.name, triggered_by="scm_poll")
                scm_poll_last_commits[job.name] = latest_remote_commit # Update last known commit
            else:
                logger.debug(f"No changes detected for job {job.name}")

        except git.exc.GitCommandError as e:
            logger.error(f"Git command error during SCM polling for {job.name}: {e}")
        except Exception as e:
            logger.error(f"Error during SCM polling for {job.name}: {e}", exc_info=True)


def run_scheduler():
    logger.info("Starting SCM polling scheduler.")
    # Configure schedule based on job definitions (more complex)
    # For simplicity, poll all "scm_poll" jobs every 1 minute here.
    # A more robust system would parse cron from each job.
    schedule.every(1).minutes.do(scm_polling_task) # Simplified global poll interval
    
    # TODO: Dynamically schedule based on each job's cron string.
    # For job in job_manager.list_jobs():
    #   For trigger in job.triggers:
    #     if trigger.type == "scm_poll" and trigger.cron:
    #       # Parse cron and schedule using the 'schedule' library's flexible API
    #       # e.g., schedule.every().monday.at("10:30").do(task_for_job, job.name)
    #       pass

    while True:
        schedule.run_pending()
        time.sleep(1)

# --- Web UI (Flask Example) ---
# You would put your Flask app code here, importing `job_manager` and `build_executor`
# Example: from web_ui.app import create_app
# web_app = create_app(job_manager, build_executor)

if __name__ == "__main__":
    logger.info("Starting PyForge Engine...")

    # Start SCM poller in a separate thread
    poller_thread = Thread(target=run_scheduler, daemon=True)
    poller_thread.start()
    logger.info("SCM Poller thread started.")

    # If you have a web UI:
    from web_ui.app import create_app # Assuming you create this file
    flask_app = create_app(job_manager, build_executor)
    logger.info("Starting Web UI on http://127.0.0.1:5000")
    flask_app.run(debug=True, use_reloader=False, host="0.0.0.0", port=5000) # Disable reloader to avoid issues with scheduler/threads

    # If no web UI, just keep the main thread alive for the poller or other services
    logger.info("PyForge Engine running. CLI is available. Press Ctrl+C to stop.")
    try:
        while True:
            # Check status of build tasks submitted to the executor pool
            processed_build_ids_in_loop = set() # To avoid re-processing in the same loop iteration
            for build_id, future in list(active_builds.items()):
                if future.done() and build_id not in processed_build_ids_in_loop:
                    processed_build_ids_in_loop.add(build_id)
                    try:
                        # Call result() to raise any exceptions that occurred in the thread
                        # (e.g., unhandled errors in _dispatch_to_agent or _run_build_process)
                        future.result()
                    except Exception as e:
                        logger.error(f"Build task for ID {build_id} raised an unhandled exception: {e}", exc_info=True)
                        # Attempt to update the build status to ERROR if it's not already terminal
                        build_obj = build_executor.get_build_status_obj(build_id)
                        if build_obj and build_obj.status not in ["ERROR", "FAILURE", "SUCCESS", "CANCELLED"]:
                            build_obj.status = "ERROR"
                            build_obj.error_message = f"Build execution thread failed: {str(e) or type(e).__name__}"
                            if not build_obj.end_time:
                                build_obj.end_time = datetime.now(timezone.utc).isoformat()
                            build_executor.save_build_status(build_obj)
                            logger.error(
                                f"Build #{build_obj.build_number} (ID: {build_id}) for job '{build_obj.job_name}' "
                                f"marked as ERROR due to thread failure. Error: {build_obj.error_message}"
                            )
                            # NOTIFICATION_HOOK: send_build_error_notification(build_obj)

                    # Check the persisted build status, which is the source of truth
                    build_obj = build_executor.get_build_status_obj(build_id)
                    if build_obj and build_obj.status in ["ERROR", "FAILURE"]:
                        logger.error(
                            f"Build #{build_obj.build_number} (ID: {build_id}) for job '{build_obj.job_name}' "
                            f"finished with status: {build_obj.status}. "
                            f"Details: {build_obj.error_message or 'No specific error message provided.'}"
                        )
                        # NOTIFICATION_HOOK: send_build_error_notification(build_obj)
                    elif build_obj and build_obj.status == "SUCCESS":
                        logger.info(
                            f"Build #{build_obj.build_number} (ID: {build_id}) for job '{build_obj.job_name}' "
                            f"finished successfully."
                        )
                        # NOTIFICATION_HOOK: send_build_success_notification(build_obj)

                    # Remove from active_builds as the initial task submission is complete.
                    if build_id in active_builds:
                        del active_builds[build_id]
            time.sleep(5) # Keep main thread alive
    except KeyboardInterrupt:
        logger.info("PyForge Engine shutting down...")
    finally:
        logger.info("Waiting for active builds to complete (or cancelling)...")
        # build_executor_pool.shutdown(wait=True) # Wait for existing tasks
        # For a quicker shutdown, you might need to implement cancellation in _run_build_process
        build_executor.shutdown(wait=False) # Use the BuildExecutor's own shutdown method
        logger.info("Shutdown complete.")
