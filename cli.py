import click
import time
import json
from pathlib import Path
import httpx # For agent health checks

# Adjust imports based on your final structure
from pyforge_engine.job_manager import JobManager
from pyforge_engine.build_executor import BuildExecutor # Assuming you instantiate it somewhere global or pass it
from pyforge_engine.build import Build # For type hinting if needed, and status checking
from pyforge_engine.agent_manager import AgentInfo # For type hinting
from pyforge_engine.logger_setup import logger # Global logger

# --- Initialization ---
# This is a bit simplified. In a real app, these would be managed by a central service.
# Ensure DATA_ROOT and JOBS_CONFIG_PATH are correctly pointing to your data and job configuration directories.
DATA_ROOT = Path(__file__).resolve().parent / "data"
job_manager = JobManager() # Loads jobs on init
build_executor = BuildExecutor(job_manager, DATA_ROOT)
# --- End Initialization ---


@click.group()
def cli():
    """PyForge: A simple Python CI/CD tool."""
    pass

@cli.command("list-jobs")
def list_jobs():
    """Lists all configured jobs."""
    jobs = job_manager.list_jobs()
    if not jobs:
        click.echo("No jobs configured.")
        return
    click.echo("Available jobs:")
    for job in jobs:
        click.echo(f"- {job.name}")
        click.echo(f"  Description: {job.description}")
        if job.scm:
            click.echo(f"  SCM: {job.scm.type} @ {job.scm.url} (Branch: {job.scm.branch})")
        if job.agent:
            click.echo(f"  Agent: {job.agent.name}")
            click.echo(f"    Connect Timeout: {job.agent.connect_timeout if job.agent.connect_timeout is not None else 'Default'}")
            click.echo(f"    Executor Wait Timeout: {job.agent.executor_wait_timeout if job.agent.executor_wait_timeout is not None else 'Default'}")

@cli.command("run-job")
@click.argument("job_name")
@click.option("--param", "-p", multiple=True, help="Parameter for the build (e.g., KEY=VALUE)")
def run_job(job_name: str, param: tuple):
    """Triggers a specific job to run."""
    job = job_manager.get_job(job_name)
    if not job:
        click.echo(f"Error: Job '{job_name}' not found.")
        return

    params_dict = {}
    for p_str in param:
        if "=" not in p_str:
            click.echo(f"Warning: Invalid parameter format '{p_str}'. Use KEY=VALUE. Skipping.")
            continue
        key, value = p_str.split("=", 1)
        params_dict[key] = value
    
    click.echo(f"Triggering job '{job_name}'...")
    if params_dict:
        click.echo(f"With parameters: {params_dict}")

    build_instance = build_executor.trigger_build(job_name, triggered_by="cli", params=params_dict)

    if build_instance:
        click.echo(f"Build #{build_instance.build_number} (ID: {build_instance.id}) for job '{job_name}' started/queued.")
        click.echo(f"Status: {build_instance.status}") # Status is a string in Build dataclass
        click.echo("You can monitor logs in data/build_logs/ or via the web UI (if running).")
    else:
        click.echo(f"Failed to trigger build for '{job_name}'. Check logs or concurrent build limit.")


@cli.command("build-status")
@click.argument("build_id")
def build_status(build_id: str):
    """Gets the status of a specific build."""
    status_info = build_executor.get_build_status(build_id)
    if status_info:
        click.echo(json.dumps(status_info, indent=2))
    else:
        click.echo(f"Build with ID '{build_id}' not found.")

@cli.command("build-log")
@click.argument("job_name") # Easier to find log if job_name is known
@click.argument("build_id")
@click.option("--follow", "-f", is_flag=True, help="Follow the log output.")
def build_log(job_name: str, build_id: str, follow: bool):
    """Shows the log for a specific build."""
    
    # First, get build status to see if it's running
    build_info = build_executor.get_build_status(build_id)
    if not build_info:
        click.echo(f"Build {build_id} for job {job_name} not found.")
        return

    log_content_func = lambda: build_executor.get_build_log_content(job_name, build_id)
    
    if not follow:
        log_content = log_content_func()
        if log_content:
            click.echo(log_content)
        else:
            click.echo(f"Log file for build '{build_id}' of job '{job_name}' not found or empty.")
    else:
        click.echo(f"Following log for {job_name} build {build_id}. Press Ctrl+C to stop.")
        last_pos = 0
        try:
            while True:
                log_content = log_content_func()
                if log_content and len(log_content) > last_pos:
                    click.echo(log_content[last_pos:], nl=False) # nl=False to avoid extra newlines
                    last_pos = len(log_content)
                
                # Update build_info to check if build is still running
                current_build_info = build_executor.get_build_status(build_id)
                # Define non-terminal statuses more explicitly based on your Build status strings
                non_terminal_statuses = [
                    "PENDING", "DISPATCHING", "WAITING_FOR_AGENT", 
                    "WAITING_FOR_AGENT_EXECUTOR", "RUNNING"
                ]
                if current_build_info and current_build_info['status'] not in non_terminal_statuses:
                    # One final read to catch any remaining logs
                    log_content = log_content_func()
                    if log_content and len(log_content) > last_pos:
                        click.echo(log_content[last_pos:], nl=False)
                    click.echo(f"\nBuild {build_id} finished with status: {current_build_info['status']}.")
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            click.echo("\nStopped following log.")

@cli.command("list-builds")
@click.argument("job_name")
@click.option("--limit", default=10, type=int, help="Number of recent builds to show.")
def list_builds(job_name: str, limit: int):
    """Lists recent builds for a job."""
    builds = build_executor.list_builds_for_job(job_name, limit=limit)
    if not builds:
        click.echo(f"No builds found for job '{job_name}'.")
        return
    
    click.echo(f"Recent builds for '{job_name}':")
    for b in builds:
        start_time = b.get('start_time', 'N/A')
        if start_time and start_time != 'N/A':
            start_time = start_time.split('.')[0].replace('T', ' ') # Prettier format
        status = b.get('status', 'UNKNOWN')
        click.echo(f"  - Build #{b['build_number']} (ID: {b['id']}) | Status: {status} | Started: {start_time} | Trigger: {b.get('triggered_by', 'N/A')}")

@cli.group("agent")
def agent_group():
    """Manage and inspect PyForge agents."""
    pass

def get_agent_details(agent: AgentInfo) -> dict:
    """Performs a health check and fetches detailed stats from the agent."""
    details = {
        "name": agent.name,
        "address": agent.address,
        "tags": ', '.join(agent.tags) if agent.tags else 'None',
        "max_executors": agent.max_executors,
        "current_load_str": f"{agent.current_executors_used}/{agent.max_executors}",
        "connection_status": "Unknown", # Default
        "python_version": "N/A",
        "platform": "N/A",
        "cpu_percent": "N/A",
        "memory_percent": "N/A",
        "memory_total_gb": "N/A",
        "memory_available_gb": "N/A",
        "disk_workspace_total_gb": "N/A",
        "disk_workspace_free_gb": "N/A",
    }
    if not agent.address:
        details["connection_status"] = "Address Not Configured"
        return details

    try:
        # Use a short timeout for the health check
        health_check_timeout = httpx.Timeout(connect=2.0, read=5.0, write=2.0, pool=2.0)
        with httpx.Client(timeout=health_check_timeout) as client:
            response = client.get(f"{agent.address}/health") # Assuming /health endpoint exists on agent
            if response.status_code == 200:
                details["connection_status"] = "Reachable"
                agent_health_data = response.json()
                details["python_version"] = agent_health_data.get("python_version", "N/A").splitlines()[0] # Get first line
                details["platform"] = agent_health_data.get("platform", "N/A")
                details["cpu_percent"] = f"{agent_health_data.get('cpu_percent', 'N/A')}%"
                details["memory_percent"] = f"{agent_health_data.get('memory_percent', 'N/A')}%"
                details["memory_total_gb"] = f"{agent_health_data.get('memory_total_gb', 'N/A')} GB"
                details["memory_available_gb"] = f"{agent_health_data.get('memory_available_gb', 'N/A')} GB"
                details["disk_workspace_total_gb"] = f"{agent_health_data.get('disk_workspace_total_gb', 'N/A')} GB"
                details["disk_workspace_free_gb"] = f"{agent_health_data.get('disk_workspace_free_gb', 'N/A')} GB"
            else:
                details["connection_status"] = f"Unhealthy (HTTP {response.status_code})"
    except httpx.TimeoutException:
        details["connection_status"] = "Unreachable (Timeout)"
    except httpx.RequestError:
        details["connection_status"] = "Unreachable (Connection Error)"
    except Exception as e:
        logger.debug(f"Unexpected error checking agent {agent.name} status: {e}")
        details["connection_status"] = "Error Checking Status"
    return details

@agent_group.command("list")
def list_agents():
    """Lists all configured agents, their load, and connection status."""
    agents_data = build_executor.agent_manager.agents # Access agents via agent_manager
    if not agents_data:
        click.echo("No agents configured.")
        return
    
    click.echo("Configured Agents:")
    # Adjusted column widths for new info
    click.echo(f"{'Name':<20} {'Address':<25} {'Load':<10} {'Status':<25} {'Python Ver.':<15} {'OS':<25} {'Tags'}")
    click.echo("-" * 150) # Increased width
    for agent_name, agent_info_obj in agents_data.items():
        details = get_agent_details(agent_info_obj) # Fetch all details
        
        # Truncate long Python versions or OS strings for list view
        python_v_short = details['python_version'].split(' ')[0] if details['python_version'] != "N/A" else "N/A"
        platform_short = details['platform'][:22] + '...' if len(details['platform']) > 25 else details['platform']

        click.echo(
            f"{details['name']:<20} "
            f"{details['address']:<25} "
            f"{details['current_load_str']:<10} "
            f"{details['connection_status']:<25} "
            f"{python_v_short:<15} "
            f"{platform_short:<25} "
            f"{details['tags']}"
        )

@agent_group.command("info")
@click.argument("agent_name")
def agent_info_cmd(agent_name: str):
    """Shows detailed configuration, load, and connection status for a specific agent."""
    agent = build_executor.agent_manager.get_agent(agent_name)
    if not agent:
        click.echo(f"Error: Agent '{agent_name}' not found in configuration.")
        return

    click.echo(f"Agent Information: {agent.name}")
    details = get_agent_details(agent)

    click.echo(f"  Address: {details['address']}")
    click.echo(f"  Tags: {details['tags']}")
    click.echo(f"  Max Executors: {details['max_executors']}")
    click.echo(f"  Current Load: {details['current_load_str']} executors used")
    click.echo(f"  Connection Status: {details['connection_status']}")
    
    if details["connection_status"] == "Reachable":
        click.echo("\n  Hardware & System Info:")
        click.echo(f"    Python Version: {details['python_version']}")
        click.echo(f"    Platform: {details['platform']}")
        click.echo(f"    CPU Usage: {details['cpu_percent']}")
        click.echo(f"    Memory Usage: {details['memory_percent']} (Total: {details['memory_total_gb']}, Available: {details['memory_available_gb']})")
        click.echo(f"    Workspace Disk: Total: {details['disk_workspace_total_gb']}, Free: {details['disk_workspace_free_gb']}")
    else:
        click.echo("  Hardware & System Info: Could not retrieve (agent unreachable, unhealthy, or error during check).")

if __name__ == '__main__':
    cli()
